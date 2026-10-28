"""Scripted mix selection/projection; not real provider routing assertions."""

import json

import pytest
from conftest import PROJECT_ID
from pydantic import ValidationError
from test_agent import FakeModel, call, final, run_agent
from test_decision_support import old_context, with_context
from test_portfolio import PATH, portfolio  # noqa: F401
from test_quotations import quotations  # noqa: F401

from app.agent.errors import InvalidModelResponse
from app.agent.prompt import SYSTEM_PROMPT
from app.contracts.portfolio import ManagementQuotationPortfolio
from app.conversation.context import ConversationContext, ConversationTurn


@pytest.fixture
def mix_portfolio(payloads, request):
    request.getfixturevalue("portfolio")
    portfolio_data = payloads[PATH]
    portfolio_data["itemCount"] = 10
    for key in ("highestValueItems", "highestUnitPriceItems"):
        base = portfolio_data[key]["items"][0]
        portfolio_data[key] = dict(
            total=10,
            returned=10,
            hasMore=False,
            items=[{**base, "sourceRow": i + 15} for i in range(10)],
        )

    def dimension(names, combination=False):
        rows = []
        for value, count, quantity, price in zip(
            names, (1, 4, 2), (20, 4, 2), ("5.00", "2.00", "100.00"), strict=True
        ):
            row = dict(itemCount=count, totalQuantity=quantity, totalQuotedValue=price)
            row.update({"system": value, "glass": "Laminado"} if combination else {"value": value})
            rows.append(row)

        def ranking(order):
            return dict(total=3, returned=3, hasMore=False, items=[rows[i] for i in order])

        return dict(
            totalItems=10,
            specifiedItems=7,
            unspecifiedItems=3,
            byQuantity=ranking((0, 1, 2)),
            byItemCount=ranking((1, 2, 0)),
            byQuotedValue=ranking((2, 0, 1)),
        )

    portfolio_data["mix"] = dict(
        systems=dimension(("Venecia 40", "VENECIA SERIE 40", "Venecia S40")),
        glass=dimension(("Laminado", "Templado", "Crudo")),
        aluminumFinishes=dimension(("Negro Mate", "NEGRO MATE", "Natural")),
        systemGlassCombinations=dimension(("Venecia 40", "VENECIA SERIE 40", "Venecia S40"), True),
    )
    return portfolio_data


@pytest.mark.anyio
@pytest.mark.parametrize(
    "question,dimension,ranking,expected",
    [
        ("¿Qué sistema hemos cotizado más?", "systems", "byQuantity", "Venecia 40"),
        ("¿Cuál aparece en más ítems?", "systems", "byItemCount", "VENECIA SERIE 40"),
        ("¿Qué sistema concentra mayor valor?", "systems", "byQuotedValue", "Venecia S40"),
        ("¿Qué vidrio cotizamos más?", "glass", "byQuantity", "Laminado"),
        ("¿Qué vidrio aparece más frecuentemente?", "glass", "byItemCount", "Templado"),
        ("¿Qué acabado tiene más unidades?", "aluminumFinishes", "byQuantity", "Negro Mate"),
        ("¿Qué combinación cotizamos más?", "systemGlassCombinations", "byQuantity", "Venecia 40"),
    ],
)
async def test_mix_uses_one_official_read_without_reaggregation_or_project_references(
    mix_portfolio, mock_backend, question, dimension, ranking, expected
):
    model = FakeModel(call("get_quotation_portfolio"), final(expected))
    result, metrics = await run_agent(model, mock_backend[0], question)
    assert result.answer == expected and not result.references
    assert result.response_type == "direct" and metrics["tool_calls"] == 1
    assert [r["path"] for r in mock_backend[1]] == ["/api/auth/me", PATH]
    evidence = json.loads(model.inputs[-1][0][-1].content)["data"]["mix"]
    assert evidence == mix_portfolio["mix"]
    first = evidence[dimension][ranking]["items"][0]
    assert first.get("value", first.get("system")) == expected


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.pop("mix"),
        lambda p: p["mix"].update(city={}),
        lambda p: p["mix"]["systems"].update(specifiedItems=8),
        lambda p: p["mix"]["systems"].update(totalItems=11, unspecifiedItems=4),
        lambda p: p["mix"]["systems"].update(unspecifiedItems=-1),
        lambda p: p["mix"]["systems"]["byQuantity"].update(hasMore=True),
        lambda p: p["mix"]["systems"]["byQuantity"].update(returned=4),
        lambda p: p["mix"]["systems"]["byQuantity"]["items"][0].update(value=" "),
        lambda p: p["mix"]["systems"]["byQuantity"]["items"][0].update(totalQuotedValue="0.001"),
        lambda p: p["mix"]["systems"]["byQuantity"]["items"][0].update(
            totalQuotedValue="1000000000000000000.00"
        ),
        lambda p: p["mix"]["systems"]["byQuantity"]["items"][0].update(projectId="unexpected"),
        lambda p: p["mix"]["systems"]["byQuantity"]["items"][0].update(itemCount=0),
        lambda p: p["mix"]["systemGlassCombinations"]["byQuantity"]["items"][0].update(glass=""),
    ],
)
def test_mix_contract_fails_closed(mix_portfolio, change):
    change(mix_portfolio)
    with pytest.raises(ValidationError):
        ManagementQuotationPortfolio.model_validate_json(json.dumps(mix_portfolio))


@pytest.mark.anyio
async def test_unspecified_is_coverage_not_a_category(mix_portfolio, mock_backend):
    model = FakeModel(
        call("get_quotation_portfolio"), final("Tres de diez ítems no tienen sistema especificado.")
    )
    result, _ = await run_agent(model, mock_backend[0])
    data = json.loads(model.inputs[-1][0][-1].content)["data"]["mix"]["systems"]
    assert (data["totalItems"], data["specifiedItems"], data["unspecifiedItems"]) == (10, 7, 3)
    assert "No especificado" not in str(data) and not result.references


@pytest.mark.anyio
async def test_partial_mix_marks_envelope_incomplete(mix_portfolio, mock_backend):
    # Category coverage is preserved separately from item coverage.
    dim = mix_portfolio["mix"]["systems"]
    dim.update(totalItems=20, specifiedItems=20, unspecifiedItems=0)
    mix_portfolio["itemCount"] = 20
    for ranking in ("byQuantity", "byItemCount", "byQuotedValue"):
        first = dim[ranking]["items"][0]
        dim[ranking] = dict(
            total=20,
            returned=10,
            hasMore=True,
            items=[{**first, "value": str(i)} for i in range(10)],
        )
    for name in ("glass", "aluminumFinishes", "systemGlassCombinations"):
        mix_portfolio["mix"][name].update(totalItems=20, unspecifiedItems=13)
    for name in ("highestValueItems", "highestUnitPriceItems"):
        mix_portfolio[name].update(total=20, hasMore=True)
    model = FakeModel(
        call("get_quotation_portfolio"), final("El top no contiene todas las categorías.")
    )
    await run_agent(model, mock_backend[0])
    assert json.loads(model.inputs[-1][0][-1].content)["complete"] is False


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["factual", "analysis", "recommendation"])
async def test_memory_cannot_supply_mix_and_decision_support_reads_again(
    mix_portfolio, mock_backend, mode
):
    context = old_context()
    with pytest.raises(InvalidModelResponse):
        await with_context(
            FakeModel(final("Predomina Venecia 40.", mode=mode)), mock_backend[0], context
        )
    model = FakeModel(
        call("get_quotation_portfolio"),
        final("Si usamos lo cotizado como señal, revisaría Venecia 40.", mode=mode),
    )
    result = await with_context(model, mock_backend[0], context)
    assert not result.references and mock_backend[1][-1]["path"] == PATH


def test_explicit_mix_semantics_in_prompt():
    for phrase in (
        "quantity != frequency != quoted value",
        "quotation mix != demand",
        "quotation mix != sales",
        "quotation mix != profitability",
        "no sumes variantes",
        "No hay filtros",
        "reference_keys=[]",
        "no estar en un ranking parcial",
        "systems.byQuantity.items[0]",
        "systems.byItemCount.items[0]",
        "systems.byQuotedValue.items[0]",
    ):
        assert phrase in SYSTEM_PROMPT


@pytest.mark.anyio
@pytest.mark.usefixtures("quotations")
async def test_individual_system_then_global_mix_uses_different_capability(
    mix_portfolio, mock_backend
):
    individual = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        call("get_commercial_quotation", {"project_id": PROJECT_ID}, "quotation"),
        final("Casa Ximena usa el sistema B.", [f"project:{PROJECT_ID}"]),
    )
    first, metrics = await run_agent(individual, mock_backend[0], "¿Qué sistema usa Casa Ximena?")
    assert metrics["tools_used"] == ["get_project_details", "get_commercial_quotation"]
    context = ConversationContext(
        turns=(ConversationTurn.completed("text", "¿Qué sistema usa?", first),)
    )
    global_model = FakeModel(
        call("get_quotation_portfolio"), final("Venecia 40 tiene 20 unidades cotizadas.")
    )
    result = await with_context(
        global_model, mock_backend[0], context, "¿Y cuál usamos más de todos?"
    )
    assert not result.references
    assert [r["path"] for r in mock_backend[1]][-2:] == ["/api/auth/me", PATH]
    assert (
        json.loads(global_model.inputs[-1][0][-1].content)["data"]["mix"]["systems"]["byQuantity"][
            "items"
        ][0]["totalQuantity"]
        == 20
    )

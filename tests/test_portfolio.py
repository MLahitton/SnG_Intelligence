"""Synthetic contract and orchestration tests; no real provider or business database."""

import json
from uuid import UUID

import pytest
from conftest import PROJECT_ID, TOKEN
from conversation_repository import MemoryRepository
from pydantic import ValidationError
from test_agent import FakeModel, call, final, run_agent
from test_decision_support import old_context, with_context
from test_live import SESSION
from test_quotations import quotations  # noqa: F401
from test_sideband import begin, complete, function, setup_live, until

from app.agent.errors import InvalidModelResponse, InvalidToolCall, UngroundedReference
from app.agent.prompt import SYSTEM_PROMPT
from app.contracts.portfolio import ManagementQuotationPortfolio
from app.conversation.store import ConversationStore
from app.core.errors import BackendResponseError, ManagementRequired

PATH = "/api/management/quotation-portfolio"
SECOND = str(UUID(int=919))


def empty_mix(total):
    def dimension():
        def ranking():
            return dict(total=0, returned=0, hasMore=False, items=[])

        return dict(
            totalItems=total,
            specifiedItems=0,
            unspecifiedItems=total,
            byQuantity=ranking(),
            byItemCount=ranking(),
            byQuotedValue=ranking(),
        )

    return {
        key: dimension()
        for key in ("systems", "glass", "aluminumFinishes", "systemGlassCombinations")
    }


@pytest.fixture
def portfolio(payloads):
    item = dict(
        projectId=PROJECT_ID,
        projectName="Casa Ximena",
        sgCode="SG0012",
        clientName="Cliente",
        section="Main",
        sourceRow=15,
        itemNumber="PAS01",
        location="Sala",
        system="Pasamanos",
        glass="Templado",
        aluminumFinish="Natural",
        quantity=4,
        unitPrice="10.00",
        totalPrice="48609963.18",
    )
    other = {
        **item,
        "projectId": SECOND,
        "projectName": "Casa B",
        "itemNumber": "02",
        "unitPrice": "25.00",
        "totalPrice": "25.00",
        "quantity": 1,
    }
    project = {k: item[k] for k in ("projectId", "projectName", "sgCode", "clientName")}
    project.update(confirmedAt="2026-10-28T13:00:00Z", projectTotal="50000000.00")
    other_project = {
        **project,
        "projectId": SECOND,
        "projectName": "Casa B",
        "projectTotal": "60.00",
    }

    def ranking(items):
        return dict(total=len(items), returned=len(items), hasMore=False, items=items)

    result = dict(
        currencyCode="COP",
        quotationCount=2,
        itemCount=2,
        totalQuantity=5,
        totalQuotedValue="50000060.00",
        highestValueItems=ranking([item, other]),
        highestUnitPriceItems=ranking([other, item]),
        highestValueProjects=ranking([project, other_project]),
    )
    result["mix"] = empty_mix(result["itemCount"])
    payloads[PATH] = result
    return result


@pytest.mark.anyio
@pytest.mark.parametrize(
    "question,answer,identifier",
    [
        ("¿Cuál es el ítem más caro de todos los proyectos?", "$48.609.963,18 COP", PROJECT_ID),
        ("¿Cuál tiene el mayor precio unitario?", "$25,00 COP", SECOND),
        ("¿Cuál es la cotización más alta?", "$50.000.000,00 COP", PROJECT_ID),
        ("¿Cuánto suman las cotizaciones actuales?", "$50.000.060,00 COP", None),
    ],
)
async def test_global_questions_use_one_read_preserve_ranking_money_and_references(
    portfolio, mock_backend, question, answer, identifier
):
    model = FakeModel(
        call("get_quotation_portfolio"),
        final(answer, [f"project:{identifier}"] if identifier else []),
    )
    result, metrics = await run_agent(model, mock_backend[0], question)
    assert metrics["tools_used"] == ["get_quotation_portfolio"]
    assert [r["path"] for r in mock_backend[1]] == ["/api/auth/me", PATH]
    assert all(r["authorized"] for r in mock_backend[1])
    assert mock_backend[1][-1]["active_role"] == "Management"
    assert result.answer == answer and "pesos" in result.spoken_answer
    assert [str(r.project_id) for r in result.references] == ([identifier] if identifier else [])
    envelope = json.loads(model.inputs[-1][0][-1].content)
    assert envelope["data"] == ManagementQuotationPortfolio.model_validate_json(
        json.dumps(portfolio)
    ).model_dump(mode="json", by_alias=True)
    assert len(envelope["available_references"]) == 2
    assert envelope["available_presentation"] == {} and result.response_type == "direct"
    assert TOKEN not in repr(model.inputs)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(currencyCode="USD"),
        lambda p: p.pop("currencyCode"),
        lambda p: p.update(quotationCount=-1),
        lambda p: p.update(totalQuantity=True),
        lambda p: p.update(totalQuotedValue="1.234"),
        lambda p: p.update(totalQuotedValue="NaN"),
        lambda p: p.update(totalQuotedValue="1000000000000000000.00"),
        lambda p: p.update(secret="forbidden"),
        lambda p: p["highestValueItems"].update(returned=1),
        lambda p: p["highestValueItems"].update(hasMore=True),
        lambda p: p["highestValueItems"].update(items=p["highestValueItems"]["items"] * 6),
        lambda p: p["highestValueItems"]["items"][0].update(commercialCatalogItemId=PROJECT_ID),
        lambda p: p["highestValueItems"]["items"][0].update(totalPrice="0.001"),
    ],
)
def test_strict_portfolio_contract_rejects_incompatible_data(portfolio, mutation):
    mutation(portfolio)
    with pytest.raises(ValidationError):
        ManagementQuotationPortfolio.model_validate_json(json.dumps(portfolio))


@pytest.mark.anyio
async def test_empty_portfolio_is_successful_read(portfolio, mock_backend):
    portfolio.update(
        quotationCount=0, itemCount=0, totalQuantity=0, totalQuotedValue="0.00", mix=empty_mix(0)
    )
    for name in ("highestValueItems", "highestUnitPriceItems", "highestValueProjects"):
        portfolio[name] = dict(total=0, returned=0, hasMore=False, items=[])
    result, metrics = await run_agent(
        FakeModel(call("get_quotation_portfolio"), final("No hay cotizaciones actuales.")),
        mock_backend[0],
    )
    assert not result.references and metrics["tool_calls"] == 1


@pytest.mark.anyio
async def test_partial_ranking_does_not_claim_full_coverage(portfolio, mock_backend):
    # Totals belong to Backend; Intelligence must not sum this top slice.
    portfolio["itemCount"] = 37
    portfolio["mix"] = empty_mix(37)
    for name in ("highestValueItems", "highestUnitPriceItems"):
        base = portfolio[name]["items"][0]
        portfolio[name] = dict(
            total=37,
            returned=10,
            hasMore=True,
            items=[{**base, "sourceRow": i + 15} for i in range(10)],
        )
    model = FakeModel(call("get_quotation_portfolio"), final("Solo se muestran diez ítems de 37."))
    await run_agent(model, mock_backend[0])
    envelope = json.loads(model.inputs[-1][0][-1].content)
    assert envelope["complete"] is False
    assert envelope["data"]["totalQuotedValue"] == "50000060.00"


@pytest.mark.anyio
async def test_authorization_failure_does_not_read_portfolio(portfolio, payloads, mock_backend):
    payloads["/api/auth/me"]["isManagement"] = False
    with pytest.raises(ManagementRequired):
        await run_agent(FakeModel(call("get_quotation_portfolio")), mock_backend[0])
    assert [r["path"] for r in mock_backend[1]] == ["/api/auth/me"]


@pytest.mark.anyio
async def test_invalid_backend_contract_fails_safely(portfolio, mock_backend):
    portfolio["currencyCode"] = "USD"
    with pytest.raises(BackendResponseError):
        await run_agent(FakeModel(call("get_quotation_portfolio")), mock_backend[0])


@pytest.mark.anyio
async def test_no_arbitrary_limits_and_no_historical_references(portfolio, mock_backend):
    with pytest.raises(InvalidToolCall):
        await run_agent(FakeModel(call("get_quotation_portfolio", {"limit": 999})), mock_backend[0])
    for mode in ("factual", "recommendation"):
        with pytest.raises(InvalidModelResponse):
            await with_context(
                FakeModel(final("Hay valor cotizado.", mode=mode)), mock_backend[0], old_context()
            )
    model = FakeModel(
        call("get_quotation_portfolio"),
        final(keys=[f"project:{UUID(int=444)}"]),
        final(keys=[f"project:{UUID(int=444)}"]),
    )
    with pytest.raises(UngroundedReference):
        await run_agent(model, mock_backend[0])


@pytest.mark.anyio
async def test_recommendation_requeries_changed_portfolio(portfolio, mock_backend):
    context = old_context()
    for value in ("100.00", "200.00"):
        portfolio["totalQuotedValue"] = value
        model = FakeModel(
            call("get_quotation_portfolio"),
            final(
                "Si importa el valor cotizado, revisaría Casa Ximena.",
                [f"project:{PROJECT_ID}"],
                mode="recommendation",
            ),
        )
        await with_context(model, mock_backend[0], context)
        assert json.loads(model.inputs[-1][0][-1].content)["data"]["totalQuotedValue"] == value
    assert sum(r["path"] == PATH for r in mock_backend[1]) == 2


@pytest.mark.anyio
@pytest.mark.usefixtures("quotations")
async def test_live_individual_then_global_followup_keeps_session(portfolio, mock_backend):
    model = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        call("get_commercial_quotation", {"project_id": PROJECT_ID}, "quotation"),
        final(
            "La cotización de Casa Ximena tiene ítems por $42,57 COP.", [f"project:{PROJECT_ID}"]
        ),
        call("get_quotation_portfolio"),
        final("El ítem más caro del portfolio es $48.609.963,18 COP.", [f"project:{PROJECT_ID}"]),
    )
    store = ConversationStore(MemoryRepository())
    key = (await store.create("manager")).conversation_id
    try:
        async with setup_live(mock_backend[0], model) as (manager, socket, _, registry, _):
            manager._conversations = store
            await store.bind_live(SESSION, key, "manager")
            for number, question in enumerate(
                ("¿Cuál es el ítem más caro de Casa Ximena?", "¿Y de todos los proyectos?"), 1
            ):
                begin(socket, f"response_{number}")
                socket.incoming.put_nowait(function(f"call_{number}", question))
                complete(socket, f"response_{number}")
                await until(
                    lambda n=number: sum(e["type"] == "response.create" for e in socket.sent) == n
                )
                assert not socket.closed and registry.owned(SESSION, "manager")
            paths = [r["path"] for r in mock_backend[1]]
            assert paths.count(PATH) == 1
            assert paths.count(f"/api/projects/{PROJECT_ID}/commercial-quotation/view") == 1
            assert paths[-2:] == ["/api/auth/me", PATH]
            assert len(store.repository.turns[key]) == 2
    finally:
        await store.shutdown()


def test_prompt_does_not_equate_quotation_with_sales_or_full_history():
    assert "No existe consulta global de precios" not in SYSTEM_PROMPT
    for text in (
        "get_quotation_portfolio",
        "highestValueItems.items[0]",
        "highestUnitPriceItems.items[0]",
        "highestValueProjects.items[0]",
        "NO equivale a ventas",
        "no un histórico",
        "no sumes rankings",
        "No hay series",
        "N+1",
        "mayor valor no prueba mayor riesgo",
    ):
        assert text in SYSTEM_PROMPT

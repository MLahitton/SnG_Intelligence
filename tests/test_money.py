import copy
import json
from decimal import Decimal
from uuid import UUID

import pytest
from conftest import PROJECT_ID
from pydantic import ValidationError
from test_agent import FakeModel, call, final, run_agent
from test_quotations import quotations  # noqa: F401 - shared fixture

from app.agent.money import speak_cop
from app.agent.prompt import SYSTEM_PROMPT
from app.agent.quotations import quotation_data
from app.agent.rendering import render_answer
from app.contracts.quotations import ConfirmedCommercialQuotation
from app.core.errors import BackendResponseError


@pytest.mark.parametrize(
    "amount,spoken",
    [
        (
            "$5.513.429,14 COP",
            "cinco millones quinientos trece mil cuatrocientos veintinueve "
            "pesos con catorce centavos",
        ),
        (
            "$48.609.963,18 COP",
            "cuarenta y ocho millones seiscientos nueve mil novecientos sesenta y tres "
            "pesos con dieciocho centavos",
        ),
        ("$0,00 COP", "cero pesos"),
        ("$0,01 COP", "cero pesos con un centavo"),
        ("$1,00 COP", "un peso"),
        ("$21,21 COP", "veintiún pesos con veintiún centavos"),
        ("$31,31 COP", "treinta y un pesos con treinta y un centavos"),
        ("$100,00 COP", "cien pesos"),
        ("$101,00 COP", "ciento un pesos"),
        ("$1.000,00 COP", "mil pesos"),
        ("$21.000,00 COP", "veintiún mil pesos"),
        ("$1.000.000,00 COP", "un millón de pesos"),
        ("$1.000.001,00 COP", "un millón un pesos"),
        ("$1.000.000.000,00 COP", "mil millones de pesos"),
        ("$1.000.000.000.000,00 COP", "un billón de pesos"),
        (
            "$5.513.429,00 COP",
            "cinco millones quinientos trece mil cuatrocientos veintinueve pesos",
        ),
    ],
)
def test_exact_money_speech_uses_same_answer(amount, spoken):
    answer = f"El total es {amount}."
    assert render_answer(answer, set()) == (answer, f"El total es {spoken}.")


def test_full_numeric_range_and_multiple_amounts():
    group = "novecientos noventa y nueve mil novecientos noventa y nueve"
    assert (
        speak_cop("$999.999.999.999,99 COP")
        == f"{group} millones {group} pesos con noventa y nueve centavos"
    )
    assert (
        speak_cop("$999.999.999.999.999.999,99 COP")
        == f"{group} billones {group} millones {group} pesos con noventa y nueve centavos"
    )
    assert speak_cop("$1,00 COP y $2,00 COP") == "un peso y dos pesos"


@pytest.mark.parametrize(
    "text",
    [
        "V5 PAS01 SG768 Casa Ximena: cantidad 1, 11,074 m², 1.2345",
        "$5.513.429,14",
        "$5.513.429,14 USD",
        "COP 5,513,429.14",
        "5513429.14 COP",
        "$5.513.429,138 COP",
        "$55.13.429,14 COP",
        "$5.513.429,14 COPextra",
        "-$1,00 COP",
        "$1.000.000.000.000.000.000,00 COP",
    ],
)
def test_only_explicit_valid_colombian_money_pattern_changes(text):
    assert speak_cop(text) == text


def test_proper_names_and_technical_values_remain_unchanged():
    text = "Ítem V5, cantidad 1, 11,074 m², total $5.513.429,14 COP."
    visual, spoken = render_answer(text, {"V5"})
    assert visual == text and spoken.startswith("Ítem V5, cantidad 1, 11,074 m², total cinco")
    name = "Proyecto $21,00 COP"
    assert render_answer(name, {name}) == (name, name)


@pytest.mark.usefixtures("quotations")
@pytest.mark.parametrize("currency", [None, "USD", "cop", "EUR"])
def test_closed_currency_contract_rejects_missing_or_other(payloads, currency):
    payload = copy.deepcopy(payloads[f"/api/projects/{PROJECT_ID}/commercial-quotation/view"])
    if currency is None:
        payload.pop("currencyCode")
    else:
        payload["currencyCode"] = currency
    with pytest.raises(ValidationError):
        ConfirmedCommercialQuotation.model_validate_json(json.dumps(payload))


@pytest.mark.anyio
@pytest.mark.usefixtures("quotations")
@pytest.mark.parametrize("field", ["projectTotal", "unitPrice", "totalPrice"])
@pytest.mark.parametrize("value", ["5513429.138030602", "-0.01", "1000000000000000000.00"])
async def test_backend_precision_regressions_fail_visibly(payloads, mock_backend, field, value):
    payload = payloads[f"/api/projects/{PROJECT_ID}/commercial-quotation/view"]
    (payload if field == "projectTotal" else payload["items"][0])[field] = value
    model = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        call("get_commercial_quotation", {"project_id": PROJECT_ID}, "two"),
    )
    with pytest.raises(BackendResponseError):
        await run_agent(model, mock_backend[0])


@pytest.mark.anyio
@pytest.mark.usefixtures("quotations")
async def test_fixture_exact_projection_and_grounded_agent_voice(payloads, mock_backend):
    payload = payloads[f"/api/projects/{PROJECT_ID}/commercial-quotation/view"]
    payload["projectTotal"] = "98765432.17"
    payload["items"][0].update(unitPrice="5513429.14", totalPrice="48609963.18")
    parsed = ConfirmedCommercialQuotation.model_validate_json(json.dumps(payload))
    assert parsed.items[0].unit_price == Decimal("5513429.14")
    projection = quotation_data(UUID(PROJECT_ID), "commercial", parsed)
    assert projection["currencyCode"] == "COP" and projection["projectTotal"] == "98765432.17"
    assert projection["items"][0]["unitPrice"] == "5513429.14"
    assert projection["items"][0]["totalPrice"] == "48609963.18"
    assert projection["items"][0]["squareMeters"] == "7.125"
    answer = "El ítem V5 tiene un precio unitario de $5.513.429,14 COP."
    model = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        call("get_commercial_quotation", {"project_id": PROJECT_ID}, "two"),
        final(answer, [f"project:{PROJECT_ID}"]),
    )
    result, _ = await run_agent(model, mock_backend[0])
    assert result.answer == answer
    assert result.spoken_answer == speak_cop(answer)
    assert result.references[0].project_id == UUID(PROJECT_ID)
    assert "currencyCode" in repr(model.inputs[-1][0])


def test_prompt_currency_unit_total_and_portfolio_boundary():
    for rule in (
        "currencyCode=COP",
        "$5.513.429,14 COP",
        "mayor totalPrice",
        "mayor precio unitario",
        "exclusivamente projectTotal",
        "consultas N+1",
    ):
        assert rule in SYSTEM_PROMPT
    assert "La API no declara moneda" not in SYSTEM_PROMPT

import copy
import json

import pytest
from conftest import DATE, PROJECT_ID, TOKEN
from pydantic import ValidationError
from test_agent import FakeModel, call, final, run_agent

from app.agent.errors import InvalidToolCall
from app.contracts.quotation_rounds import QuotationRounds

pytestmark = pytest.mark.anyio
PATH = f"/api/projects/{PROJECT_ID}/quotation-rounds"


@pytest.fixture
def history(payloads):
    first = dict(
        roundNumber=1,
        prepareQuotationTask=None,
        budgetQuotation=None,
        reviewQuotationTask=None,
        commercialQuotation=dict(
            versionNumber=1, confirmedAt=DATE, projectTotal=100.0, currencyCode="COP"
        ),
        presentQuotationTask=None,
        clientDecision=dict(decision="ChangesRequested", note="Client comment", recordedAt=DATE),
        requotationRequest=None,
    )
    second = dict(
        first,
        roundNumber=2,
        commercialQuotation=None,
        clientDecision=None,
        requotationRequest=dict(
            targetQuotationRound=2, requestedChanges="Advisor changes", requestedAt=DATE
        ),
    )
    payloads[PATH] = dict(
        projectId=PROJECT_ID, total=2, returned=2, hasMore=False, rounds=[first, second]
    )
    return payloads[PATH]


async def test_fresh_history_is_read_only_and_preserves_attribution(history, mock_backend):
    transport, requests = mock_backend
    model = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        call("get_quotation_rounds", {"project_id": PROJECT_ID}, "rounds"),
        final(
            "The advisor opened round 2; commercial round 1 remains confirmed.",
            [f"project:{PROJECT_ID}"],
        ),
    )
    result, _ = await run_agent(model, transport)
    assert str(result.references[0].project_id) == PROJECT_ID
    assert requests[-1]["path"] == PATH
    assert all(r["method"] == "GET" for r in requests)
    messages = repr(model.inputs[-1][0])
    assert "Advisor changes" in messages and "Client comment" in messages
    assert TOKEN not in messages
    assert history["rounds"][1]["commercialQuotation"] is None


async def test_history_requires_fresh_resolved_project(mock_backend):
    transport, requests = mock_backend
    with pytest.raises(InvalidToolCall):
        await run_agent(
            FakeModel(call("get_quotation_rounds", {"project_id": PROJECT_ID})), transport
        )
    assert requests == []


@pytest.mark.parametrize(
    "mutation",
    ["duplicate", "order", "coverage", "version", "request", "extra", "currency", "naive"],
)
async def test_round_contract_rejects_inconsistent_history(history, mutation):
    value = copy.deepcopy(history)
    if mutation == "duplicate":
        value["rounds"][1] = value["rounds"][0]
    if mutation == "order":
        value["rounds"].reverse()
    if mutation == "coverage":
        value["total"] = 3
    if mutation == "version":
        value["rounds"][0]["commercialQuotation"]["versionNumber"] = 2
    if mutation == "request":
        value["rounds"][1]["requotationRequest"]["targetQuotationRound"] = 3
    if mutation == "extra":
        value["storageKey"] = "private"
    if mutation == "currency":
        value["rounds"][0]["commercialQuotation"]["currencyCode"] = "USD"
    if mutation == "naive":
        value["rounds"][1]["requotationRequest"]["requestedAt"] = "2026-10-28T12:00:00"
    with pytest.raises(ValidationError):
        QuotationRounds.model_validate_json(json.dumps(value), strict=True)


async def test_partial_history_reports_first_twenty_without_claiming_current(history):
    value = copy.deepcopy(history)
    base = dict(value["rounds"][1], requotationRequest=None)
    value.update(
        total=23,
        returned=20,
        hasMore=True,
        rounds=[dict(base, roundNumber=i) for i in range(1, 21)],
    )
    result = QuotationRounds.model_validate_json(json.dumps(value), strict=True)
    assert result.has_more and result.total == 23 and result.rounds[-1].round_number == 20

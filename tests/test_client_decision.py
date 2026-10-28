import json
from uuid import uuid4

import pytest
from conftest import DATE, PROJECT_ID, TASK_ID, TOKEN
from pydantic import SecretStr, ValidationError
from test_agent import FakeModel, call, final, run_agent

from app.agent.errors import InvalidToolCall
from app.agent.prompt import SYSTEM_PROMPT
from app.clients.sng_control import SnGControlClient
from app.clients.transport import create_http_client
from app.contracts.client_decision import ClientQuotationDecision
from app.core.errors import BackendResponseError, ManagementRequired, OutboundPolicyError
from app.tools import ReadTools

pytestmark = pytest.mark.anyio
PATH = f"/api/projects/{PROJECT_ID}/client-quotation-decision"


@pytest.fixture
def decision(payloads):
    value = dict(
        available=True,
        quotationRound=1,
        projectId=PROJECT_ID,
        commercialQuotationId=TASK_ID,
        presentQuotationTaskId=TASK_ID,
        decision="ChangesRequested",
        note=None,
        recordedAt=DATE,
    )
    payloads[PATH] = value
    return value


@pytest.mark.parametrize("outcome", ["Approved", "ChangesRequested", "Rejected", None])
async def test_official_read_and_grounded_reference(decision, outcome, mock_backend):
    if outcome is None:
        decision.update(
            available=False,
            commercialQuotationId=None,
            presentQuotationTaskId=None,
            decision=None,
            recordedAt=None,
        )
    else:
        decision["decision"] = outcome
    transport, requests = mock_backend
    model = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        call("get_client_quotation_decision", {"project_id": PROJECT_ID}, "decision_read"),
        final("Respuesta registrada oficialmente.", [f"project:{PROJECT_ID}"]),
    )
    result, _ = await run_agent(model, transport, "¿Qué respondió el cliente?")
    assert str(result.references[0].project_id) == PROJECT_ID
    assert [r["path"] for r in requests[-2:]] == ["/api/auth/me", PATH]
    messages = model.inputs[-1][0]
    assert TOKEN not in repr(messages)
    assert '"decision":' in repr(messages)
    assert "decision:" not in str(result.references)


async def test_unresolved_project_is_rejected_before_backend(mock_backend):
    transport, requests = mock_backend
    with pytest.raises(InvalidToolCall):
        await run_agent(
            FakeModel(call("get_client_quotation_decision", {"project_id": PROJECT_ID})), transport
        )
    assert requests == []


@pytest.mark.parametrize(
    "change",
    [
        {"decision": "Pending"},
        {"extra": "x"},
        {"recordedAt": "2026-10-28T12:00:00"},
        {"available": False},
        {"decision": None},
        {"note": "x" * 2001},
        {"projectId": "invalid"},
        {"commercialQuotationId": None},
    ],
)
async def test_contract_fails_closed(decision, change):
    with pytest.raises(ValidationError):
        ClientQuotationDecision.model_validate_json(json.dumps({**decision, **change}), strict=True)


async def test_wrong_project_and_auth_errors(decision, payloads, mock_backend):
    transport, requests = mock_backend
    async with create_http_client("https://control.test", 7, transport) as http:
        client = SnGControlClient(http, SecretStr(TOKEN), uuid4())
        decision["projectId"] = str(uuid4())
        with pytest.raises(BackendResponseError):
            await ReadTools(client).get_client_quotation_decision(PROJECT_ID)
        payloads["/api/auth/me"]["isManagement"] = False
        before = len(requests)
        with pytest.raises(ManagementRequired):
            await ReadTools(client).get_client_quotation_decision(PROJECT_ID)
        assert [r["path"] for r in requests[before:]] == ["/api/auth/me"]
        client.clear_credentials()


async def test_only_get_is_allowed(mock_backend):
    transport, requests = mock_backend
    async with create_http_client("https://control.test", 7, transport) as http:
        with pytest.raises(OutboundPolicyError):
            await http.post(PATH, json={"decision": "Approved"})
    assert requests == []


@pytest.mark.parametrize("mode", ["analysis", "recommendation", "scenario"])
async def test_decision_support_reads_fresh(decision, mock_backend, mode):
    transport, requests = mock_backend
    for _ in range(2):
        model = FakeModel(
            call("get_project_details", {"project_id": PROJECT_ID}),
            call("get_client_quotation_decision", {"project_id": PROJECT_ID}, "decision_read"),
            final("La respuesta está registrada; no implica recotización.", mode=mode),
        )
        await run_agent(model, transport)
    assert [r["path"] for r in requests].count(PATH) == 2


def test_prompt_distinguishes_explicit_evidence_and_downstream_limits():
    for rule in (
        "Never infer a client decision",
        "NOT payment",
        "does NOT close/archive",
        "Fresh reads remain mandatory",
        "untrusted data",
        "not proof of the exact",
    ):
        assert rule in SYSTEM_PROMPT

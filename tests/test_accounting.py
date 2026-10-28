import json
from decimal import Decimal
from uuid import uuid4

import pytest
from conftest import DATE, PROJECT_ID, TASK_ID, TOKEN
from pydantic import SecretStr, ValidationError
from test_agent import FakeModel, call, final, run_agent

from app.agent.errors import InvalidToolCall
from app.agent.prompt import SYSTEM_PROMPT
from app.clients.sng_control import SnGControlClient
from app.clients.transport import create_http_client
from app.contracts.accounting import AccountingState
from app.contracts.projects import ProjectTask
from app.core.errors import BackendResponseError, ManagementRequired, OutboundPolicyError
from app.tools import ReadTools

pytestmark = pytest.mark.anyio
PATH = f"/api/projects/{PROJECT_ID}/initial-deposit"


@pytest.fixture
def deposit(payloads):
    value = dict(
        projectId=PROJECT_ID,
        accountingHandoff=dict(
            available=True, quotationRound=2, commercialQuotationId=TASK_ID, sentAt=DATE
        ),
        deposit=dict(
            available=True,
            amount="5000000.01",
            currencyCode="COP",
            receivedAt="2026-10-16T06:00:00-05:00",
            recordedAt=DATE,
            reference="TEST-REF",
            note=None,
        ),
    )
    payloads[PATH] = value
    return value


@pytest.mark.parametrize("state", ["absent", "waiting", "recorded"])
async def test_official_evidence_remains_distinct_and_grounded(deposit, mock_backend, state):
    if state != "recorded":
        deposit["deposit"].update(
            available=False, amount=None, receivedAt=None, recordedAt=None, reference=None
        )
    if state == "absent":
        deposit["accountingHandoff"].update(
            available=False, quotationRound=None, commercialQuotationId=None, sentAt=None
        )
    transport, requests = mock_backend
    model = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        call("get_initial_deposit", {"project_id": PROJECT_ID}, "deposit"),
        final("El registro no demuestra pago total del proyecto.", [f"project:{PROJECT_ID}"]),
    )
    result, _ = await run_agent(model, transport, "¿Ya registraron el anticipo?")
    assert str(result.references[0].project_id) == PROJECT_ID
    assert [r["path"] for r in requests[-2:]] == ["/api/auth/me", PATH]
    assert TOKEN not in repr(model.inputs)
    content = json.loads(model.inputs[-1][0][-1].content)
    assert content["data"] == deposit
    parsed = AccountingState.model_validate_json(json.dumps(deposit), strict=True)
    assert parsed.deposit.amount == (Decimal("5000000.01") if state == "recorded" else None)


@pytest.mark.parametrize(
    "change",
    [
        dict(amount="0.00"),
        dict(amount="-1.00"),
        dict(amount="1.001"),
        dict(amount="1000000000000000000.00"),
        dict(currencyCode="USD"),
        dict(recordedAt="2026-10-16T12:00:00"),
        dict(receivedAt="2027-01-01T00:00:00Z"),
        dict(available=False),
        dict(reference="x" * 201),
        dict(note="x" * 2001),
        dict(extra=True),
    ],
)
def test_deposit_contract_fails_closed(deposit, change):
    deposit["deposit"].update(change)
    with pytest.raises(ValidationError):
        AccountingState.model_validate_json(json.dumps(deposit), strict=True)


async def test_resolved_project_required_and_no_write_paths(mock_backend):
    transport, requests = mock_backend
    with pytest.raises(InvalidToolCall):
        await run_agent(
            FakeModel(call("get_initial_deposit", {"project_id": PROJECT_ID})), transport
        )
    async with create_http_client("https://control.test", 7, transport) as http:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with pytest.raises(OutboundPolicyError):
                await http.request(method, PATH)
        with pytest.raises(OutboundPolicyError):
            await http.get(f"/api/projects/{PROJECT_ID}/send-approved-quotation-to-accounting")
    assert requests == []


async def test_wrong_project_and_management_revalidation(deposit, payloads, mock_backend):
    transport, requests = mock_backend
    async with create_http_client("https://control.test", 7, transport) as http:
        client = SnGControlClient(http, SecretStr(TOKEN), uuid4())
        tools = ReadTools(client)
        await tools.get_initial_deposit(PROJECT_ID)
        deposit["projectId"] = str(uuid4())
        with pytest.raises(BackendResponseError):
            await tools.get_initial_deposit(PROJECT_ID)
        payloads["/api/auth/me"]["isManagement"] = False
        before = len(requests)
        with pytest.raises(ManagementRequired):
            await tools.get_initial_deposit(PROJECT_ID)
        assert [r["path"] for r in requests[before:]] == ["/api/auth/me"]
        client.clear_credentials()


def test_accounting_task_no_round_or_sla(payloads):
    value = payloads[f"/api/projects/{PROJECT_ID}/tasks"][0]
    value.update(
        type="RegisterInitialDeposit",
        area="Accounting",
        quotationRound=None,
        status="Pending",
        leadTimeStatus="NotStarted",
    )
    assert ProjectTask.model_validate_json(json.dumps(value), strict=True).quotation_round is None
    for change in (
        dict(quotationRound=2),
        dict(area="Budgets"),
        dict(deadlineAt=DATE),
        dict(wasOverdue=False),
    ):
        with pytest.raises(ValidationError):
            ProjectTask.model_validate_json(json.dumps({**value, **change}), strict=True)


def test_semantic_boundaries_are_explicit():
    for rule in (
        "No required deposit percentage",
        "not full project payment",
        "never assert that the",
        "No global deposit aggregates",
        "conversation memory is not payment evidence",
        "No overdue inference",
    ):
        assert rule in SYSTEM_PROMPT

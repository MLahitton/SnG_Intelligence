"""Offline presentation contract tests; provider prose is a fixture, not a live LLM eval."""

import copy
import json
from uuid import UUID

import httpx
import openai
import pytest
from conftest import PROJECT_ID, TASK_ID
from test_agent import run_agent
from test_openai_model import KEY, function_call, output_message, response_body

from app.agent.openai_model import OpenAIModel
from app.agent.prompt import MANAGEMENT_LABELS_ES, SYSTEM_PROMPT


@pytest.mark.parametrize(
    "internal,label",
    [
        ("PrepareQuotation", "Preparar cotización"),
        ("ReviewQuotation", "Revisar cotización"),
        ("PresentQuotationToClient", "Presentar cotización al cliente"),
        ("Pending", "Pendiente"),
        ("InProgress", "En curso"),
        ("Completed", "Completada"),
        ("OnTrack", "En tiempo"),
        ("DueSoon", "Próxima a vencer"),
        ("DueToday", "Vence hoy"),
        ("Overdue", "Atrasada"),
        ("NotStarted", "Sin iniciar"),
        ("CompletedOnTime", "Completada a tiempo"),
        ("CompletedLate", "Completada tarde"),
        ("UntrackedLegacy", "Sin seguimiento histórico"),
    ],
)
def test_spanish_label_contract_is_available_to_model(internal, label):
    assert MANAGEMENT_LABELS_ES[internal] == label
    assert f"{internal} → {label}" in SYSTEM_PROMPT
    assert "sin exponer sus enums técnicos" in SYSTEM_PROMPT


def test_date_presentation_policy_includes_timezone_and_boundary_cases():
    assert "America/Bogota" in SYSTEM_PROMPT
    assert "2026-10-21T22:00:00Z" in SYSTEM_PROMPT
    assert "21 de octubre de 2026, 5:00 p. m. (hora de Colombia)" in SYSTEM_PROMPT
    assert "2026-10-22T02:00:00Z corresponde al 21 de octubre de 2026" in SYSTEM_PROMPT
    assert "9:00 p. m." in SYSTEM_PROMPT
    assert "no restes cinco horas otra vez" in " ".join(SYSTEM_PROMPT.split())
    assert "Una fecha sin hora conserva su día" in SYSTEM_PROMPT
    assert "no supongas UTC" in SYSTEM_PROMPT
    assert "Una fecha null no está disponible" in SYSTEM_PROMPT
    assert "no recalcules deadlines" in SYSTEM_PROMPT


def test_budget_context_does_not_infer_business_state_or_assignment_history():
    # These are contractual prompt assertions; no Leak Time calculation lives here.
    assert "PrepareQuotation con status Pending todavía no tiene Leak Time activo" in SYSTEM_PROMPT
    assert "Presupuestos comienza con la primera asignación del SG" in SYSTEM_PROMPT
    assert "Pending sin deadline no debe" in SYSTEM_PROMPT
    assert "pendiente de inicio/asignación" in " ".join(SYSTEM_PROMPT.split())
    assert "no inventes historia de asignación" in SYSTEM_PROMPT
    assert "sin derivar ni sobrescribir sus valores" in SYSTEM_PROMPT
    assert "expón la discrepancia" in SYSTEM_PROMPT


@pytest.mark.anyio
@pytest.mark.parametrize(
    "task_type,status,leak_status,deadline,prose",
    [
        (
            "PrepareQuotation",
            "InProgress",
            "OnTrack",
            "2026-10-21T22:00:00Z",
            "Preparar cotización: En tiempo. Plazo: 21 de octubre de 2026, "
            "5:00 p. m. (hora de Colombia).",
        ),
        (
            "ReviewQuotation",
            "InProgress",
            "DueSoon",
            "2026-10-22T02:00:00Z",
            "Revisar cotización: Próxima a vencer. Plazo: 21 de octubre de 2026, "
            "9:00 p. m. (hora de Colombia).",
        ),
        (
            "PrepareQuotation",
            "Pending",
            "NotStarted",
            None,
            "Preparar cotización: pendiente de inicio/asignación, sin plazo disponible. "
            "El seguimiento de tiempos aún no está activo.",
        ),
    ],
)
async def test_presentation_context_round_trip_preserves_evidence(
    mock_backend, payloads, task_type, status, leak_status, deadline, prose
):
    project_name = "Casa OnTrack — Peña"
    project_path = f"/api/projects/{PROJECT_ID}"
    payloads[project_path]["name"] = project_name
    task = payloads[f"{project_path}/tasks"][0]
    task.update(type=task_type, status=status, leakTimeStatus=leak_status, deadlineAt=deadline)
    original = copy.deepcopy(payloads)
    transport, _ = mock_backend
    requests = []
    evidence_refs = {}

    def provider(request):
        wire = json.loads(request.content)
        requests.append(wire)
        # Verify actual SDK instructions, not just constants unused by the adapter.
        assert wire["instructions"] == SYSTEM_PROMPT
        assert "español natural" in wire["instructions"]
        assert "No traduzcas ni alteres nombres" in wire["instructions"]
        assert "America/Bogota" in wire["instructions"]
        for value in (task_type, status, leak_status):
            assert f"{value} → {MANAGEMENT_LABELS_ES[value]}" in wire["instructions"]
        if len(requests) == 1:
            return httpx.Response(
                200,
                json=response_body(
                    [
                        function_call(
                            name="get_project_details",
                            arguments=json.dumps({"project_id": PROJECT_ID}),
                        )
                    ]
                ),
            )
        envelope = json.loads(wire["input"][-1]["output"])
        received_task = envelope["data"]["tasks"][0]
        assert received_task["type"] == task_type
        assert received_task["status"] == status
        assert received_task["leakTimeStatus"] == leak_status
        assert received_task["deadlineAt"] == deadline
        assert envelope["data"]["project"]["name"] == project_name
        evidence_refs.update(envelope["available_references"])
        return httpx.Response(
            200,
            json=response_body(
                [
                    output_message(
                        json.dumps(
                            {
                                "answer": f"{project_name}: {prose}",
                                "reference_keys": [f"project:{PROJECT_ID}", f"task:{TASK_ID}"],
                            },
                            ensure_ascii=False,
                        )
                    )
                ]
            ),
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http,
        openai.AsyncOpenAI(api_key=KEY, http_client=http, max_retries=0) as sdk,
    ):
        result, metrics = await run_agent(OpenAIModel(sdk, "test-model"), transport)
    assert len(requests) == 2
    assert metrics["tools_used"] == ["get_project_details"]
    assert payloads == original
    assert project_name in result.answer  # A proper name containing an enum stays intact.
    natural_text = result.answer.removeprefix(f"{project_name}: ")
    assert not any(value in natural_text for value in MANAGEMENT_LABELS_ES)
    if deadline is not None:
        assert deadline not in result.answer
        assert "hora de Colombia" in result.answer
    else:
        assert "pendiente de inicio/asignación" in result.answer
        assert "atrasada" not in natural_text and "fuera de plazo" not in natural_text
    assert result.references[0].project_id == UUID(PROJECT_ID)
    assert result.references[0].label == project_name
    assert result.references[1].task_id == UUID(TASK_ID)
    assert result.references[1].label == task_type  # Grounding labels remain authoritative.
    assert [r.model_dump(mode="json", by_alias=True) for r in result.references] == [
        evidence_refs[f"project:{PROJECT_ID}"],
        evidence_refs[f"task:{TASK_ID}"],
    ]
    assert json.loads(result.model_dump_json().encode("utf-8"))["answer"] == result.answer

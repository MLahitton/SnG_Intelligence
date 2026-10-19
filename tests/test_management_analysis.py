"""Offline scripted evals: protocol/grounding guarantees, not provider quality claims."""

import asyncio
import copy
import json
import logging
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import PROJECT_ID, TASK_ID, TOKEN
from pydantic import SecretStr, ValidationError
from test_agent import FakeModel, call, final, run_agent

from app.agent.contracts import ComparisonArguments, ModelAnswer, ModelTurn, ToolCall
from app.agent.core import Agent
from app.agent.errors import (
    InvalidModelResponse,
    InvalidToolCall,
    SensitiveContent,
    ToolResultTooLarge,
    UngroundedReference,
)
from app.agent.prompt import SYSTEM_PROMPT
from app.agent.security import CredentialGuard
from app.agent.toolbox import ToolBox
from app.clients.sng_control import SnGControlClient
from app.contracts.presentation import ComparisonPresentation
from app.conversation.context import ConversationContext, ConversationTurn
from app.core.errors import BackendResponseError, ManagementRequired
from app.core.latency import LatencyMetrics, latency_scope
from app.tools import ReadTools

pytestmark = pytest.mark.anyio


@asynccontextmanager
async def tools_with(transport):
    async with httpx.AsyncClient(transport=transport, base_url="https://control.test") as http:
        client = SnGControlClient(http, SecretStr(TOKEN), uuid4())
        try:
            yield ReadTools(client), client
        finally:
            client.clear_credentials()


def projects(payloads, number=2):
    """Synthetic current facts only; each project has a distinguishable task/deadline."""
    base = payloads[f"/api/projects/{PROJECT_ID}"]
    task = payloads[f"/api/projects/{PROJECT_ID}/tasks"][0]
    identifiers = []
    payloads["/api/projects"] = []
    for index in range(number):
        identifier = str(UUID(int=100 + index))
        identifiers.append(identifier)
        project = {**base, "id": identifier, "name": f"Proyecto {index + 1}"}
        payloads["/api/projects"].append(project)
        payloads[f"/api/projects/{identifier}"] = project
        payloads[f"/api/projects/{identifier}/tasks"] = [
            {
                **task,
                "id": str(UUID(int=200 + index)),
                "projectId": identifier,
                "type": "PrepareQuotation",
                "status": "Pending",
                "leakTimeStatus": "NotStarted",
                "deadlineAt": None if index == 0 else "2026-10-21T22:00:00Z",
            }
        ]
        payloads[f"/api/projects/{identifier}/documents"] = copy.deepcopy(
            payloads[f"/api/projects/{PROJECT_ID}/documents"]
        )
        payloads[f"/api/projects/{identifier}/notifications"] = []
    return identifiers


def comparison_turns(identifiers, *, docs=False, activity=False):
    return (
        ModelTurn(
            calls=tuple(
                ToolCall(f"find_{i}", "find_projects", json.dumps({"query": f"Proyecto {i + 1}"}))
                for i in range(len(identifiers))
            )
        ),
        call(
            "compare_projects",
            {
                "project_ids": identifiers,
                "include_documents": docs,
                "include_activity": activity,
            },
            "compare",
        ),
    )


async def test_snapshot_parallel_official_counts_order_bounds_and_projection(payloads):
    overview = payloads["/api/management/overview"]
    attention = overview["attention"]["items"][0]
    tasks = payloads["/api/management/tasks"]
    overview["attention"] = {
        "total": 25,
        "items": [
            {
                **attention,
                "taskId": str(UUID(int=1000 + i)),
                "leakTimeStatus": "DueSoon" if i == 0 else "Overdue",
            }
            for i in range(25)
        ],
    }
    payloads["/api/management/tasks"] = [
        {
            **tasks[0],
            "taskId": str(UUID(int=2000 + i)),
            "leakTimeStatus": "Overdue" if i == 19 else "NotStarted",
        }
        for i in range(20)
    ]
    started = set()
    both = asyncio.Event()

    async def backend(request):
        if request.url.path != "/api/auth/me":
            started.add(request.url.path)
            if len(started) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 1)
        return httpx.Response(200, json=payloads[request.url.path])

    latency = LatencyMetrics()
    async with tools_with(httpx.MockTransport(backend)) as (tools, _), asyncio.timeout(2):
        with latency_scope(latency):
            snapshot = await tools.get_management_snapshot()
    assert snapshot.overview.total_projects == 19
    assert snapshot.overview.tasks.pending == 3
    assert snapshot.overview.leak_time.overdue == 5
    assert snapshot.overview.attention.items[0].leak_time_status == "DueSoon"
    assert snapshot.tasks[0].leak_time_status == "Overdue"
    assert snapshot.attention_coverage.model_dump() == {
        "total": 25,
        "returned": 8,
        "has_more": True,
    }
    assert snapshot.tasks_coverage.model_dump() == {"total": 20, "returned": 12, "has_more": True}
    assert latency.values["management_snapshot_ms"] > 0
    assert "clientId" not in snapshot.model_dump_json(by_alias=True)
    assert len(overview["attention"]["items"]) == 25  # Original data unchanged.


@pytest.mark.parametrize("failure", [True, False])
@pytest.mark.parametrize("operation", ["snapshot", "comparison"])
async def test_composite_reads_cancel_all_siblings(payloads, failure, operation):
    identifiers = projects(payloads)
    active = set()
    started = asyncio.Event()
    reached = set()
    expected = 2 if operation == "snapshot" else 8

    async def backend(request):
        path = request.url.path
        if path == "/api/auth/me":
            return httpx.Response(200, json=payloads[path])
        active.add(path)
        reached.add(path)
        if len(reached) == expected:
            started.set()
        try:
            await started.wait()
            if failure and path == (
                "/api/management/overview"
                if operation == "snapshot"
                else f"/api/projects/{identifiers[0]}"
            ):
                return httpx.Response(500)
            await asyncio.Event().wait()
        finally:
            active.remove(path)

    async with tools_with(httpx.MockTransport(backend)) as (tools, _):
        operation_call = (
            tools.get_management_snapshot()
            if operation == "snapshot"
            else tools.compare_projects(
                [UUID(i) for i in identifiers],
                include_documents=True,
                include_activity=True,
            )
        )
        task = asyncio.create_task(operation_call)
        await asyncio.wait_for(started.wait(), 2)
        if not failure:
            task.cancel()
        with pytest.raises(BackendResponseError if failure else asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert not active


@pytest.mark.parametrize(
    "question,kind,keys",
    [
        (
            "Dame un resumen ejecutivo.",
            "management_brief",
            ["metric:total_projects", "metric:tasks.pending", "area:0"],
        ),
        (
            "¿Qué debería preocuparme hoy?",
            "management_brief",
            [f"task:{TASK_ID}", "metric:leak_time.overdue"],
        ),
        ("¿Qué área está más cargada?", "comparison", ["area:0"]),
        ("¿Tenemos un cuello de botella?", "management_brief", ["area:0"]),
    ],
)
async def test_scripted_evals_a_b_c_g_one_round(mock_backend, question, kind, keys):
    model = FakeModel(
        call("get_management_snapshot"),
        final(
            "Hay datos actuales de carga. No hay evidencia suficiente para afirmar un cuello "
            "de botella. Recomendaría revisar las tareas pendientes.",
            kind=kind,
            presentation=keys,
        ),
    )
    latency = LatencyMetrics()
    with latency_scope(latency):
        result, metrics = await run_agent(model, mock_backend[0], question)
    assert result.response_type == kind
    assert metrics["tools_used"] == ["get_management_snapshot"]
    assert latency.values["agent_tool_rounds"] == 1
    assert latency.values["agent_model_calls"] == 2
    assert latency.values["management_snapshot_ms"] > 0
    envelope = json.loads(model.inputs[-1][0][-1].content)
    assert envelope["data"]["attentionCoverage"]["total"] == 17
    assert envelope["complete"] is False
    if kind == "comparison":
        assert result.presentation.areas[0].pending == 3


async def test_brief_empty_state(payloads, mock_backend):
    overview = payloads["/api/management/overview"]
    overview["totalProjects"] = 0
    for group in ("tasks", "leakTime"):
        overview[group] = dict.fromkeys(overview[group], 0)
    overview["areas"] = []
    overview["attention"] = {"total": 0, "items": []}
    payloads["/api/management/tasks"] = []
    model = FakeModel(
        call("get_management_snapshot"),
        final("No hay proyectos.", kind="management_brief", presentation=["metric:total_projects"]),
    )
    result, _ = await run_agent(model, mock_backend[0])
    assert result.presentation.metrics[0].value == 0
    assert result.presentation.items == [] and result.references == []
    assert result.presentation.tasks_coverage.total == 0


async def test_attention_presentation_cannot_reverse_backend_order(payloads, mock_backend):
    attention = payloads["/api/management/overview"]["attention"]
    second = str(UUID(int=900))
    attention["items"].append({**attention["items"][0], "taskId": second})
    model = FakeModel(
        call("get_management_snapshot"),
        final(kind="management_brief", presentation=[f"task:{second}", f"task:{TASK_ID}"]),
    )
    result, _ = await run_agent(model, mock_backend[0])
    assert [str(t.task_id) for t in result.presentation.items] == [TASK_ID, second]


@pytest.mark.parametrize("number", [2, 4])
async def test_compare_parallel_current_facts_and_complete_ordered_references(payloads, number):
    identifiers = projects(payloads, number)
    started = set()
    ready = asyncio.Event()
    paths = []

    async def backend(request):
        path = request.url.path
        paths.append(path)
        if any(
            path == f"/api/projects/{i}{suffix}" for i in identifiers for suffix in ("", "/tasks")
        ):
            started.add(path)
            if len(started) == number * 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 2)
        return httpx.Response(200, json=payloads[path])

    model = FakeModel(
        *comparison_turns(identifiers),
        final(
            "Comparación actual.",
            kind="comparison",
            presentation=[f"project:{i}" for i in reversed(identifiers)],
        ),
    )
    latency = LatencyMetrics()
    with latency_scope(latency):
        result, _ = await run_agent(model, httpx.MockTransport(backend))
    assert isinstance(result.presentation, ComparisonPresentation)
    assert [str(r.project_id) for r in result.references] == identifiers
    assert [str(p.project.project_id) for p in result.presentation.projects] == identifiers
    first, second = result.presentation.projects[:2]
    assert next(m.value for m in first.metrics if m.key == "tasks.Pending") == 1
    assert first.tasks[0].task == "Preparar cotización"
    assert first.tasks[0].leak_time_status == "Sin iniciar"
    assert first.tasks[0].deadline_at is None
    assert second.tasks[0].deadline_at.isoformat() == "2026-10-21T22:00:00+00:00"
    assert not any(path.endswith(("/documents", "/notifications")) for path in paths)
    assert latency.values["comparison_fetch_ms"] > 0
    assert latency.values["agent_tool_rounds"] == 2
    assert latency.values["agent_model_calls"] == 3


@pytest.mark.parametrize("docs,activity", [(True, False), (False, True), (True, True)])
async def test_comparison_optional_reads_only_when_requested(
    payloads, mock_backend, docs, activity
):
    identifiers = projects(payloads)
    model = FakeModel(
        *comparison_turns(identifiers, docs=docs, activity=activity),
        final(kind="comparison", presentation=[f"project:{i}" for i in identifiers]),
    )
    result, _ = await run_agent(model, mock_backend[0])
    paths = [c["path"] for c in mock_backend[1]]
    assert sum(path.endswith("/documents") for path in paths) == (2 if docs else 0)
    assert sum(path.endswith("/notifications") for path in paths) == (2 if activity else 0)
    metrics = {m.key: m.value for m in result.presentation.projects[0].metrics}
    assert ("documents.returned" in metrics) is docs
    assert ("activity.returned" in metrics) is activity


@pytest.mark.parametrize(
    "ids",
    [
        [],
        [PROJECT_ID],
        [PROJECT_ID] * 2,
        [str(UUID(int=i)) for i in range(5)],
        ["https://evil.test", PROJECT_ID],
    ],
)
async def test_invalid_comparison_rejected_before_any_read(mock_backend, ids):
    with pytest.raises(InvalidToolCall):
        await run_agent(
            FakeModel(
                call(
                    "compare_projects",
                    {"project_ids": ids, "include_documents": False, "include_activity": False},
                )
            ),
            mock_backend[0],
        )
    assert not mock_backend[1]


async def test_fabricated_ids_and_whole_batch_rejected(mock_backend):
    batch = ModelTurn(
        calls=(
            ToolCall("safe", "get_management_snapshot", "{}"),
            ToolCall(
                "bad",
                "compare_projects",
                json.dumps(
                    {
                        "project_ids": [PROJECT_ID, str(UUID(int=400))],
                        "include_documents": False,
                        "include_activity": False,
                    }
                ),
            ),
        )
    )
    with pytest.raises(InvalidToolCall):
        await run_agent(FakeModel(batch), mock_backend[0])
    assert not mock_backend[1]


async def test_ambiguous_candidates_cannot_be_compared(payloads, mock_backend):
    identifiers = projects(payloads)
    model = FakeModel(
        call("find_projects", {"query": "Proyecto"}), comparison_turns(identifiers)[1]
    )
    with pytest.raises(InvalidToolCall):
        await run_agent(model, mock_backend[0])
    assert not any(c["path"] == f"/api/projects/{identifiers[0]}" for c in mock_backend[1])


@pytest.mark.parametrize(
    "key", ["metric:riskScore", "metric:billing", "project:ffffffff-ffff-ffff-ffff-ffffffffffff"]
)
async def test_fabricated_presentation_facts_rejected(mock_backend, key):
    with pytest.raises(UngroundedReference):
        await run_agent(
            FakeModel(
                call("get_management_snapshot"), final(kind="management_brief", presentation=[key])
            ),
            mock_backend[0],
        )


async def test_model_cannot_supply_own_metrics():
    with pytest.raises(ValidationError):
        ModelAnswer.model_validate_json(
            json.dumps(
                {
                    "responseType": "management_brief",
                    "answer": "x",
                    "spokenAnswer": "x",
                    "reference_keys": [],
                    "presentation_keys": [],
                    "metrics": [{"value": 99}],
                }
            )
        )
    schema = ComparisonArguments.model_json_schema()
    assert set(schema["required"]) == {"project_ids", "include_documents", "include_activity"}
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize(
    "question", ["¿Cuánto facturamos este mes?", "¿Cuál es el margen?", "¿Qué cambió desde ayer?"]
)
async def test_scripted_eval_h_and_unavailable_limits(mock_backend, question):
    result, _ = await run_agent(
        FakeModel(
            final(
                "No dispongo de ese dato; solo puedo consultar el estado operativo actual.",
                kind="unavailable",
            )
        ),
        mock_backend[0],
        question,
    )
    assert result.response_type == "unavailable" and not result.references
    assert not mock_backend[1]


async def test_snapshot_security_projection_and_safe_telemetry(mock_backend, caplog):
    caplog.set_level(logging.INFO, logger="sng_intelligence")
    model = FakeModel(call("get_management_snapshot"), final(kind="management_brief"))
    result, _ = await run_agent(model, mock_backend[0])
    text = repr(model.inputs) + result.model_dump_json()
    assert TOKEN not in text and "fake-openai-key" not in text
    assert all(
        field not in text for field in ("clientId", "assignedToGoogleSubject", "Authorization")
    )
    assert all(
        value not in caplog.text
        for value in (TOKEN, PROJECT_ID, TASK_ID, "Proyecto", "Consulta de Gerencia")
    )


async def test_sensitive_backend_text_cannot_enter_model(payloads, mock_backend):
    payloads["/api/management/tasks"][0]["projectName"] = TOKEN
    model = FakeModel(call("get_management_snapshot"))
    with pytest.raises(SensitiveContent):
        await run_agent(model, mock_backend[0])
    assert len(model.inputs) == 1


async def test_live_authorization_scope_allows_composite_reads_but_revalidates_each_run(
    payloads, mock_backend
):
    identifiers = projects(payloads)
    async with tools_with(mock_backend[0]) as (_, client):
        for _ in range(2):
            async with ReadTools._authorized_run(client, "manager") as tools:
                await tools.get_management_snapshot()
                await tools.compare_projects(
                    [UUID(i) for i in identifiers], include_documents=False, include_activity=False
                )
        payloads["/api/auth/me"]["isManagement"] = False
        with pytest.raises(ManagementRequired):
            async with ReadTools._authorized_run(client, "manager"):
                pytest.fail("Revoked identity must not run tools")
    assert sum(c["path"] == "/api/auth/me" for c in mock_backend[1]) == 3


async def test_scripted_evals_d_e_f_cross_modal_fresh_reads_and_ordinals(payloads, mock_backend):
    identifiers = projects(payloads)
    context = ConversationContext()
    guard = CredentialGuard(SecretStr(TOKEN), SecretStr("fake-openai-key"))
    for modality, question in [
        ("text", "Compara Proyecto 1 con Proyecto 2"),
        ("text", "¿Cuál tiene más pendientes?"),
        ("voice", "¿Y cuál vence primero?"),
        ("text", "Amplía el segundo proyecto"),
    ]:
        if "Amplía" in question:
            model = FakeModel(
                call("get_project_details", {"project_id": identifiers[1]}),
                final(
                    "Datos actuales.",
                    kind="project_status",
                    presentation=[f"project:{identifiers[1]}"],
                ),
            )
        else:
            model = FakeModel(
                *comparison_turns(identifiers),
                final(
                    "Proyecto 1 y Proyecto 2 tienen una tarea pendiente cada uno; "
                    "solo Proyecto 2 tiene fecha conocida: 2026-10-21T22:00:00Z.",
                    kind="comparison",
                    presentation=[f"project:{i}" for i in identifiers],
                ),
            )
        async with tools_with(mock_backend[0]) as (tools, _):
            result = await Agent(model, ToolBox(tools, guard), guard, {}).run(question, context)
        if context.turns:
            history = repr(model.inputs[0][0])
            assert history.index(
                f'"projectId": "{identifiers[0]}"'.replace(": ", ":")
            ) < history.index(f'"projectId": "{identifiers[1]}"'.replace(": ", ":"))
        if modality == "voice":
            assert "cinco de la tarde" in result.spoken_answer
            assert "T22:00" not in result.spoken_answer
        context = ConversationContext(
            (*context.turns, ConversationTurn.completed(modality, question, result))
        )
    paths = [c["path"] for c in mock_backend[1]]
    assert paths.count(f"/api/projects/{identifiers[0]}") == 3
    assert paths.count(f"/api/projects/{identifiers[1]}") == 4
    # Old comparison references never authorize skipping fresh evidence.
    async with tools_with(mock_backend[0]) as (tools, _):
        with pytest.raises(InvalidModelResponse):
            await Agent(
                FakeModel(final("Todavía tiene una tarea.")), ToolBox(tools, guard), guard, {}
            ).run("¿Y ahora?", context)
        with pytest.raises(InvalidToolCall):
            await Agent(
                FakeModel(comparison_turns(identifiers)[1]), ToolBox(tools, guard), guard, {}
            ).run("Compara otra vez", context)


def test_management_policy_covers_unavailable_analysis_and_authority():
    for rule in (
        "get_management_snapshot",
        "2 a 4 UUID",
        "nunca infieras subidas",
        "no capacidad humana",
        "solo una hipótesis prudente",
        "no implica bloqueado ni atrasado",
        "2–4 frases",
        "hasMore",
        "include_documents e include_activity son false",
    ):
        assert rule in SYSTEM_PROMPT


async def test_http_agent_exports_numeric_analysis_timings(mock_backend):
    _, metrics = await run_agent(
        FakeModel(call("get_management_snapshot"), final()), mock_backend[0]
    )
    assert metrics["management_snapshot_ms"] >= 0
    assert metrics["agent_total_ms"] > 0
    assert metrics["agent_model_calls"] == 2
    assert metrics["agent_tool_rounds"] == 1


async def test_oversized_snapshot_fails_instead_of_silent_truncation(payloads, mock_backend):
    payloads["/api/management/overview"]["attention"]["items"][0]["projectName"] = "X" * 50000
    model = FakeModel(call("get_management_snapshot"))
    with pytest.raises(ToolResultTooLarge):
        await run_agent(model, mock_backend[0])
    assert len(model.inputs) == 1


@pytest.mark.parametrize("association", ["project", "task", "activity"])
async def test_comparison_rejects_mismatched_backend_associations(
    payloads, mock_backend, association
):
    identifiers = projects(payloads)
    if association == "project":
        # Copy to avoid mutating the search result: provenance lookup remains valid.
        payloads[f"/api/projects/{identifiers[0]}"] = {
            **payloads[f"/api/projects/{identifiers[0]}"],
            "id": PROJECT_ID,
        }
    elif association == "task":
        payloads[f"/api/projects/{identifiers[0]}/tasks"][0]["projectId"] = PROJECT_ID
    else:
        payloads[f"/api/projects/{identifiers[0]}/notifications"] = payloads[
            f"/api/projects/{PROJECT_ID}/notifications"
        ]
    model = FakeModel(*comparison_turns(identifiers, activity=association == "activity"))
    with pytest.raises(UngroundedReference):
        await run_agent(model, mock_backend[0])


async def test_comparison_preserves_official_overdue_even_with_future_deadline(
    payloads, mock_backend
):
    identifiers = projects(payloads)
    task = payloads[f"/api/projects/{identifiers[1]}/tasks"][0]
    task["deadlineAt"] = "2099-10-21T22:00:00Z"
    task["leakTimeStatus"] = "Overdue"
    model = FakeModel(
        *comparison_turns(identifiers),
        final(
            kind="comparison",
            presentation=[f"project:{identifier}" for identifier in identifiers],
        ),
    )
    result, _ = await run_agent(model, mock_backend[0])
    compared = result.presentation.projects[1]
    assert compared.tasks[0].leak_time_status == "Atrasada"
    assert compared.tasks[0].deadline_at.year == 2099
    assert next(m.value for m in compared.metrics if m.key == "deadlines.Overdue") == 1

"""Scripted protocol/grounding checks, not proof of real-model recommendation quality."""

import json
from typing import get_args
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import CLIENT_ID, PROJECT_ID, TOKEN
from conversation_repository import MemoryRepository
from pydantic import SecretStr
from test_agent import FakeModel, call, final, run_agent
from test_conversation import turn
from test_live import SESSION
from test_management_analysis import comparison_turns, projects
from test_memory import REFERENCE, draft
from test_openai_model import invoke, output_message, response_body
from test_semantic_response import semantic_payload
from test_sideband import begin, complete, function, setup_live, until

from app.agent.contracts import AnswerMode, ModelAnswer
from app.agent.core import MAX_EXECUTION_SECONDS, MAX_TOOL_CALLS, MAX_TOOL_ROUNDS, Agent
from app.agent.errors import InvalidModelResponse
from app.agent.prompt import SYSTEM_PROMPT
from app.agent.security import CredentialGuard
from app.agent.toolbox import ToolBox
from app.clients.sng_control import SnGControlClient
from app.clients.transport import create_http_client
from app.conversation.context import ConversationContext
from app.conversation.memory import assemble_memory
from app.conversation.store import ConversationStore
from app.tools import ReadTools

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("mode", get_args(AnswerMode))
async def test_modes_survive_real_sdk_output_parser(mode):
    payload = semantic_payload(answerMode=mode)
    result = await invoke(
        lambda _: httpx.Response(200, json=response_body([output_message(json.dumps(payload))]))
    )
    assert result.answer.answer_mode == mode
    schema = ModelAnswer.model_json_schema()
    assert "answerMode" in schema["required"]
    assert set(schema["properties"]["answerMode"]["enum"]) == set(get_args(AnswerMode))


@pytest.mark.parametrize("mode", ["unknown", "Analysis", None, False, {}])
async def test_sdk_rejects_unknown_modes(mode):
    with pytest.raises(InvalidModelResponse):
        await invoke(
            lambda _: httpx.Response(
                200,
                json=response_body([output_message(json.dumps(semantic_payload(answerMode=mode)))]),
            )
        )


async def with_context(model, transport, context, question="¿Qué revisarías ahora?"):
    guard = CredentialGuard(SecretStr(TOKEN))
    async with create_http_client("https://control.test", 7, transport) as http:
        client = SnGControlClient(http, SecretStr(TOKEN), uuid4())
        try:
            return await Agent(model, ToolBox(ReadTools(client), guard), guard, {}).run(
                question, context
            )
        finally:
            client.clear_credentials()


def old_context():
    return ConversationContext(
        memory=assemble_memory(
            draft(
                userDecisions=("Priorizar plazos.",),
                userAssumptions=("Producción podría demorarse dos días.",),
                priorProposals=("Antes se propuso priorizar Casa Ximena.",),
            ),
            (REFERENCE,),
        )
    )


@pytest.mark.parametrize("mode", ["factual", "analysis", "recommendation", "scenario"])
@pytest.mark.parametrize("context", [None, ConversationContext(), old_context()])
async def test_operational_modes_require_fresh_reads_even_with_old_proposal(
    mock_backend, mode, context
):
    model = FakeModel(final("Priorizaría Casa Ximena; sigue atrasada.", mode=mode))
    with pytest.raises(InvalidModelResponse):
        await with_context(model, mock_backend[0], context)
    assert not mock_backend[1]
    assert len(model.inputs) == 1  # No metadata repair can bypass the read guard.


@pytest.mark.parametrize("mode", ["analysis", "recommendation", "scenario"])
async def test_fallback_response_type_cannot_bypass_operational_mode(mock_backend, mode):
    with pytest.raises(InvalidModelResponse):
        await run_agent(FakeModel(final(mode=mode, kind="unavailable")), mock_backend[0])


@pytest.mark.parametrize(
    "mode,question,answer",
    [
        ("brainstorm", "Ideas generales para reuniones", "Una opción sería definir una agenda."),
        ("conversation", "Gracias", "Con gusto."),
    ],
)
async def test_generic_modes_without_reads_even_after_history(mock_backend, mode, question, answer):
    result = await with_context(
        FakeModel(final(answer, mode=mode)), mock_backend[0], old_context(), question
    )
    assert result.answer == answer and not result.references and not mock_backend[1]
    assert "answerMode" not in result.model_dump_json(by_alias=True)


@pytest.mark.parametrize("mode", ["brainstorm", "conversation"])
@pytest.mark.parametrize(
    "fields",
    [
        {"keys": [f"project:{PROJECT_ID}"]},
        {"presentation": ["metric:total_projects"]},
        {"kind": "management_brief"},
    ],
)
async def test_generic_modes_cannot_claim_evidence_without_reads(mock_backend, mode, fields):
    with pytest.raises(InvalidModelResponse):
        await with_context(FakeModel(final(mode=mode, **fields)), mock_backend[0], old_context())


async def test_historical_recall_remains_factual_fallback_only(mock_backend):
    with pytest.raises(InvalidModelResponse):
        await with_context(
            FakeModel(final("Te propuse priorizar Ximena.")), mock_backend[0], old_context()
        )
    result = await with_context(
        FakeModel(final("No puedo verificar esa recomendación anterior.", kind="unavailable")),
        mock_backend[0],
        old_context(),
    )
    assert result.response_type == "unavailable" and not result.references


async def test_business_brainstorm_uses_fresh_tasks_not_historical_status(mock_backend):
    model = FakeModel(
        call("list_management_tasks"),
        final(
            "Una alternativa sería revisar las tareas actuales antes de cambiar el proceso.",
            [f"project:{PROJECT_ID}"],
            mode="brainstorm",
        ),
    )
    result = await with_context(model, mock_backend[0], old_context())
    assert result.references[0].project_id == UUID(PROJECT_ID)
    assert any(r["path"] == "/api/management/tasks" for r in mock_backend[1])


async def test_snapshot_recommendation_has_current_references_and_cautious_language(mock_backend):
    answer = (
        "Recomendaría revisar las tareas atrasadas. Mi lectura es que la concentración merece "
        "atención, pero no demuestra un cuello de botella ni mide capacidad humana."
    )
    model = FakeModel(
        call("get_management_snapshot"),
        final(
            answer,
            [f"project:{PROJECT_ID}"],
            mode="recommendation",
            kind="management_brief",
            presentation=["metric:lead_time.overdue", "area:0"],
        ),
    )
    result, metrics = await run_agent(model, mock_backend[0], "¿Qué harías hoy?")
    assert metrics["tools_used"] == ["get_management_snapshot"]
    assert result.references[0].project_id == UUID(PROJECT_ID)
    assert "no demuestra" in result.answer
    envelope = json.loads(model.inputs[-1][0][-1].content)
    assert envelope["data"]["overview"]["leadTime"]["overdue"] == 5
    assert set(r.model_dump(mode="json")["project_id"] for r in result.references) == {PROJECT_ID}


async def test_priority_revision_uses_new_comparison_and_memory_objective(payloads, mock_backend):
    ids = projects(payloads)
    context = old_context()
    for number in range(2):
        first, second = ids[number], ids[1 - number]
        payloads[f"/api/projects/{first}/tasks"][0]["leadTimeStatus"] = "Overdue"
        payloads[f"/api/projects/{second}/tasks"][0]["leadTimeStatus"] = "OnTrack"
        answer = (
            f"Yo priorizaría Proyecto {number + 1} por su tarea atrasada, "
            "siguiendo tu objetivo de plazo."
        )
        model = FakeModel(
            *comparison_turns(ids),
            final(
                answer,
                mode="recommendation",
                kind="comparison",
                presentation=[f"project:{i}" for i in ids],
            ),
        )
        result = await with_context(model, mock_backend[0], context)
        assert result.answer == answer
        assert [str(r.project_id) for r in result.references] == ids
        assert "Priorizar plazos" in repr(model.inputs[0][0])
        assert "Overdue" in model.inputs[-1][0][-1].content
        context = ConversationContext(
            memory=context.memory, turns=(turn("¿Lo mantienes?", answer),)
        )
    assert sum(r["path"] == f"/api/projects/{ids[0]}/tasks" for r in mock_backend[1]) == 2


@pytest.mark.parametrize("changed", ["answer", "mode"])
async def test_repair_cannot_change_recommendation_or_mode(mock_backend, changed):
    answer = "Recomendaría revisar las tareas actuales."
    model = FakeModel(
        call("get_management_snapshot"),
        final(answer, [f"project:{CLIENT_ID}"], mode="recommendation"),
        final(
            answer if changed == "mode" else "Recomendaría otra cosa.",
            mode="conversation" if changed == "mode" else "recommendation",
        ),
    )
    with pytest.raises(InvalidModelResponse):
        await run_agent(model, mock_backend[0])
    assert model.inputs[-1][1] == ()


async def test_live_three_decision_turns_keep_session_and_refresh_evidence(mock_backend):
    steps = [
        (
            "analysis",
            "¿Qué te preocupa?",
            "Mi lectura es que las tareas atrasadas merecen atención.",
        ),
        ("recommendation", "¿Qué harías?", "Recomendaría revisar las tareas atrasadas primero."),
        (
            "scenario",
            "¿Y si Producción se demora dos días?",
            "Bajo ese supuesto podría afectar otras etapas, "
            "pero no hay dependencias para cuantificarlo.",
        ),
    ]
    scripted = []
    for mode, _, answer in steps:
        scripted.extend(
            [call("get_management_snapshot"), final(answer, [f"project:{PROJECT_ID}"], mode=mode)]
        )
    model = FakeModel(*scripted)
    store = ConversationStore(MemoryRepository())
    key = (await store.create("manager")).conversation_id
    try:
        async with setup_live(mock_backend[0], model) as (manager, socket, vault, registry, _):
            manager._conversations = store
            await store.bind_live(SESSION, key, "manager")
            for number, (_, question, _) in enumerate(steps, 1):
                begin(socket, f"response_{number}")
                socket.incoming.put_nowait(function(f"call_{number}", question))
                complete(socket, f"response_{number}")
                await until(
                    lambda n=number: sum(s["type"] == "response.create" for s in socket.sent) == n
                )
                assert not socket.closed and registry.owned(SESSION, "manager")
                assert vault._get(SESSION).subject == "manager"
                assert len(store.repository.turns[key]) == number
            assert sum(r["path"] == "/api/management/overview" for r in mock_backend[1]) == 3
            assert sum(r["path"] == "/api/auth/me" for r in mock_backend[1]) == 3
            assert "answerMode" not in repr(socket.sent)
            assert "answer_mode" not in repr(store.repository.turns[key])
    finally:
        await store.shutdown()


def test_prompt_constraints_and_unchanged_budgets():
    assert (MAX_TOOL_ROUNDS, MAX_TOOL_CALLS, MAX_EXECUTION_SECONDS) == (6, 8, 120)
    for rule in (
        "Decision Support V1",
        "riskScore",
        "health score",
        "probability",
        "percentage likelihood",
        "no productividad",
        "Logística +3",
        "revocación",
        "2–4",
        "recall histórico",
        "N+1",
    ):
        assert rule in SYSTEM_PROMPT

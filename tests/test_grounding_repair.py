import asyncio
import json
import logging
from uuid import UUID

import httpx
import openai
import pytest
from conftest import CLIENT_ID, PROJECT_ID, TOKEN
from test_agent import FakeModel, call, final, run_agent
from test_live import KEY, SESSION
from test_openai_model import output_message, response_body
from test_sideband import begin, complete, function, setup_live, until

from app.agent import core
from app.agent.contracts import ModelMessage, ModelTurn
from app.agent.errors import (
    AgentLimitExceeded,
    InvalidModelResponse,
    InvalidToolCall,
    SensitiveContent,
    UngroundedReference,
)
from app.agent.openai_model import OpenAIModel
from app.agent.prompt import GROUNDING_REPAIR_INSTRUCTION
from app.conversation.store import ConversationStore
from app.live.errors import LiveSessionUnavailable

pytestmark = pytest.mark.anyio
FOREIGN = f"project:{CLIENT_ID}"
CURRENT = f"project:{PROJECT_ID}"


@pytest.mark.parametrize("keys", [[CURRENT], []])
async def test_one_repair_preserves_prose_uses_current_evidence_and_logs_counts(
    mock_backend, caplog, keys
):
    caplog.set_level(logging.INFO, logger="sng_intelligence")
    model = FakeModel(call("get_management_overview"), final(keys=[FOREIGN]), final(keys=keys))
    result, metrics = await run_agent(model, mock_backend[0])
    assert [r.project_id for r in result.references] == ([UUID(PROJECT_ID)] if keys else [])
    assert result.answer == result.spoken_answer == "Respuesta basada en datos."
    assert metrics["agent_model_calls"] == 3
    assert metrics["agent_tool_rounds"] == metrics["tool_calls"] == 1
    messages, tools = model.inputs[-1]
    assert tools == ()
    assert messages[-1] == ModelMessage(role="developer", content=GROUNDING_REPAIR_INSTRUCTION)
    assert messages[-2].role == "assistant"
    assert json.loads(messages[-2].content)["reference_keys"] == [FOREIGN]
    rows = [json.loads(r.message) for r in caplog.records if r.message.startswith("{")]
    repairs = [r for r in rows if "agent_event" in r]
    assert [r["agent_event"] for r in repairs] == [
        "grounding_repair_started",
        "grounding_repair_succeeded",
    ]
    assert all(r["model_call_number"] == 3 and r["available_reference_count"] == 2 for r in repairs)
    for sensitive in (PROJECT_ID, CLIENT_ID, TOKEN, result.answer):
        assert sensitive not in caplog.text


async def test_presentation_repaired_from_same_evidence(mock_backend):
    model = FakeModel(
        call("get_management_overview"),
        final(presentation=["metric:invented"]),
        final(presentation=["metric:total_projects"]),
    )
    result, _ = await run_agent(model, mock_backend[0])
    assert result.presentation.metric.value == 19
    assert len(model.inputs) == 3


@pytest.mark.parametrize(
    "repair,error",
    [
        (final(keys=["project:another-invented-key"]), UngroundedReference),
        (final(presentation=["metric:invented"]), UngroundedReference),
        (final(kind="project_status", presentation=[]), UngroundedReference),
        (final(spoken="New spoken facts"), InvalidModelResponse),
        (final("Changed facts"), InvalidModelResponse),
        (call("list_management_tasks"), InvalidModelResponse),
        (ModelTurn(), InvalidModelResponse),
        (final(keys=[TOKEN]), SensitiveContent),
    ],
)
async def test_failed_repair_never_loops_or_executes_tools(mock_backend, caplog, repair, error):
    caplog.set_level(logging.INFO, logger="sng_intelligence")
    model = FakeModel(call("get_management_overview"), final(keys=[FOREIGN]), repair)
    with pytest.raises(error):
        await run_agent(model, mock_backend[0])
    assert len(model.inputs) == 3 and not model.turns
    assert not any(r["path"] == "/api/management/tasks" for r in mock_backend[1])
    assert "grounding_repair_failed" in caplog.text
    assert "grounding_repair_succeeded" not in caplog.text
    assert TOKEN not in caplog.text and CLIENT_ID not in caplog.text


@pytest.mark.parametrize("stage", ["input", "output"])
async def test_repair_respects_transcript_budget(mock_backend, monkeypatch, stage):
    class BudgetModel(FakeModel):
        async def respond(self, messages, tools):
            result = await super().respond(messages, tools)
            if (stage == "input" and len(self.inputs) == 2) or not tools:
                monkeypatch.setattr(core, "MAX_TRANSCRIPT_BYTES", 1)
            return result

    model = BudgetModel(call("get_management_overview"), final(keys=[FOREIGN]), final())
    with pytest.raises(AgentLimitExceeded):
        await run_agent(model, mock_backend[0])
    assert len(model.inputs) == (2 if stage == "input" else 3)


async def test_repair_shares_original_execution_timeout(mock_backend, monkeypatch):
    class WaitingRepair(FakeModel):
        async def respond(self, messages, tools):
            if not tools:
                await asyncio.Event().wait()
            return await super().respond(messages, tools)

    monkeypatch.setattr(core, "MAX_EXECUTION_SECONDS", 0.1)
    with pytest.raises(AgentLimitExceeded):
        await run_agent(
            WaitingRepair(call("get_management_overview"), final(keys=[FOREIGN])), mock_backend[0]
        )


async def test_repair_at_last_tool_round_cannot_open_another_round(mock_backend, monkeypatch):
    monkeypatch.setattr(core, "MAX_TOOL_ROUNDS", 1)
    monkeypatch.setattr(core, "MAX_TOOL_CALLS", 1)
    model = FakeModel(call("get_management_overview"), final(keys=[FOREIGN]), final())
    _, metrics = await run_agent(model, mock_backend[0])
    assert metrics["agent_model_calls"] == 3
    assert metrics["agent_tool_rounds"] == metrics["tool_calls"] == 1


async def test_official_sdk_final_only_request():
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert body["tools"] == [] and body["tool_choice"] == "none"
        assert body["store"] is False and body["text"]["format"]["strict"] is True
        assert "previous_response_id" not in body and "conversation" not in body
        assert TOKEN not in request.content.decode() and KEY not in request.content.decode()
        assert body["input"][-1]["role"] == "developer"
        return httpx.Response(200, json=response_body([output_message()]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with openai.AsyncOpenAI(api_key=KEY, http_client=http, max_retries=0) as sdk:
            result = await OpenAIModel(sdk, "test-model").respond(
                (ModelMessage(role="developer", content=GROUNDING_REPAIR_INSTRUCTION),), ()
            )
    assert result.answer.answer == "Hola"


@pytest.mark.parametrize("repair_succeeds", [True, False])
async def test_four_live_turns_repair_history_reference_and_keep_session(
    mock_backend, payloads, caplog, repair_succeeds
):
    caplog.set_level(logging.INFO, logger="sng_intelligence")
    payloads[f"/api/projects/{PROJECT_ID}"]["name"] = "Casa Ximena"
    # Global results refer to a different project: the previous project is NOT fresh evidence.
    for item in payloads["/api/management/overview"]["attention"]["items"]:
        item.update(projectId=CLIENT_ID, projectName="Otro proyecto")
    for item in payloads["/api/management/tasks"]:
        item.update(projectId=CLIENT_ID, projectName="Otro proyecto")
    model = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        final("Casa Ximena: pendiente.", keys=[CURRENT]),
        call("get_project_details", {"project_id": PROJECT_ID}),
        final("Casa Ximena: pendiente.", keys=[CURRENT]),
        call("get_management_overview"),
        call("list_management_tasks", identifier="call_2"),
        final("Hay 19 proyectos.", keys=[CURRENT]),
        final("Hay 19 proyectos.", keys=[] if repair_succeeds else [CURRENT]),
        call("get_management_overview"),
        final("Hay 19 proyectos."),
    )
    questions = [
        "¿Cómo va Casa Ximena?",
        "¿Qué sigue?",
        "¿Tenemos más proyectos, alguno que esté atrasado?",
        "¿Cuántos proyectos hay?",
    ]
    store = ConversationStore()
    entry = store.create("manager")
    try:
        async with setup_live(mock_backend[0], model) as (manager, socket, vault, registry, _):
            manager._conversations = store
            store.bind_live(SESSION, entry.conversation_id, "manager")
            for number, question in enumerate(questions, 1):
                begin(socket, f"response_{number}")
                socket.incoming.put_nowait(function(f"call_{number}", question))
                complete(socket, f"response_{number}")
                await until(
                    lambda expected=number: (
                        sum(s["type"] == "response.create" for s in socket.sent) == expected
                    )
                )
                assert registry.owned(SESSION, "manager") is manager._runs[SESSION].entry
                assert vault._get(SESSION).subject == "manager"
                assert not socket.closed
                assert store.conversation_for_live(SESSION) == entry.conversation_id
                assert len(entry.turns) == number
            assert len(model.inputs) == 10
            assert "Casa Ximena" in repr(model.inputs[2][0])
            assert json.loads(model.inputs[7][0][-2].content)["reference_keys"] == [CURRENT]
            assert model.inputs[7][1] == ()
            assert json.loads(entry.turns[1].references)[0]["projectId"] == PROJECT_ID
            assert entry.turns[2].references == "[]"
            outputs = [
                json.loads(s["item"]["output"])
                for s in socket.sent
                if s["type"] == "response.item.create"
            ]
            if repair_succeeds:
                assert outputs[2]["spokenAnswer"] == "Hay 19 proyectos."
                assert "turn_recovered" not in caplog.text
            else:
                assert outputs[2] == {
                    "responseType": "unavailable",
                    "spokenAnswer": "No pude verificar esa consulta en este momento.",
                }
                assert entry.turns[2].response_type == "unavailable"
                assert entry.turns[2].assistant_content == outputs[2]["spokenAnswer"]
                assert "turn_recovered" in caplog.text
            assert outputs[3]["spokenAnswer"] == "Hay 19 proyectos."
            paths = [r["path"] for r in mock_backend[1]]
            assert paths.count("/api/auth/me") == 4
            assert paths.count(f"/api/projects/{PROJECT_ID}") == 2
            assert paths.count("/api/management/overview") == 2
            assert paths.count("/api/management/tasks") == 1
    finally:
        await store.shutdown()
    for private in (TOKEN, KEY, PROJECT_ID, CLIENT_ID, "Casa Ximena", *questions):
        assert private not in caplog.text


@pytest.mark.parametrize("error", [InvalidModelResponse, InvalidToolCall, AgentLimitExceeded])
async def test_only_explicit_response_errors_are_recoverable(mock_backend, error):
    model = FakeModel(error(), call("get_management_overview"), final())
    async with setup_live(mock_backend[0], model) as (manager, socket, vault, registry, _):
        begin(socket)
        socket.incoming.put_nowait(function())
        complete(socket)
        if error is InvalidModelResponse:
            await until(lambda: any(s["type"] == "response.create" for s in socket.sent))
            assert not socket.closed and registry.owned(SESSION, "manager")
            begin(socket, "response_2")
            socket.incoming.put_nowait(function("call_2"))
            complete(socket, "response_2")
            await until(lambda: sum(s["type"] == "response.create" for s in socket.sent) == 2)
        else:
            await until(lambda: not manager._runs)
            assert socket.closed and not registry._entries
            with pytest.raises(LiveSessionUnavailable):
                vault._get(SESSION)
            assert not any(s["type"] == "response.item.create" for s in socket.sent)


@pytest.mark.parametrize("invalidate", ["credential", "owner"])
async def test_invalidated_session_cannot_publish_recoverable_answer(mock_backend, invalidate):
    entered, release = asyncio.Event(), asyncio.Event()

    class FailingModel(FakeModel):
        async def respond(self, messages, tools):
            entered.set()
            await release.wait()
            raise InvalidModelResponse()

    store = ConversationStore()
    entry = store.create("manager")
    try:
        async with setup_live(mock_backend[0], FailingModel()) as (
            manager,
            socket,
            vault,
            registry,
            _,
        ):
            manager._conversations = store
            store.bind_live(SESSION, entry.conversation_id, "manager")
            begin(socket)
            socket.incoming.put_nowait(function())
            complete(socket)
            await asyncio.wait_for(entered.wait(), 1)
            if invalidate == "credential":
                vault.revoke(SESSION)
            else:
                registry.owned(SESSION, "manager").subject = "different-owner"
            release.set()
            await until(lambda: not manager._runs)
            assert socket.closed and not registry._entries
            assert not entry.turns and store.conversation_for_live(SESSION) is None
            assert not any(
                s["type"] in {"response.item.create", "response.create"} for s in socket.sent
            )
    finally:
        await store.shutdown()

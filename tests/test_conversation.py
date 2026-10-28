import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID

import httpx
import openai
import pytest
from conftest import PROJECT_ID, TOKEN
from conversation_repository import MemoryRepository
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_agent import FakeModel, call, final
from test_chat import chat_app
from test_live import AUTH, KEY, LIVE_SETTINGS, SDP, SESSION, FakeLiveProvider, live_app
from test_live_provider import created
from test_openai_model import response_body
from test_sideband import begin, complete, function, until

from app.agent.contracts import ChatResponse, ModelMessage, ProjectReference
from app.agent.errors import ProviderTimeout
from app.agent.openai_model import OpenAIModel
from app.conversation.context import ConversationContext, ConversationTurn
from app.conversation.store import (
    ConversationBusy,
    ConversationForbidden,
    ConversationStore,
    ConversationUnavailable,
)
from app.live.credentials import LiveCredentialContext
from app.live.executor import LiveAgentExecutor
from app.live.provider import OpenAILiveProvider
from app.live.registry import LiveRegistry
from app.live.sideband import LiveSidebandManager

BASE = "/api/intelligence/conversation"


def turn(text="previous answer", question="previous question", references="[]", modality="text"):
    return ConversationTurn(modality, question, text, "direct", references, datetime.now(UTC))


@pytest.mark.anyio
async def test_durable_history_context_bounds_owner_close_and_shutdown():
    repository = MemoryRepository()
    store = ConversationStore(repository)
    entry = await store.resume("manager")
    key = entry.conversation_id
    assert len(key) == 43
    assert (await store.resume("manager")).conversation_id == key
    with pytest.raises(ConversationForbidden):
        await store.owned(key, "other")
    with pytest.raises(ConversationUnavailable):
        await store.owned("x" * 43, "manager")
    async with store.operation(key, "manager"):
        for i in range(20):
            await store.append(key, "manager", turn(str(i)))
    async with store.operation(key, "manager") as context:
        assert len(context.turns) == 16 and context.turns[0].assistant_content == "4"
        await store.append(key, "manager", turn("a" * 15_000))
    async with store.operation(key, "manager") as context:
        assert context.characters <= 16_000
        await store.append(key, "manager", turn("b" * 16_001))
    async with store.operation(key, "manager") as context:
        assert context.turns == ()
    assert len(await repository.list_turns(key, "manager")) == 22
    await store.shutdown()
    assert not store._runtime and not store._live
    restarted = ConversationStore(repository)
    assert (await restarted.resume("manager")).conversation_id == key
    assert len(await repository.list_turns(key, "manager")) == 22
    await restarted.close(key, "manager")
    await restarted.close(key, "manager")
    with pytest.raises(ConversationForbidden):
        await restarted.close(key, "other")
    assert (await restarted.resume("manager")).conversation_id != key
    assert len(await repository.list_turns(key, "manager")) == 22
    await restarted.shutdown()


@pytest.mark.anyio
async def test_concurrency_order_close_cancels_active_and_waiting_operations():
    store = ConversationStore(MemoryRepository())
    entry = await store.create("manager")
    key = entry.conversation_id
    started = asyncio.Event()

    async def blocked():
        async with store.operation(key, "manager"):
            started.set()
            await asyncio.Event().wait()
            await store.append(key, "manager", turn("must not commit"))

    first = asyncio.create_task(blocked())
    await started.wait()
    second = asyncio.create_task(blocked())
    await asyncio.sleep(0)
    await store.close(key, "manager")
    assert (
        first.cancelled()
        and second.cancelled()
        and store.repository.turns[entry.conversation_id] == ()
    )
    await store.shutdown()


@pytest.mark.anyio
async def test_serialized_chat_sideband_context_and_live_blocks_new_text():
    store = ConversationStore(MemoryRepository())
    entry = await store.create("manager")
    key = entry.conversation_id
    seen = []

    async def add(i):
        async with store.operation(key, "manager", live=True) as context:
            seen.append(len(context.turns))
            await asyncio.sleep(0)
            await store.append(key, "manager", turn(str(i), modality="voice" if i else "text"))

    await asyncio.gather(*(add(i) for i in range(5)))
    assert seen == [0, 1, 2, 3, 4]
    await store.bind_live(SESSION, key, "manager")
    with pytest.raises(ConversationBusy):
        async with store.operation(key, "manager"):
            pytest.fail("Text cannot race an active Live session")
    store.detach_live(SESSION)
    async with store.operation(key, "manager") as context:
        assert len(context.turns) == 5
    await store.shutdown()


@pytest.mark.anyio
async def test_shutdown_closes_live_without_closing_history():
    repository = MemoryRepository()
    store = ConversationStore(repository)
    entry = await store.create("manager")
    async with store.operation(entry.conversation_id, "manager"):
        await store.append(entry.conversation_id, "manager", turn())
    closed = []

    async def close_live(session):
        closed.append(session)

    store.close_live = close_live
    await store.bind_live(SESSION, entry.conversation_id, "manager")
    await store.shutdown()
    assert closed == [SESSION]
    assert (await repository.get(entry.conversation_id, "manager")).closed_at is None
    assert len(await repository.list_turns(entry.conversation_id, "manager")) == 1


def test_chat_context_refresh_and_failure_no_commit(settings, mock_backend, payloads, caplog):
    transport, calls = mock_backend
    model = FakeModel(
        call("get_management_overview"),
        final("Tenemos 19 proyectos."),
        call("get_management_overview"),
        final("Ahora tenemos 20 proyectos."),
        final("Tenemos 19 proyectos."),  # Stale-only response must fail closed.
        ProviderTimeout(),
        final("Respuesta independiente.", mode="conversation"),
    )
    app = chat_app(settings, transport, model)
    with TestClient(app) as api:
        response = api.post(BASE, json={}, headers=AUTH)
        assert response.status_code == 201
        key = response.json()["conversationId"]
        for message in ["Cuantos proyectos?", "Cuantos ahora?"]:
            result = api.post(
                "/api/intelligence/chat",
                json={"message": message, "conversationId": key},
                headers=AUTH,
            )
            assert result.status_code == 200
            payloads["/api/management/overview"]["totalProjects"] = 20
        store = app.state.conversations
        entry = store.repository.entries[key]
        assert len(store.repository.turns[entry.conversation_id]) == 2
        assert sum(c["path"] == "/api/management/overview" for c in calls) == 2
        history = model.inputs[2][0]
        assert [m.role for m in history] == ["developer", "user", "assistant", "assistant", "user"]
        assert "19 proyectos" in history[2].content
        assert all(not m.calls and not m.call_id for m in history)
        assert "totalProjects" not in repr(history)
        for expected in [502, 504]:
            result = api.post(
                "/api/intelligence/chat",
                json={"message": "Ahora?", "conversationId": key},
                headers=AUTH,
            )
            assert (
                result.status_code == expected
                and len(store.repository.turns[entry.conversation_id]) == 2
            )
        assert (
            api.post("/api/intelligence/chat", json={"message": "Solo"}, headers=AUTH).status_code
            == 200
        )
        assert (
            len(model.inputs[-1][0]) == 1
            and len(store.repository.turns[entry.conversation_id]) == 2
        )
        assert (
            api.post(BASE + "/close", json={"conversationId": key}, headers=AUTH).status_code == 200
        )
        assert (
            api.post(BASE + "/close", json={"conversationId": key}, headers=AUTH).status_code == 200
        )
        assert (
            api.post(
                "/api/intelligence/chat",
                json={"message": "Otra", "conversationId": key},
                headers=AUTH,
            ).status_code
            == 404
        )
    assert "19 proyectos" not in caplog.text and TOKEN not in caplog.text
    assert "conversation_context_chars" in caplog.text


@pytest.mark.parametrize(
    "path", ["/api/intelligence/chat", "/api/intelligence/live/session", BASE + "/close"]
)
def test_foreign_conversation_rejected(settings, mock_backend, payloads, path):
    transport, _ = mock_backend
    app, provider, model = live_app(settings, transport)
    with TestClient(app) as api:
        key = api.post(BASE, json={}, headers=AUTH).json()["conversationId"]
        payloads["/api/auth/me"]["subject"] = "other"
        body = {"conversationId": key}
        if path.endswith("chat"):
            body["message"] = "Consulta"
        if path.endswith("session"):
            body["sdp"] = SDP
        assert api.post(path, json=body, headers=AUTH).status_code == 403
        assert not provider.calls and not model.inputs


@pytest.mark.parametrize(
    "body", [{"subject": "forged"}, {"conversationId": "x" * 43}, {"history": []}]
)
def test_create_server_owned_only(settings, mock_backend, body):
    transport, _ = mock_backend
    with TestClient(chat_app(settings, transport, FakeModel())) as api:
        assert api.post(BASE, json=body, headers=AUTH).status_code == 422
        assert api.post(BASE, json={}).status_code == 401


def test_creation_management_only(settings, mock_backend, payloads):
    transport, _ = mock_backend
    payloads["/api/auth/me"]["isManagement"] = False
    with TestClient(chat_app(settings, transport, FakeModel())) as api:
        assert api.post(BASE, json={}, headers=AUTH).status_code == 403
        assert not api.app.state.conversations.repository.entries


@pytest.mark.anyio
async def test_live_initial_input_real_wire_and_responses_history():
    refs = json.dumps([{"type": "project", "projectId": PROJECT_ID, "label": "Casa Ximena"}])
    context = ConversationContext((turn("Casa Ximena o Casa X?", "Como va Casa?", refs),))
    captured = []

    def live(request):
        captured.append(json.loads(request.content))
        return httpx.Response(201, json=created())

    async with httpx.AsyncClient(transport=httpx.MockTransport(live)) as http:
        await OpenAILiveProvider(http, SecretStr(KEY), LIVE_SETTINGS).create(SDP, context)
    session = captured[0]["session"]
    assert session["input"] == context.live_input()
    assert session["input"][2]["content"][0]["type"] == "output_text"
    assert "Casa Ximena o Casa X?" not in session["instructions"]
    assert session["store"] is False
    assert TOKEN not in json.dumps(session) and KEY not in json.dumps(session)

    def responses(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=response_body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(responses)) as http:
        async with openai.AsyncOpenAI(api_key=KEY, http_client=http) as sdk:
            await OpenAIModel(sdk, "test").respond(
                (*context.messages(), ModelMessage("user", "La de Ximena")), ()
            )
    wire = captured[1]
    assert wire["store"] is False and "previous_response_id" not in wire
    assert [m["role"] for m in wire["input"]] == [
        "developer",
        "user",
        "assistant",
        "assistant",
        "user",
    ]
    assert all("type" not in m for m in wire["input"])


@pytest.mark.anyio
async def test_voice_commit_once_before_output_and_live_close_preserves_context(mock_backend):
    from contextlib import asynccontextmanager

    transport, _ = mock_backend
    store, registry, vault = (
        ConversationStore(MemoryRepository()),
        LiveRegistry(),
        LiveCredentialContext(),
    )
    entry = await store.create("manager")
    key = entry.conversation_id
    model = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        final("Proyecto: datos actuales.", [f"project:{PROJECT_ID}"]),
    )

    @asynccontextmanager
    async def models():
        yield model

    provider = FakeLiveProvider()
    async with httpx.AsyncClient(transport=transport, base_url="https://control.test") as http:
        manager = LiveSidebandManager(
            registry,
            vault,
            LiveAgentExecutor(http, vault, models),
            conversations=store,
            close_grace=0.02,
        )
        store.close_live = manager.close_internal
        registry.add(SESSION, "manager", 600)
        vault.capture(
            SESSION, SecretStr(TOKEN), "manager", registry.owned(SESSION, "manager").expires_at
        )
        await store.bind_live(SESSION, key, "manager")
        await manager.attach(SESSION, "manager", provider, "test-id")
        original = provider.socket.send

        async def send(event):
            if event["type"] == "response.item.create":
                assert len(store.repository.turns[entry.conversation_id]) == 1
            await original(event)

        provider.socket.send = send
        begin(provider.socket)
        provider.socket.incoming.put_nowait(function())
        provider.socket.incoming.put_nowait(function())
        complete(provider.socket)
        await until(lambda: any(e["type"] == "response.create" for e in provider.socket.sent))
        assert (
            len(store.repository.turns[entry.conversation_id]) == 1
            and store.repository.turns[entry.conversation_id][0].modality == "voice"
        )
        assert (
            json.loads(store.repository.turns[entry.conversation_id][0].references)[0]["projectId"]
            == PROJECT_ID
        )
        await manager.close(SESSION, "manager")
        assert store.repository.turns[key] and store.conversation_for_live(SESSION) is None
        assert TOKEN not in repr(entry) + str(
            ConversationContext(store.repository.turns[entry.conversation_id]).live_input()
        )
        await store.shutdown()
        await manager.shutdown()


def test_live_binding_close_reopen_and_rollback(settings, mock_backend):
    transport, _ = mock_backend
    app, provider, _ = live_app(settings, transport)
    histories = []
    original_create = provider.create

    async def create_with_context(sdp, context=None):
        histories.append(context)
        return await original_create(sdp)

    provider.create = create_with_context
    with TestClient(app) as api:
        key = api.post(BASE, json={}, headers=AUTH).json()["conversationId"]
        body = {"sdp": SDP, "conversationId": key}
        assert (
            api.post("/api/intelligence/live/session", json=body, headers=AUTH).status_code == 201
        )
        assert histories[0] == ConversationContext()
        assert (
            api.post(
                "/api/intelligence/chat",
                json={"message": "Hola", "conversationId": key},
                headers=AUTH,
            ).status_code
            == 409
        )
        assert (
            api.post(BASE + "/close", json={"conversationId": key}, headers=AUTH).status_code == 200
        )
        assert provider.socket.closed and not app.state.live_sidebands._runs
        assert app.state.live_credentials._LiveCredentialContext__items == {}
        assert app.state.conversations.repository.entries[key].closed_at is not None


@pytest.mark.parametrize(
    "question,tool",
    [
        ("Dame mas informacion del segundo", "get_project_details"),
        ("Y que documentos tiene?", "get_project_documents"),
    ],
)
def test_ordered_voice_references_and_active_project_refresh(
    settings, mock_backend, question, tool
):
    transport, calls = mock_backend
    second = ProjectReference(project_id=UUID(PROJECT_ID), label="Proyecto", sg_code=None)
    first = ProjectReference(
        project_id=UUID("10000000-0000-4000-8000-000000000099"), label="Casa Ximena", sg_code=None
    )
    model = FakeModel(call(tool, {"project_id": PROJECT_ID}), final("Datos consultados ahora."))
    app = chat_app(settings, transport, model)
    with TestClient(app) as api:
        key = api.post(BASE, json={}, headers=AUTH).json()["conversationId"]

        async def seed():
            async with app.state.conversations.operation(key, "manager"):
                result = ChatResponse(
                    response_type="direct",
                    answer="1. Casa Ximena; 2. Proyecto",
                    spoken_answer="1. Casa Ximena; 2. Proyecto",
                    references=[first, second],
                    presentation={"kind": "direct", "metric": None},
                )
                await app.state.conversations.append(
                    key, "manager", ConversationTurn.completed("voice", "Proyectos?", result)
                )

        api.portal.call(seed)
        result = api.post(
            "/api/intelligence/chat",
            json={"message": question, "conversationId": key},
            headers=AUTH,
        )
        assert result.status_code == 200
        metadata = model.inputs[0][0][-2].content
        assert metadata.index("Casa Ximena") < metadata.index('"label":"Proyecto"')
        assert any(PROJECT_ID in c["path"] for c in calls)
        assert result.json()["references"] == []  # Old references are NOT current evidence.


@pytest.mark.parametrize(
    "content",
    [
        TOKEN,
        KEY,
        "Bearer other-token",
        "sk-othersecret123456",
        SDP,
        "eyJhbGciOiJIUzI1NiJ9.payload.signature",
        "data:audio/mp3;base64,aaaa",
    ],
)
def test_sensitive_content_never_retained_or_sent_to_model(
    settings, mock_backend, monkeypatch, content, caplog
):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    transport, _ = mock_backend
    model = FakeModel(final())
    app = chat_app(settings, transport, model)
    with TestClient(app) as api:
        key = api.post(BASE, json={}, headers=AUTH).json()["conversationId"]
        result = api.post(
            "/api/intelligence/chat", headers=AUTH, json={"message": content, "conversationId": key}
        )
        assert result.status_code >= 400
        assert not model.inputs
        assert app.state.conversations.repository.turns[key] == ()
        assert content not in result.text + caplog.text


@pytest.mark.anyio
async def test_close_associated_live_cancels_delegation_and_suppresses_output(mock_backend):
    from contextlib import asynccontextmanager

    transport, _ = mock_backend
    store, registry, vault = (
        ConversationStore(MemoryRepository()),
        LiveRegistry(),
        LiveCredentialContext(),
    )
    entry = await store.create("manager")
    started = asyncio.Event()

    class WaitingModel:
        async def respond(self, messages, tools):
            started.set()
            await asyncio.Event().wait()
            return final("Must not return")

    @asynccontextmanager
    async def models():
        yield WaitingModel()

    provider = FakeLiveProvider()
    async with httpx.AsyncClient(transport=transport, base_url="https://control.test") as http:
        manager = LiveSidebandManager(
            registry,
            vault,
            LiveAgentExecutor(http, vault, models),
            conversations=store,
            close_grace=0.02,
        )
        store.close_live = manager.close_internal
        registry.add(SESSION, "manager", 600)
        vault.capture(
            SESSION, SecretStr(TOKEN), "manager", registry.owned(SESSION, "manager").expires_at
        )
        await store.bind_live(SESSION, entry.conversation_id, "manager")
        await manager.attach(SESSION, "manager", provider, "safe-request-id")
        begin(provider.socket)
        provider.socket.incoming.put_nowait(function())
        complete(provider.socket)
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(store.close(entry.conversation_id, "manager"), 2)
        assert not store.repository.turns[entry.conversation_id] and provider.socket.closed
        assert not vault._LiveCredentialContext__items and not manager._runs
        assert all(event["type"] == "session.close" for event in provider.socket.sent)
        await store.shutdown()
        await manager.shutdown()


def test_attach_failure_does_not_clear_conversation_but_removes_binding(settings, mock_backend):
    from app.live.errors import LiveAttachError

    transport, _ = mock_backend
    app, provider, _ = live_app(settings, transport)
    original_create = provider.create

    async def create_with_context(sdp, context=None):
        return await original_create(sdp)

    provider.create = create_with_context
    provider.attach_failure = LiveAttachError()
    with TestClient(app) as api:
        key = api.post(BASE, json={}, headers=AUTH).json()["conversationId"]
        result = api.post(
            "/api/intelligence/live/session", json={"sdp": SDP, "conversationId": key}, headers=AUTH
        )
        assert result.status_code >= 400
        assert app.state.conversations.repository.entries[key].closed_at is None
        assert not app.state.conversations._live
        assert not app.state.live_credentials._LiveCredentialContext__items


def test_offline_text_to_live_to_text_and_reopen(settings, mock_backend, payloads):
    """Scripted protocol eval, not a claim about real model referent quality."""
    from contextlib import asynccontextmanager

    from conftest import CLIENT_ID
    from test_live import FakeSocket

    transport, backend_calls = mock_backend
    project = payloads[f"/api/projects/{PROJECT_ID}"]
    project["name"] = "Casa Ximena"
    payloads["/api/projects"] = [project, {**project, "id": CLIENT_ID, "name": "Casa X"}]
    model = FakeModel(
        call("find_projects", {"query": "Casa"}),
        final(
            "Encontré Casa X y Casa Ximena. ¿Cuál quieres revisar?",
            [f"project:{CLIENT_ID}", f"project:{PROJECT_ID}"],
            kind="clarification",
        ),
        call("get_project_documents", {"project_id": PROJECT_ID}),
        final("Documentos consultados."),
    )
    voice_model = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        final("Casa Ximena: datos actuales.", [f"project:{PROJECT_ID}"]),
    )

    @asynccontextmanager
    async def voice_models():
        yield voice_model

    app, provider, _ = live_app(settings, transport, model)
    captured = []
    original = provider.create

    async def create_with_history(sdp, context=None):
        captured.append(context)
        return await original(sdp)

    provider.create = create_with_history
    with TestClient(app) as api:
        app.state.live_sidebands._executor = LiveAgentExecutor(
            app.state.control_http, app.state.live_credentials, voice_models
        )
        key = api.post(BASE, json={}, headers=AUTH).json()["conversationId"]
        assert (
            api.post(
                "/api/intelligence/chat",
                headers=AUTH,
                json={"message": "¿Cómo va Casa?", "conversationId": key},
            ).status_code
            == 200
        )
        assert (
            api.post(
                "/api/intelligence/live/session",
                headers=AUTH,
                json={"sdp": SDP, "conversationId": key},
            ).status_code
            == 201
        )
        assert len(captured[0].turns) == 1
        assert "Casa Ximena" in str(captured[0].live_input())

        async def delegate():
            begin(provider.socket)
            # Script the provider's self-contained resolution of "La de Ximena".
            provider.socket.incoming.put_nowait(function(question="¿Cómo va Casa Ximena?"))
            complete(provider.socket)
            await until(lambda: any(e["type"] == "response.create" for e in provider.socket.sent))

        api.portal.call(delegate)
        store = app.state.conversations
        entry = store.repository.entries[key]
        assert [t.modality for t in store.repository.turns[entry.conversation_id]] == [
            "text",
            "voice",
        ]
        assert "Casa Ximena" in repr(voice_model.inputs[0][0])
        assert (
            api.post(
                "/api/intelligence/live/close", headers=AUTH, json={"sessionId": SESSION}
            ).status_code
            == 200
        )
        assert (
            api.post(
                "/api/intelligence/chat",
                headers=AUTH,
                json={"message": "¿Y qué documentos tiene?", "conversationId": key},
            ).status_code
            == 200
        )
        assert any(c["path"] == f"/api/projects/{PROJECT_ID}/documents" for c in backend_calls)
        assert "Casa Ximena: datos actuales." in repr(model.inputs[2][0])
        provider.socket = FakeSocket()
        assert (
            api.post(
                "/api/intelligence/live/session",
                headers=AUTH,
                json={"sdp": SDP, "conversationId": key},
            ).status_code
            == 201
        )
        assert len(captured[1].turns) == 3
        assert TOKEN not in str(captured[1].live_input())


def test_live_startup_budget_drops_whole_oldest_turns():
    context = ConversationContext(
        tuple(turn("OLD BUSINESS ANSWER", question=str(i) + "ñ" * 350) for i in range(16))
    )
    wire = context.live_input()
    assert len(wire) < len(context.messages())
    assert wire[-2]["content"][0]["text"] == "15" + "ñ" * 350
    assert (
        sum(len(part["text"].encode("utf-8")) for message in wire for part in message["content"])
        <= 6000
    )
    assert all("OLD BUSINESS ANSWER" not in m["content"][0]["text"] for m in wire)
    users = [m["content"][0]["text"] for m in wire if m["role"] == "user"]
    assert 0 < len(users) < 16 and "0" + "ñ" * 350 not in users
    assert "Context coverage is incomplete" in str(wire)
    assert len(context.turns) == 16  # Provider projection does not mutate stored context.

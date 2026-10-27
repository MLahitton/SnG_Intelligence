import asyncio
import json
import logging
import pickle
from contextlib import asynccontextmanager

import httpx
import pytest
from conftest import PROJECT_ID, TOKEN
from conversation_repository import MemoryRepository
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_agent import FakeModel, call, final
from test_live import AUTH, BASE, KEY, SESSION, FakeLiveProvider, create, live_app

from app.agent.errors import AgentUnavailable
from app.conversation.store import ConversationStore
from app.live.credentials import LiveCredentialContext
from app.live.errors import (
    LiveAttachError,
    LiveDisconnected,
    LiveProtocolError,
    LiveSessionUnavailable,
)
from app.live.events import parse_event
from app.live.executor import LiveAgentExecutor
from app.live.registry import LiveRegistry
from app.live.sideband import LiveSidebandManager


def test_live_interruption_policy_preserves_business_grounding():
    from app.live.prompt import DELEGATION_INSTRUCTIONS, LIVE_INSTRUCTIONS, consultation_tool

    assert "habla clara e intencional del usuario" in LIVE_INSTRUCTIONS
    for text in (
        "ruido ambiente",
        "ecos",
        "sonidos breves",
        "silencios parciales",
        "tu propia reproducción",
        "complete su nuevo turno",
    ):
        assert text in LIVE_INSTRUCTIONS
    assert "spokenAnswer es la autoridad factual" in LIVE_INSTRUCTIONS
    assert "No inventes datos empresariales" in LIVE_INSTRUCTIONS
    assert "No respondas hechos empresariales de memoria" in DELEGATION_INSTRUCTIONS
    assert consultation_tool()["name"] == "consult_sng_intelligence"


def response(kind, **data):
    return {
        "type": "response.event",
        "delegation_id": "delegation_1",
        "event": {"type": kind, **data},
    }


def function(call_id="call_1", question="¿Cómo va Casa Ximena?", **extra):
    return response(
        "response.output_item.done",
        item={
            "type": "function_call",
            "name": "consult_sng_intelligence",
            "call_id": call_id,
            "arguments": json.dumps({"question": question}),
            **extra,
        },
    )


def begin(socket, response_id="response_1"):
    socket.incoming.put_nowait(
        {
            "type": "session.delegation.created",
            "delegation": {"id": "delegation_1", "target": "responses"},
        }
    )
    socket.incoming.put_nowait(response("response.created", response={"id": response_id}))


def complete(socket, response_id="response_1"):
    socket.incoming.put_nowait(
        response("response.completed", response={"id": response_id, "output": []})
    )


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():  # noqa: ASYNC110 - bounded polling of observable fake transport state
            await asyncio.sleep(0.001)


@asynccontextmanager
async def setup_live(transport, model=None, lifetime=600):
    registry = LiveRegistry()
    vault = LiveCredentialContext()
    provider = FakeLiveProvider()
    model = model or FakeModel(final())

    @asynccontextmanager
    async def model_factory():
        yield model

    async with httpx.AsyncClient(transport=transport, base_url="https://control.test") as http:
        executor = LiveAgentExecutor(http, vault, model_factory)
        manager = LiveSidebandManager(registry, vault, executor, close_grace=0.02)
        registry.add(SESSION, "manager", lifetime)
        vault.capture(
            SESSION, SecretStr(TOKEN), "manager", registry.owned(SESSION, "manager").expires_at
        )
        await manager.attach(SESSION, "manager", provider, "safe-correlation-id")
        try:
            yield manager, provider.socket, vault, registry, model
        finally:
            await manager.shutdown()


def test_credential_scope_lifetime_no_serialization():
    clock = [10.0]
    vault = LiveCredentialContext(lambda: clock[0])
    vault.capture(SESSION, SecretStr(TOKEN), "owner", 10000)
    lease = vault._get(SESSION)
    assert isinstance(lease.token, SecretStr)
    assert lease.subject == "owner" and lease.expires_at == 610
    assert TOKEN not in repr(vault) + repr(lease)
    with pytest.raises(TypeError):
        pickle.dumps(vault)
    with pytest.raises(TypeError):
        pickle.dumps(lease)
    with pytest.raises(TypeError):
        json.dumps(vault)
    clock[0] = 610
    with pytest.raises(LiveSessionUnavailable):
        vault._get(SESSION)
    assert vault._LiveCredentialContext__items == {}
    vault.capture("new", SecretStr("another-synthetic-token"), "owner", 620)
    assert vault._get("new").token.get_secret_value() != TOKEN
    vault.clear()
    with pytest.raises(LiveSessionUnavailable):
        vault._get("new")


@pytest.mark.anyio
async def test_real_agent_grounding_and_exactly_once_batch(mock_backend, caplog, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    model = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        final(
            "Proyecto: sin iniciar.",
            [f"project:{PROJECT_ID}"],
            kind="project_status",
            presentation=[f"project:{PROJECT_ID}"],
        ),
        final("No dispongo de ese dato."),
    )
    transport, backend_calls = mock_backend
    async with setup_live(transport, model) as (manager, socket, vault, registry, _):
        begin(socket)
        socket.incoming.put_nowait(function())
        socket.incoming.put_nowait(function())
        socket.incoming.put_nowait(function("call_2", "¿Cuánto facturamos?"))
        complete(socket)
        complete(socket)
        await until(lambda: any(x["type"] == "response.create" for x in socket.sent))
        assert [x["type"] for x in socket.sent] == [
            "response.item.create",
            "response.item.create",
            "response.create",
        ]
        result = json.loads(socket.sent[0]["item"]["output"])
        assert result == {
            "responseType": "project_status",
            "spokenAnswer": "Proyecto: sin iniciar.",
        }
        assert socket.sent[0]["item"]["call_id"] == "call_1"
        assert socket.sent[1]["item"]["call_id"] == "call_2"
        socket.incoming.put_nowait(function())
        await asyncio.sleep(0.01)
        assert len(model.inputs) == 3  # Two independent Agent runs, one tool round.
        assert sum(c["path"] == "/api/auth/me" for c in backend_calls) == 2
        entry = registry.owned(SESSION, "manager")
        assert not any(hasattr(entry, k) for k in ("token", "sdp", "transcript", "question"))
        assert TOKEN not in repr(registry.__dict__) + json.dumps(socket.sent)
        await manager.close(SESSION, "manager")
        await manager.close(SESSION, "manager")
        assert socket.closed and not manager._runs
        with pytest.raises(LiveSessionUnavailable):
            vault._get(SESSION)
    assert all(c["method"] == "GET" for c in backend_calls)
    for text in (TOKEN, KEY, "Proyecto: sin iniciar.", "¿Cómo va Casa Ximena?"):
        assert text not in caplog.text


@pytest.mark.parametrize(
    "event",
    [
        response("response.function_call_arguments.delta", delta='{"question":'),
        response("response.function_call_arguments.done", arguments='{"question":"Consulta"}'),
        {"type": "session.input_transcript.delta", "delta": "consult_sng_intelligence"},
        {"type": "session.input_audio.append", "audio": "synthetic"},
    ],
    ids=["delta", "arguments-done", "transcript", "audio"],
)
@pytest.mark.anyio
async def test_partial_arguments_and_transcripts_never_execute(mock_backend, event):
    async with setup_live(mock_backend[0]) as (_, socket, _, _, model):
        begin(socket)
        socket.incoming.put_nowait(event)
        await asyncio.sleep(0.01)
        assert not model.inputs and not socket.sent


@pytest.mark.parametrize(
    "event",
    [
        function(name="arbitrary"),
        function(arguments='{"question":""}'),
        function(arguments='{"question":"Consulta","subject":"manager"}'),
        function(arguments='{"question":123}'),
        function(question="x" * 4001),
        function(call_id="../escape"),
        function(status="in_progress"),
        function(question=TOKEN),
        "not json",
        {"type": "response.event", "event": []},
    ],
    ids=[
        "tool",
        "blank",
        "extra",
        "wrong-type",
        "long",
        "call-id",
        "unfinished",
        "secret",
        "json",
        "shape",
    ],
)
@pytest.mark.anyio
async def test_invalid_calls_fail_closed_without_execution(mock_backend, event, caplog):
    async with setup_live(mock_backend[0]) as (manager, socket, vault, registry, model):
        begin(socket)
        socket.incoming.put_nowait(event)
        complete(socket)
        await until(lambda: socket.closed)
        assert not model.inputs and not registry._entries
        assert not any(x["type"] == "response.item.create" for x in socket.sent)
        with pytest.raises(LiveSessionUnavailable):
            vault._get(SESSION)
        assert not manager._runs
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("change", ["subject", "management", "401", "403"])
@pytest.mark.anyio
async def test_reauthorization_revokes_before_agent(mock_backend, payloads, change):
    if change in {"401", "403"}:
        transport = httpx.MockTransport(lambda request: httpx.Response(int(change)))
    else:
        transport = mock_backend[0]
        if change == "subject":
            payloads["/api/auth/me"]["subject"] = "different"
        else:
            payloads["/api/auth/me"]["isManagement"] = False
    async with setup_live(transport) as (_, socket, vault, registry, model):
        begin(socket)
        socket.incoming.put_nowait(function())
        complete(socket)
        await until(lambda: socket.closed)
        assert not model.inputs and not registry._entries
        assert [x["type"] for x in socket.sent] == ["session.close"]
        with pytest.raises(LiveSessionUnavailable):
            vault._get(SESSION)


@pytest.mark.parametrize(
    "ending", ["remote-close", "disconnect", "malformed", "shutdown", "expiry"]
)
@pytest.mark.anyio
async def test_all_terminal_paths_revoke_and_cleanup(mock_backend, ending):
    async with setup_live(mock_backend[0], lifetime=0.04 if ending == "expiry" else 600) as (
        manager,
        socket,
        vault,
        registry,
        _,
    ):
        run = manager._runs[SESSION]
        if ending == "remote-close":
            socket.incoming.put_nowait({"type": "session.closed"})
        elif ending == "disconnect":
            socket.incoming.put_nowait(LiveDisconnected())
        elif ending == "malformed":
            socket.incoming.put_nowait("garbage")
        elif ending == "shutdown":
            await manager.shutdown()
        await until(lambda: socket.closed)
        assert not registry._entries and not manager._runs
        assert run.receiver.done() and run.expiry.done()
        with pytest.raises(LiveSessionUnavailable):
            vault._get(SESSION)


@pytest.mark.parametrize("ending", ["close", "expiry", "shutdown", "disconnect", "timeout"])
@pytest.mark.anyio
async def test_revocation_cancels_active_agent_and_never_delivers(mock_backend, ending):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class SlowModel(FakeModel):
        async def respond(self, messages, tools):
            entered.set()
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()

    async with setup_live(
        mock_backend[0], SlowModel(), lifetime=0.08 if ending == "expiry" else 600
    ) as (manager, socket, vault, registry, _):
        if ending == "timeout":
            manager._tool_timeout = 0.03
        begin(socket)
        socket.incoming.put_nowait(function())
        complete(socket)
        await asyncio.wait_for(entered.wait(), 1)
        if ending == "close":
            await manager.close(SESSION, "manager")
        elif ending == "shutdown":
            await manager.shutdown()
        elif ending == "disconnect":
            socket.incoming.put_nowait(LiveDisconnected())
        await until(lambda: socket.closed)
        assert cancelled.is_set()
        assert [x["type"] for x in socket.sent] == ["session.close"]
        with pytest.raises(LiveSessionUnavailable):
            vault._get(SESSION)


@pytest.mark.anyio
async def test_agent_failure_does_not_fabricate_output(mock_backend):
    async with setup_live(mock_backend[0], FakeModel(AgentUnavailable())) as (
        _,
        socket,
        vault,
        _,
        _,
    ):
        begin(socket)
        socket.incoming.put_nowait(function())
        complete(socket)
        await until(lambda: socket.closed)
        assert [x["type"] for x in socket.sent] == ["session.close"]
        with pytest.raises(LiveSessionUnavailable):
            vault._get(SESSION)


def test_attach_failure_rolls_back_public_creation(settings, mock_backend):
    app, provider, _ = live_app(settings, mock_backend[0])
    provider.attach_failure = LiveAttachError()
    with TestClient(app) as api:
        assert create(api).status_code == 502
        assert not app.state.live_registry._entries
        assert not app.state.live_registry._pending
        assert not app.state.live_sidebands._runs
        with pytest.raises(LiveSessionUnavailable):
            app.state.live_credentials._get(SESSION)


def test_credential_only_captured_after_auth_and_public_delegate_removed(
    settings, mock_backend, payloads
):
    payloads["/api/auth/me"]["isManagement"] = False
    app, provider, _ = live_app(settings, mock_backend[0])
    with TestClient(app) as api:
        assert create(api).status_code == 403
        assert not provider.attached
        assert not app.state.live_credentials._LiveCredentialContext__items
        assert api.post(BASE + "/delegate", json={}, headers=AUTH).status_code == 404
        assert "/api/intelligence/live/delegate" not in api.get("/openapi.json").json()["paths"]


@pytest.mark.parametrize(
    "raw",
    ["{}", "[]", "null", '"hello"', '{"type":3}', "x" * 262145],
    ids=["object", "array", "null", "string", "type", "oversize"],
)
def test_event_boundary_rejects_malformed_and_oversized(raw):
    with pytest.raises(LiveProtocolError):
        parse_event(raw)


@pytest.mark.anyio
async def test_public_success_waits_for_authenticated_attach(settings, mock_backend):
    from test_live import SDP

    app, provider, _ = live_app(settings, mock_backend[0])
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_attach(session_id):
        entered.set()
        await release.wait()
        return provider.socket

    provider.attach = delayed_attach
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://intelligence.test"
        ) as api,
    ):
        task = asyncio.create_task(api.post(BASE + "/session", headers=AUTH, json={"sdp": SDP}))
        await asyncio.wait_for(entered.wait(), 1)
        assert not task.done()
        assert isinstance(app.state.live_credentials._get(SESSION).token, SecretStr)
        release.set()
        result = await task
        assert result.status_code == 201 and set(result.json()) == {"sessionId", "sdp"}
        assert TOKEN not in result.text and KEY not in result.text


@pytest.mark.anyio
async def test_attach_cancel_rolls_back_credential_and_registry(settings, mock_backend):
    from test_live import SDP

    app, provider, _ = live_app(settings, mock_backend[0])
    entered = asyncio.Event()

    async def cancelled_attach(session_id):
        entered.set()
        await asyncio.sleep(60)

    provider.attach = cancelled_attach
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://intelligence.test"
        ) as api,
    ):
        task = asyncio.create_task(api.post(BASE + "/session", headers=AUTH, json={"sdp": SDP}))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not app.state.live_sidebands._runs
        assert not app.state.live_registry._entries
        assert not app.state.live_credentials._LiveCredentialContext__items


@pytest.mark.anyio
async def test_new_response_racing_continuation_does_not_duplicate_or_fail(mock_backend):
    async with setup_live(mock_backend[0], FakeModel(final(), final())) as (
        manager,
        socket,
        _,
        _,
        model,
    ):
        original = socket.send
        continued = 0

        async def send(event):
            nonlocal continued
            await original(event)
            if event["type"] == "response.create":
                continued += 1
                if continued == 1:
                    socket.incoming.put_nowait(
                        response("response.created", response={"id": "response_2"})
                    )
                    socket.incoming.put_nowait(function("call_2"))
                    complete(socket, "response_2")
                    await asyncio.sleep(0.01)

        socket.send = send
        begin(socket)
        socket.incoming.put_nowait(function())
        complete(socket)
        await until(lambda: continued == 2)
        assert len(model.inputs) == 2 and not socket.closed
        assert len(manager._registry.owned(SESSION, "manager").calls) == 2


@pytest.mark.anyio
async def test_contract_smoke_http_create_sideband_agent_and_close(settings, mock_backend, caplog):
    from test_live import SDP

    model = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        final(
            "Proyecto: sin iniciar.",
            [f"project:{PROJECT_ID}"],
            kind="project_status",
            presentation=[f"project:{PROJECT_ID}"],
        ),
    )

    @asynccontextmanager
    async def model_factory():
        yield model

    app, provider, _ = live_app(settings, mock_backend[0])
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://intelligence.test"
        ) as api,
    ):
        vault = app.state.live_credentials
        app.state.live_sidebands._executor = LiveAgentExecutor(
            app.state.control_http, vault, model_factory
        )
        created = await api.post(BASE + "/session", headers=AUTH, json={"sdp": SDP})
        assert created.status_code == 201 and provider.attached
        begin(provider.socket)
        provider.socket.incoming.put_nowait(function())
        complete(provider.socket)
        await until(lambda: any(e["type"] == "response.create" for e in provider.socket.sent))
        output = json.loads(provider.socket.sent[0]["item"]["output"])
        assert output == {
            "responseType": "project_status",
            "spokenAnswer": "Proyecto: sin iniciar.",
        }
        closed = await api.post(BASE + "/close", headers=AUTH, json={"sessionId": SESSION})
        assert closed.json() == {"closed": True} and provider.socket.closed
        assert not app.state.live_registry._entries and not app.state.live_sidebands._runs
        with pytest.raises(LiveSessionUnavailable):
            vault._get(SESSION)
    assert TOKEN not in caplog.text and SDP not in caplog.text
    assert "¿Cómo va Casa Ximena?" not in caplog.text


@pytest.mark.parametrize(
    "ending", ["response.failed", "response.incomplete", "provider-error", "remote-close"]
)
@pytest.mark.parametrize("executing", [False, True])
@pytest.mark.anyio
async def test_terminal_turn_events_clear_jobs_credentials_and_conversation_binding(
    mock_backend, ending, executing, caplog
):
    caplog.set_level(logging.INFO, logger="sng_intelligence")
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class WaitingModel(FakeModel):
        async def respond(self, messages, tools):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    store = ConversationStore(MemoryRepository())
    entry = await store.create("manager")
    try:
        async with setup_live(mock_backend[0], WaitingModel()) as (
            manager,
            socket,
            vault,
            registry,
            _,
        ):
            manager._conversations = store
            await store.bind_live(SESSION, entry.conversation_id, "manager")
            run = manager._runs[SESSION]
            begin(socket)
            socket.incoming.put_nowait(function())
            if executing:
                complete(socket)
                await asyncio.wait_for(entered.wait(), 1)
            else:
                await until(lambda: bool(run.pending))
            if ending == "provider-error":
                # Untrusted unknown code must never be copied to telemetry.
                socket.incoming.put_nowait(
                    {
                        "type": "error",
                        "error": {
                            "code": KEY,
                            "message": "PRIVATE diagnostic and transcript",
                        },
                    }
                )
            elif ending == "remote-close":
                socket.incoming.put_nowait({"type": "session.closed", "reason": "PRIVATE"})
            else:
                socket.incoming.put_nowait(
                    response(
                        ending,
                        response={
                            "id": "response_1",
                            "error": {"message": "PRIVATE"},
                            "incomplete_details": {"reason": "PRIVATE"},
                        },
                    )
                )
            await until(lambda: not manager._runs)
            assert not run.pending and not run.accepted
            assert run.response_id is None and run.delegation_id is None
            assert run.job is None or run.job.done()
            assert run.receiver.done() and run.expiry.done()
            assert not registry._entries and socket.closed
            assert cancelled.is_set() == executing
            with pytest.raises(LiveSessionUnavailable):
                vault._get(SESSION)
            assert store.conversation_for_live(SESSION) is None
            assert store.repository.turns[entry.conversation_id] == ()
            # A failed Live turn leaves text continuation possible, without a stale lock.
            async with asyncio.timeout(1):
                async with store.operation(entry.conversation_id, "manager") as context:
                    assert not context.turns
            assert not any(
                item["type"] in {"response.item.create", "response.create"} for item in socket.sent
            )
            rows = [
                json.loads(record.message)
                for record in caplog.records
                if record.name == "sng_intelligence" and record.message.startswith("{")
            ]
            expected = (
                ending
                if ending.startswith("response.")
                else ("provider_error" if ending == "provider-error" else "provider_session_closed")
            )
            assert any(row.get("live_event") == expected for row in rows)
            assert any(row.get("live_event") == "session_closed" for row in rows)
            assert any(row.get("turn_number") == 1 for row in rows)
            for sensitive in (TOKEN, KEY, "PRIVATE", "¿Cómo va Casa Ximena?"):
                assert sensitive not in caplog.text
    finally:
        await store.shutdown()


@pytest.mark.anyio
async def test_completed_turn_admits_next_delegation_and_logs_safe_order(mock_backend, caplog):
    caplog.set_level(logging.INFO, logger="sng_intelligence")
    async with setup_live(mock_backend[0]) as (manager, socket, _, _, _):
        begin(socket)
        complete(socket)
        await until(lambda: manager._runs[SESSION].completed)
        socket.incoming.put_nowait(
            {
                "type": "session.delegation.created",
                "delegation": {
                    "id": "delegation_2",
                    "target": "responses",
                },
            }
        )
        for kind in ("response.created", "response.completed"):
            socket.incoming.put_nowait(
                {
                    "type": "response.event",
                    "delegation_id": "delegation_2",
                    "event": {"type": kind, "response": {"id": "response_2"}},
                }
            )
        await until(
            lambda: manager._runs[SESSION].turn_number == 2 and manager._runs[SESSION].completed
        )
        assert not socket.closed and not manager._runs[SESSION].pending
        rows = [
            json.loads(record.message)
            for record in caplog.records
            if record.message.startswith("{")
        ]
        events = [
            row.get("live_event")
            for row in rows
            if row.get("live_event")
            in {"delegation_created", "response.created", "response.completed"}
        ]
        assert events == ["delegation_created", "response.created", "response.completed"] * 2

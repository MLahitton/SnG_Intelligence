import asyncio
import json

import pytest
from conftest import TOKEN
from fastapi.testclient import TestClient
from test_agent import FakeModel, final

from app.agent.errors import ProviderTimeout
from app.api.dependencies import get_agent_model
from app.api.live import get_live_provider, get_live_settings
from app.live.config import LiveSettings
from app.live.contracts import LiveSessionResponse
from app.live.errors import LiveProviderError
from app.main import create_app

SDP = (
    "v=0\r\no=- 123 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=ice-ufrag:test\r\n"
    "a=ice-pwd:test-ice-password\r\na=fingerprint:sha-256 AA:BB\r\n"
    "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
)
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SESSION = "live_test-opaque-123"
LIVE_SETTINGS = LiveSettings("gpt-live-1", "gpt-6-luna", "marin")
BASE = "/api/intelligence/live"
KEY = "test-live-provider-key"


class FakeSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def receive(self):
        value = await self.incoming.get()
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, str) else json.dumps(value)

    async def send(self, event):
        assert TOKEN not in json.dumps(event) and KEY not in json.dumps(event)
        self.sent.append(event)
        if event["type"] == "session.close":
            self.incoming.put_nowait({"type": "session.closed"})

    async def close(self):
        self.closed = True


class FakeLiveProvider:
    def __init__(self):
        self.calls = 0
        self.failure = None
        self.socket = FakeSocket()
        self.attach_failure = None
        self.attached = False

    async def attach(self, session_id):
        assert session_id == SESSION
        if self.attach_failure:
            raise self.attach_failure
        self.attached = True
        return self.socket

    async def create(self, sdp):
        assert TOKEN not in sdp and KEY not in sdp
        self.calls += 1
        if self.failure:
            raise self.failure
        return LiveSessionResponse(sessionId=SESSION, sdp=SDP)


def live_app(settings, transport, model=None):
    app = create_app(settings, transport=transport)
    provider = FakeLiveProvider()
    app.dependency_overrides[get_live_provider] = lambda: provider
    app.dependency_overrides[get_live_settings] = lambda: LIVE_SETTINGS
    model = model or FakeModel(final())
    app.dependency_overrides[get_agent_model] = lambda: model
    return app, provider, model


def create(api, **extra):
    return api.post(BASE + "/session", headers=AUTH, json={"sdp": SDP, **extra})


def delegate(api, **extra):
    return api.post(
        BASE + "/delegate",
        headers=AUTH,
        json={
            "sessionId": SESSION,
            "callId": "call_1",
            "question": "¿Cómo va Casa Ximena?",
            **extra,
        },
    )


@pytest.mark.parametrize(
    "path,body",
    [
        ("session", {"sdp": SDP}),
        ("close", {"sessionId": SESSION}),
    ],
)
@pytest.mark.parametrize(
    "management,headers,status",
    [
        (True, {}, 401),
        (False, AUTH, 403),
    ],
)
def test_guard_before_live_or_agent(
    settings, mock_backend, payloads, path, body, management, headers, status
):
    payloads["/api/auth/me"]["isManagement"] = management
    transport, _ = mock_backend
    app, provider, model = live_app(settings, transport)
    with TestClient(app) as api:
        response = api.post(f"{BASE}/{path}", headers=headers, json=body)
    assert response.status_code == status
    assert provider.calls == 0 and not model.inputs
    assert TOKEN not in response.text


def test_create_safe_contract_registry_close_and_logs(settings, mock_backend, caplog, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    transport, calls = mock_backend
    app, provider, model = live_app(settings, transport)
    with TestClient(app) as api:
        response = create(api)
        assert response.status_code == 201
        assert provider.attached
        assert response.json() == {"sessionId": SESSION, "sdp": SDP}
        assert response.headers["cache-control"] == "no-store"
        entry = app.state.live_registry.owned(SESSION, "manager")
        assert not hasattr(entry, "sdp") and not hasattr(entry, "token")
        assert provider.calls == 1 and not model.inputs
        result = api.post(BASE + "/close", headers=AUTH, json={"sessionId": SESSION})
        assert result.json() == {"closed": True}
        assert delegate(api).status_code == 404
        registry = app.state.live_registry
    assert registry._entries == {}
    assert all(c["method"] == "GET" for c in calls)
    for secret in (TOKEN, KEY, SDP, SESSION, "manager", "¿Cómo va Casa Ximena?"):
        assert secret not in caplog.text
    events = [json.loads(r.message) for r in caplog.records if r.name == "sng_intelligence"]
    assert any(e.get("live_event") == "created" for e in events)
    assert any(
        e.get("live_event") == "session_closed" and e["live_duration_seconds"] >= 0 for e in events
    )


@pytest.mark.parametrize(
    "sdp",
    [
        "",
        "https://evil.test",
        "v=1\r\n",
        SDP + "x=\x00\r\n",
        SDP.replace("m=audio", "m=video"),
        SDP * 200,
    ],
    ids=["empty", "url", "version", "nul", "no-audio", "oversize"],
)
def test_invalid_sdp(settings, mock_backend, sdp):
    app, provider, _ = live_app(settings, mock_backend[0])
    with TestClient(app) as api:
        response = create(api, sdp=sdp)
    assert response.status_code in (413, 422)
    assert set(response.json()) == {"error", "requestId"}
    assert not provider.calls


@pytest.mark.parametrize("path", ["session", "close"])
def test_body_limit_before_deserialization(settings, mock_backend, path):
    app, provider, _ = live_app(settings, mock_backend[0])
    with TestClient(app) as api:
        response = api.post(
            f"{BASE}/{path}",
            headers={**AUTH, "Content-Type": "application/json"},
            content=b"x" * 66000,
        )
    assert response.status_code == 413 and not provider.calls


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "arbitrary"),
        ("store", True),
        ("tools", []),
        ("voice", "custom"),
        ("subject", "fake"),
    ],
)
def test_no_caller_session_configuration(settings, mock_backend, field, value):
    app, provider, _ = live_app(settings, mock_backend[0])
    with TestClient(app) as api:
        assert create(api, **{field: value}).status_code == 422
    assert not provider.calls


@pytest.mark.parametrize("secret", [TOKEN, KEY])
def test_sdp_credentials_never_reach_provider(settings, mock_backend, monkeypatch, secret):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    app, provider, _ = live_app(settings, mock_backend[0])
    with TestClient(app) as api:
        response = create(api, sdp=SDP + f"a=private:{secret}\r\n")
    assert response.status_code == 422 and not provider.calls
    assert secret not in response.text


@pytest.mark.parametrize("error,status", [(LiveProviderError(), 502), (ProviderTimeout(), 504)])
def test_creation_failure_releases_capacity(settings, mock_backend, error, status):
    app, provider, _ = live_app(settings, mock_backend[0])
    provider.failure = error
    with TestClient(app) as api:
        assert create(api).status_code == status
        assert not app.state.live_registry._pending
        assert not app.state.live_registry._entries


@pytest.mark.parametrize("path", ["close"])
def test_foreign_management_subject_rejected(settings, mock_backend, payloads, path):
    app, _, model = live_app(settings, mock_backend[0])
    with TestClient(app) as api:
        create(api)
        payloads["/api/auth/me"]["subject"] = "different-manager"
        response = (
            delegate(api)
            if path == "delegate"
            else api.post(BASE + "/close", headers=AUTH, json={"sessionId": SESSION})
        )
        assert response.status_code == 403
        assert app.state.live_registry.owned(SESSION, "manager")
    assert not model.inputs


def test_live_optional_configuration_health_and_fallback_contracts(
    settings, mock_backend, monkeypatch
):
    for key in (
        "SNG_INTELLIGENCE_LIVE_MODEL",
        "SNG_INTELLIGENCE_LIVE_VOICE",
        "SNG_INTELLIGENCE_LIVE_DELEGATION_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    with TestClient(create_app(settings, transport=mock_backend[0])) as api:
        assert api.get("/health").status_code == 200
        assert create(api).json()["error"] == "live_not_configured"
        schema = api.get("/openapi.json").json()
    for path in (
        "/voice",
        "/voice/transcribe",
        "/voice/speech",
        "/live/session",
        "/live/close",
    ):
        assert "post" in schema["paths"]["/api/intelligence" + path]


@pytest.mark.anyio
async def test_create_cancellation_releases_reservation():
    from app.live.registry import LiveRegistry

    registry = LiveRegistry()

    async def work():
        with registry.reserve("manager"):
            await asyncio.sleep(60)

    task = asyncio.create_task(work())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert registry._pending == {}


@pytest.mark.parametrize(
    "content,media,status",
    [
        (b"{invalid", "application/json", 422),
        (b"{}", "text/plain", 415),
    ],
)
def test_invalid_json_and_media_are_safe(settings, mock_backend, content, media, status):
    app, provider, _ = live_app(settings, mock_backend[0])
    with TestClient(app) as api:
        response = api.post(
            BASE + "/session", content=content, headers={**AUTH, "Content-Type": media}
        )
        assert response.status_code == status
        assert set(response.json()) == {"error", "requestId"}
    assert not provider.calls


@pytest.mark.parametrize("secret", [TOKEN, KEY])
def test_provider_cannot_echo_credentials_to_browser(settings, mock_backend, monkeypatch, secret):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)

    class EchoProvider(FakeLiveProvider):
        async def create(self, sdp):
            return LiveSessionResponse(sessionId=SESSION, sdp=SDP + f"a=private:{secret}\r\n")

    app, _, _ = live_app(settings, mock_backend[0])
    app.dependency_overrides[get_live_provider] = EchoProvider
    with TestClient(app) as api:
        result = create(api)
        assert result.status_code == 422 and secret not in result.text
        assert app.state.live_registry._entries == {}
        assert app.state.live_registry._pending == {}

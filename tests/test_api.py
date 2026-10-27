import json
import logging
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import AUTH, TOKEN
from fastapi.testclient import TestClient

from app.main import create_app


def test_health_is_public_and_offline(settings, mock_backend):
    transport, calls = mock_backend
    with TestClient(create_app(settings, transport=transport)) as api:
        response = api.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert not calls


@pytest.mark.parametrize(
    "authorization",
    [None, "Basic abc", "Bearer", "Bearer ", "Bearer  ", "Bearer a b", "Bearer a\tb", "Bearer a,b"],
)
def test_invalid_bearer_never_calls_backend(settings, mock_backend, authorization):
    transport, calls = mock_backend
    with TestClient(create_app(settings, transport=transport)) as api:
        headers = {} if authorization is None else {"Authorization": authorization}
        response = api.get("/api/intelligence/session", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert not calls


def test_duplicate_authorization_rejected(settings, mock_backend):
    transport, calls = mock_backend
    with TestClient(create_app(settings, transport=transport)) as api:
        response = api.get(
            "/api/intelligence/session",
            headers=[
                ("Authorization", f"Bearer {TOKEN}"),
                ("Authorization", "Bearer other"),
            ],
        )
    assert response.status_code == 401
    assert not calls


@pytest.mark.parametrize(
    "management,expected", [(True, 200), (False, 403), ("true", 502), (1, 502), (None, 502)]
)
def test_management_is_backend_strict_boolean(settings, management, expected, caplog):
    def backend(request):
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(200, json={**AUTH, "isManagement": management})

    caplog.set_level(logging.INFO, logger="sng_intelligence")
    with TestClient(create_app(settings, transport=httpx.MockTransport(backend))) as api:
        response = api.request(
            "GET",
            "/api/intelligence/session?isManagement=true",
            json={
                "isManagement": True,
                "subject": "forged",
                "email": "forged@example.test",
            },
            headers={"Authorization": f"bEaReR {TOKEN}"},
        )
    assert response.status_code == expected
    assert TOKEN not in response.text + repr(response.headers) + caplog.text
    assert "subject" not in response.text
    if expected == 200:
        assert response.json() == {"authenticated": True, "isManagement": True}


@pytest.mark.parametrize(
    "backend_status,expected",
    [(401, 401), (403, 403), (404, 404), (500, 502), (503, 502), (429, 503), (302, 502)],
)
def test_backend_status_is_safe(settings, backend_status, expected, caplog):
    calls = []

    def backend(request):
        calls.append(request.url.host)
        return httpx.Response(
            backend_status,
            text=f"Authorization: {TOKEN} stack trace",
            headers={
                "Location": "https://evil.test/",
                "X-Private": TOKEN,
            },
        )

    caplog.set_level(logging.DEBUG)
    with TestClient(create_app(settings, transport=httpx.MockTransport(backend))) as api:
        response = api.get(
            "/api/intelligence/session", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert response.status_code == expected
    assert TOKEN not in response.text + repr(response.headers) + caplog.text
    assert calls == ["control.test"]


@pytest.mark.parametrize(
    "error_type,expected", [(httpx.ReadTimeout, 504), (httpx.ConnectError, 503)]
)
def test_safe_network_errors(settings, error_type, expected, caplog):
    def backend(request):
        raise error_type(f"secret={TOKEN}", request=request)

    caplog.set_level(logging.DEBUG)
    with TestClient(create_app(settings, transport=httpx.MockTransport(backend))) as api:
        response = api.get(
            "/api/intelligence/session", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert response.status_code == expected
    assert TOKEN not in response.text + caplog.text


@pytest.mark.parametrize("body", [b"not-json", b"null", b"[]", b"{}", b'{"isManagement":true}'])
def test_invalid_auth_contract(settings, body):
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    with TestClient(create_app(settings, transport=transport)) as api:
        response = api.get(
            "/api/intelligence/session", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert response.status_code == 502


@pytest.mark.parametrize("supplied", [str(uuid4()), "untrusted-token-like-data", "", "../invalid"])
def test_correlation_is_uuid_and_propagated(settings, mock_backend, supplied, caplog):
    transport, calls = mock_backend
    caplog.set_level(logging.INFO, logger="sng_intelligence")
    with TestClient(create_app(settings, transport=transport)) as api:
        response = api.get(
            "/api/intelligence/session",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "X-Request-ID": supplied,
            },
        )
    identifier = response.headers["x-request-id"]
    UUID(identifier)
    assert calls[0]["request_id"] == identifier
    assert response.headers["cache-control"] == "no-store"
    events = [json.loads(r.message) for r in caplog.records if r.name == "sng_intelligence"]
    event = next(row for row in events if row.get("operation") == "session")
    assert event["request_id"] == identifier
    assert event["status"] == 200
    assert event["operation"] == "session"


def test_only_explicit_routes_no_cors_or_proxy(settings, mock_backend):
    transport, calls = mock_backend
    app = create_app(settings, transport=transport)
    assert {
        (path, method) for path, methods in app.openapi()["paths"].items() for method in methods
    } == {
        ("/health", "get"),
        ("/readiness", "get"),
        ("/api/intelligence/session", "get"),
        ("/api/intelligence/chat", "post"),
        ("/api/intelligence/conversation", "post"),
        ("/api/intelligence/conversation/resume", "post"),
        ("/api/intelligence/conversation/close", "post"),
        ("/api/intelligence/voice", "post"),
        ("/api/intelligence/voice/transcribe", "post"),
        ("/api/intelligence/voice/speech", "post"),
        ("/api/intelligence/live/session", "post"),
        ("/api/intelligence/live/close", "post"),
    }
    with TestClient(app) as api:
        for path in [
            "/api/proxy",
            "/api/tools/request",
            "/api/tools/arbitrary-call",
        ]:
            assert api.get(path, params={"url": "https://evil.test"}).status_code == 404
        for method in ["POST", "PUT", "PATCH", "DELETE"]:
            assert api.request(method, "/api/intelligence/session").status_code == 405
        response = api.options(
            "/api/intelligence/session",
            headers={"Origin": "https://evil.test", "Access-Control-Request-Method": "GET"},
        )
        assert "access-control-allow-origin" not in response.headers
    assert not calls

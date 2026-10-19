import asyncio
from uuid import uuid4

import httpx
import pytest
from conftest import AUTH, PROJECT_ID, TOKEN
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api import dependencies
from app.clients.sng_control import SnGControlClient
from app.clients.transport import create_http_client
from app.core.errors import AuthenticationRequired, OutboundPolicyError
from app.main import create_app
from app.tools import ReadTools


@pytest.mark.anyio
@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/management/overview"),
        ("POST", "/api/projects"),
        ("PUT", f"/api/projects/{PROJECT_ID}"),
        ("PATCH", f"/api/projects/{PROJECT_ID}"),
        ("DELETE", f"/api/projects/{PROJECT_ID}/documents"),
        ("GET", f"/api/projects/{PROJECT_ID}/documents/file"),
        ("GET", f"/api/projects/{PROJECT_ID}/documents/current.zip"),
        ("GET", f"/api/projects/{PROJECT_ID}/documents/initial-handoff.zip"),
        ("GET", "/api/auth/me?url=https://evil.test"),
        ("GET", "https://evil.test/api/auth/me"),
        ("GET", "http://control.test/api/auth/me"),
        ("GET", "https://control.test:444/api/auth/me"),
        ("GET", "https://user:secret@control.test/api/auth/me"),
        ("GET", "/api/projects/not-a-uuid"),
        ("GET", "/api/auth/%6de"),
    ],
)
async def test_transport_rejects_writes_files_queries_and_other_origins(mock_backend, method, path):
    transport, calls = mock_backend
    async with create_http_client("https://control.test", 7, transport) as http:
        with pytest.raises(OutboundPolicyError):
            await http.request(method, path)
    assert not calls


@pytest.mark.anyio
async def test_no_shared_credentials_or_cookie_persistence():
    observed = []
    ready = asyncio.Event()

    async def backend(request):
        observed.append((request.headers.get("authorization"), request.headers.get("cookie")))
        if len(observed) == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), timeout=2)
        return httpx.Response(200, json=AUTH, headers={"Set-Cookie": f"session={TOKEN}; Path=/"})

    async with create_http_client("https://control.test", 7, httpx.MockTransport(backend)) as http:
        first = SnGControlClient(http, SecretStr("first-token"), uuid4())
        second = SnGControlClient(http, SecretStr("second-token"), uuid4())
        await asyncio.gather(first.get_current_user(), second.get_current_user())
        await second.get_current_user()
        assert not http.cookies
        assert "authorization" not in http.headers
        first.clear_credentials()
        second.clear_credentials()
        with pytest.raises(AuthenticationRequired):
            await first.get_current_user()
        with pytest.raises(AuthenticationRequired):
            await second.get_current_user()
        assert "first-token" not in repr(first.__dict__)
        assert "second-token" not in repr(second.__dict__)
    assert observed == [
        ("Bearer first-token", None),
        ("Bearer second-token", None),
        ("Bearer second-token", None),
    ]


@pytest.mark.parametrize("backend_status", [200, 401, 500])
def test_dependency_clears_credentials_and_lifespan_closes_pool(
    settings, monkeypatch, backend_status
):
    captured = []
    original = dependencies.SnGControlClient

    def capture(*args):
        client = original(*args)
        captured.append(client)
        return client

    monkeypatch.setattr(dependencies, "SnGControlClient", capture)
    transport = httpx.MockTransport(lambda _: httpx.Response(backend_status, json=AUTH))
    application = create_app(settings, transport=transport)
    with TestClient(application) as api:
        pool = application.state.control_http
        response = api.get(
            "/api/intelligence/session", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status_code in {200, 401, 502}
        assert captured[0]._token is None
        assert TOKEN not in repr(captured[0].__dict__)
        assert "authorization" not in pool.headers
        assert not pool.cookies
        assert not pool.is_closed
    assert pool.is_closed
    assert not hasattr(application.state, "control_http")


def test_public_client_and_tool_surfaces_are_closed():
    assert {name for name in dir(ReadTools) if not name.startswith("_")} == {
        "get_management_snapshot",
        "compare_projects",
        "get_management_overview",
        "list_management_tasks",
        "get_project_details",
        "get_project_documents",
        "get_project_activity",
        "find_projects",
    }
    assert {name for name in dir(SnGControlClient) if not name.startswith("_")} == {
        "get_current_user",
        "get_management_overview",
        "list_management_tasks",
        "get_project",
        "list_projects",
        "list_project_tasks",
        "list_project_documents",
        "list_project_notifications",
        "clear_credentials",
        "download_project_document",
    }


def test_logs_never_record_raw_path_query_or_headers(settings, mock_backend, caplog):
    transport, _ = mock_backend
    caplog.set_level("INFO", logger="sng_intelligence")
    with TestClient(create_app(settings, transport=transport)) as api:
        api.get(
            f"/unknown/{TOKEN}?secret={TOKEN}",
            headers={"Authorization": f"Bearer {TOKEN}", "X-Request-ID": TOKEN},
        )
    own_logs = "\n".join(
        record.message for record in caplog.records if record.name == "sng_intelligence"
    )
    assert TOKEN not in own_logs
    assert "unmatched" in own_logs


@pytest.mark.anyio
async def test_explicit_timeout_reaches_transport():
    async def backend(request):
        assert request.extensions["timeout"] == {
            "connect": 1.5,
            "read": 1.5,
            "write": 1.5,
            "pool": 1.5,
        }
        return httpx.Response(200, json=AUTH)

    async with create_http_client(
        "https://control.test", 1.5, httpx.MockTransport(backend)
    ) as http:
        await SnGControlClient(http, SecretStr(TOKEN), uuid4()).get_current_user()

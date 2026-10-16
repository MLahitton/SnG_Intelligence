import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import AUTH, PROJECT_ID, TOKEN
from pydantic import SecretStr

from app.clients.sng_control import SnGControlClient
from app.clients.transport import create_http_client
from app.core.errors import (
    BackendResponseError,
    BackendTimeout,
    InvalidArguments,
    ManagementRequired,
    ResourceNotFound,
)
from app.tools import ReadTools

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "method,suffix",
    [
        ("get_current_user", "/api/auth/me"),
        ("get_management_overview", "/api/management/overview"),
        ("list_management_tasks", "/api/management/tasks"),
        ("get_project", f"/api/projects/{PROJECT_ID}"),
        ("list_project_tasks", f"/api/projects/{PROJECT_ID}/tasks"),
        ("list_project_documents", f"/api/projects/{PROJECT_ID}/documents"),
        ("list_project_notifications", f"/api/projects/{PROJECT_ID}/notifications"),
    ],
)
async def test_exact_client_paths_and_forwarding(mock_backend, method, suffix):
    transport, calls = mock_backend
    async with create_http_client("https://control.test", 7, transport) as http:
        client = SnGControlClient(http, SecretStr(TOKEN), uuid4())
        args = (PROJECT_ID,) if "/projects/" in suffix else ()
        await getattr(client, method)(*args)
        assert "authorization" not in http.headers
    assert [(x["method"], x["path"], x["authorized"]) for x in calls] == [("GET", suffix, True)]


async def test_overview_values_preserved(mock_backend, payloads):
    transport, calls = mock_backend
    async with create_http_client("https://control.test", 7, transport) as http:
        tools = ReadTools(SnGControlClient(http, SecretStr(TOKEN), uuid4()))
        result = await tools.get_management_overview()
    assert result.model_dump(mode="json", by_alias=True) == payloads["/api/management/overview"]
    assert len(calls) == 2


async def test_tasks_order_nulls_and_unknown_strings_preserved(mock_backend, payloads):
    transport, _ = mock_backend
    async with create_http_client("https://control.test", 7, transport) as http:
        result = await ReadTools(
            SnGControlClient(http, SecretStr(TOKEN), uuid4())
        ).list_management_tasks()
    assert [t.model_dump(mode="json", by_alias=True) for t in result] == payloads[
        "/api/management/tasks"
    ]


async def test_project_details_concurrent_and_no_duplicate_calls(payloads):
    seen = []
    both_started = asyncio.Event()

    async def backend(request):
        path = request.url.path
        if path != "/api/auth/me":
            seen.append(path)
            if len(seen) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=2)
        return httpx.Response(200, json=payloads[path])

    async with create_http_client("https://control.test", 7, httpx.MockTransport(backend)) as http:
        result = await ReadTools(
            SnGControlClient(http, SecretStr(TOKEN), uuid4())
        ).get_project_details(UUID(PROJECT_ID))
    assert (
        result.project.model_dump(mode="json", by_alias=True)
        == payloads[f"/api/projects/{PROJECT_ID}"]
    )
    assert [t.model_dump(mode="json", by_alias=True) for t in result.tasks] == payloads[
        f"/api/projects/{PROJECT_ID}/tasks"
    ]
    assert len(seen) == 2


@pytest.mark.parametrize(
    "method,suffix",
    [("get_project_documents", "documents"), ("get_project_activity", "notifications")],
)
async def test_metadata_and_activity_only(mock_backend, payloads, method, suffix):
    transport, calls = mock_backend
    async with create_http_client("https://control.test", 7, transport) as http:
        result = await getattr(
            ReadTools(SnGControlClient(http, SecretStr(TOKEN), uuid4())), method
        )(PROJECT_ID)
    actual = (
        [r.model_dump(mode="json", by_alias=True) for r in result]
        if isinstance(result, list)
        else result.model_dump(mode="json", by_alias=True)
    )
    assert actual == payloads[f"/api/projects/{PROJECT_ID}/{suffix}"]
    assert [c["path"] for c in calls] == ["/api/auth/me", f"/api/projects/{PROJECT_ID}/{suffix}"]


@pytest.mark.parametrize(
    "project_id",
    [
        "https://evil.test",
        "../",
        "/api/auth/me",
        "?url=evil",
        f"{PROJECT_ID}/documents",
        f"{PROJECT_ID}?a=b",
        f"{PROJECT_ID}#fragment",
        "bad",
        "",
        None,
        123,
    ],
)
@pytest.mark.parametrize(
    "method", ["get_project_details", "get_project_documents", "get_project_activity"]
)
async def test_tools_reject_invalid_id_before_any_io(mock_backend, project_id, method):
    transport, calls = mock_backend
    async with create_http_client("https://control.test", 7, transport) as http:
        tools = ReadTools(SnGControlClient(http, SecretStr(TOKEN), uuid4()))
        with pytest.raises(InvalidArguments) as error:
            await getattr(tools, method)(project_id)
    assert error.value.__context__ is None
    assert not calls


@pytest.mark.parametrize(
    "method",
    ["get_project", "list_project_tasks", "list_project_documents", "list_project_notifications"],
)
async def test_client_rejects_url_directly(mock_backend, method):
    transport, calls = mock_backend
    async with create_http_client("https://control.test", 7, transport) as http:
        with pytest.raises(InvalidArguments):
            await getattr(SnGControlClient(http, SecretStr(TOKEN), uuid4()), method)(
                "https://evil.test"
            )
    assert not calls


@pytest.mark.parametrize(
    "method",
    [
        "get_management_overview",
        "list_management_tasks",
        "get_project_details",
        "get_project_documents",
        "get_project_activity",
    ],
)
async def test_all_tools_enforce_management(mock_backend, payloads, method):
    payloads["/api/auth/me"]["isManagement"] = False
    transport, calls = mock_backend
    async with create_http_client("https://control.test", 7, transport) as http:
        args = (PROJECT_ID,) if method.startswith("get_project") else ()
        with pytest.raises(ManagementRequired):
            await getattr(ReadTools(SnGControlClient(http, SecretStr(TOKEN), uuid4())), method)(
                *args
            )
    assert [c["path"] for c in calls] == ["/api/auth/me"]


async def test_project_404_cancels_sibling():
    sibling_started = asyncio.Event()
    sibling_finished = asyncio.Event()

    async def backend(request):
        if request.url.path == "/api/auth/me":
            return httpx.Response(200, json=AUTH)
        if request.url.path.endswith("/tasks"):
            sibling_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                sibling_finished.set()
        await sibling_started.wait()
        return httpx.Response(404, text=TOKEN)

    async with create_http_client("https://control.test", 7, httpx.MockTransport(backend)) as http:
        with pytest.raises(ResourceNotFound):
            await ReadTools(SnGControlClient(http, SecretStr(TOKEN), uuid4())).get_project_details(
                PROJECT_ID
            )
    assert sibling_finished.is_set()


@pytest.mark.parametrize("body", [b"invalid", b"{}", b"null", b"[]"])
async def test_unexpected_contract_is_safe(body):
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    async with create_http_client("https://control.test", 7, transport) as http:
        with pytest.raises(BackendResponseError) as error:
            await SnGControlClient(http, SecretStr(TOKEN), uuid4()).get_management_overview()
    assert error.value.__context__ is None


async def test_unknown_fields_are_not_forwarded(mock_backend, payloads):
    payloads["/api/management/overview"]["futureSensitiveField"] = TOKEN
    transport, _ = mock_backend
    async with create_http_client("https://control.test", 7, transport) as http:
        result = await SnGControlClient(http, SecretStr(TOKEN), uuid4()).get_management_overview()
    assert TOKEN not in result.model_dump_json()


async def test_exception_request_is_scrubbed_and_not_chained():
    captured = []

    def backend(request):
        captured.append(request)
        raise httpx.ReadTimeout(TOKEN, request=request)

    async with create_http_client("https://control.test", 7, httpx.MockTransport(backend)) as http:
        with pytest.raises(BackendTimeout) as error:
            await SnGControlClient(http, SecretStr(TOKEN), uuid4()).get_current_user()
    assert error.value.__context__ is None
    assert error.value.__cause__ is None
    assert TOKEN not in str(error.value)
    assert "authorization" not in captured[0].headers

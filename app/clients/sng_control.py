from time import perf_counter
from uuid import UUID

import httpx
from pydantic import SecretStr, TypeAdapter, ValidationError

from app.contracts.auth import CurrentUser
from app.contracts.documents import ProjectDocuments
from app.contracts.management import ManagementOverview, ManagementTask
from app.contracts.notifications import ProjectNotification
from app.contracts.projects import Project, ProjectTask
from app.core.errors import (
    AuthenticationRequired,
    BackendResponseError,
    BackendTimeout,
    BackendUnavailable,
    IntelligenceError,
    InvalidArguments,
    ManagementRequired,
    ResourceNotFound,
)
from app.core.latency import BackendOperation, backend_timing


def validate_project_id(project_id: UUID | str) -> UUID:
    if isinstance(project_id, UUID):
        return project_id
    if isinstance(project_id, str):
        try:
            return UUID(project_id)
        except ValueError:
            pass
    raise InvalidArguments()


class SnGControlClient:
    """Request-scoped credential holder over a lifespan-scoped connection pool.

    Never register this object globally or pass it to a model. Dispose in a finally block.
    """

    def __init__(self, http: httpx.AsyncClient, token: SecretStr, request_id: UUID) -> None:
        self._http = http
        self._token: SecretStr | None = token
        self._request_id = request_id

    def clear_credentials(self) -> None:
        self._token = None

    async def _get[T](self, path: str, adapter: TypeAdapter[T], operation: BackendOperation) -> T:
        start = perf_counter()
        status = "cancelled"
        try:
            result = await self._read(path, adapter)
            status = "success"
            return result
        except IntelligenceError as exc:
            status = exc.code
            raise
        except Exception:
            status = "failure"
            raise
        finally:
            backend_timing(operation, (perf_counter() - start) * 1000, status)

    async def _read[T](self, path: str, adapter: TypeAdapter[T]) -> T:
        if self._token is None:
            raise AuthenticationRequired()
        request = self._http.build_request(
            "GET",
            path,
            headers={
                "Authorization": f"Bearer {self._token.get_secret_value()}",
                "X-Request-ID": str(self._request_id),
                "Accept": "application/json",
            },
        )
        error: IntelligenceError | None = None
        response: httpx.Response | None = None
        try:
            response = await self._http.send(request, follow_redirects=False)
        except httpx.TimeoutException:
            error = BackendTimeout()
        except httpx.RequestError:
            error = BackendUnavailable()
        finally:
            # Do not leave credentials in response.request or exception request objects.
            request.headers.pop("Authorization", None)
        if error is not None:
            # Raise outside except so no httpx exception/context can carry credentials.
            raise error
        assert response is not None
        status_errors: dict[int, type[IntelligenceError]] = {
            401: AuthenticationRequired,
            403: ManagementRequired,
            404: ResourceNotFound,
            429: BackendUnavailable,
        }
        if response.status_code in status_errors:
            raise status_errors[response.status_code]()
        if response.status_code != 200:
            raise BackendResponseError()
        try:
            # JSON-mode strict validation accepts JSON UUID/datetime strings, not coercive bools.
            return adapter.validate_json(response.content, strict=True)
        except ValidationError:
            pass
        raise BackendResponseError()

    async def get_current_user(self) -> CurrentUser:
        return await self._get("/api/auth/me", TypeAdapter(CurrentUser), "auth_me")

    async def get_management_overview(self) -> ManagementOverview:
        return await self._get(
            "/api/management/overview", TypeAdapter(ManagementOverview), "management_overview"
        )

    async def list_management_tasks(self) -> list[ManagementTask]:
        return await self._get(
            "/api/management/tasks", TypeAdapter(list[ManagementTask]), "management_tasks"
        )

    async def get_project(self, project_id: UUID | str) -> Project:
        identifier = validate_project_id(project_id)
        return await self._get(
            f"/api/projects/{identifier}", TypeAdapter(Project), "project_details"
        )

    async def list_projects(self) -> list[Project]:
        return await self._get("/api/projects", TypeAdapter(list[Project]), "projects_list")

    async def list_project_tasks(self, project_id: UUID | str) -> list[ProjectTask]:
        identifier = validate_project_id(project_id)
        return await self._get(
            f"/api/projects/{identifier}/tasks", TypeAdapter(list[ProjectTask]), "project_tasks"
        )

    async def list_project_documents(self, project_id: UUID | str) -> ProjectDocuments:
        identifier = validate_project_id(project_id)
        return await self._get(
            f"/api/projects/{identifier}/documents", TypeAdapter(ProjectDocuments), "documents"
        )

    async def list_project_notifications(self, project_id: UUID | str) -> list[ProjectNotification]:
        identifier = validate_project_id(project_id)
        return await self._get(
            f"/api/projects/{identifier}/notifications",
            TypeAdapter(list[ProjectNotification]),
            "notifications",
        )

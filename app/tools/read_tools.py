import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Never
from uuid import UUID

from app.auth.management_guard import require_management
from app.clients.sng_control import SnGControlClient, validate_project_id
from app.contracts.documents import ProjectDocuments
from app.contracts.management import ManagementOverview, ManagementTask
from app.contracts.notifications import ProjectNotification
from app.contracts.projects import ProjectCandidate, ProjectDetails
from app.core.errors import InvalidArguments, ManagementRequired
from app.core.latency import measure


class ReadTools:
    """Internal request-scoped tools. Their arguments contain no identity or credentials.

    Default calls check Management individually. Live may use a validated, task-bound run scope.
    """

    def __init__(self, client: SnGControlClient) -> None:
        self._client = client
        self._run_task: asyncio.Task[object] | None = None
        self._run_scoped = False
        self._active = True

    @classmethod
    @asynccontextmanager
    async def _authorized_run(
        cls, client: SnGControlClient, expected_subject: str
    ) -> AsyncIterator["ReadTools"]:
        # No caller-supplied boolean can skip auth. This factory always revalidates.
        with measure("management_revalidation_ms"):
            identity = await require_management(client)
            if identity.subject != expected_subject:
                raise ManagementRequired()
        tools = cls(client)
        tools._run_scoped = True
        tools._run_task = asyncio.current_task()
        try:
            yield tools
        finally:
            tools._active = False
            tools._run_task = None

    async def _authorize(self) -> None:
        if self._run_scoped:
            if not self._active or asyncio.current_task() is not self._run_task:
                raise ManagementRequired()
        else:
            await require_management(self._client)

    def __reduce__(self) -> Never:
        raise TypeError("Read tools cannot be serialized")

    async def get_management_overview(self) -> ManagementOverview:
        await self._authorize()
        return await self._client.get_management_overview()

    async def list_management_tasks(self) -> list[ManagementTask]:
        await self._authorize()
        return await self._client.list_management_tasks()

    async def get_project_details(self, project_id: UUID | str) -> ProjectDetails:
        identifier = validate_project_id(project_id)
        await self._authorize()
        # Cancel and await the sibling on failure; nothing outlives the request scope.
        project = asyncio.create_task(self._client.get_project(identifier))
        tasks = asyncio.create_task(self._client.list_project_tasks(identifier))
        try:
            project_result, tasks_result = await asyncio.gather(project, tasks)
            return ProjectDetails(project=project_result, tasks=tasks_result)
        finally:
            for pending in (project, tasks):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(project, tasks, return_exceptions=True)

    async def get_project_documents(self, project_id: UUID | str) -> ProjectDocuments:
        identifier = validate_project_id(project_id)
        await self._authorize()
        return await self._client.list_project_documents(identifier)

    async def get_project_activity(self, project_id: UUID | str) -> list[ProjectNotification]:
        identifier = validate_project_id(project_id)
        await self._authorize()
        return await self._client.list_project_notifications(identifier)

    async def find_projects(self, query: str) -> list[ProjectCandidate]:
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise InvalidArguments()
        terms = query.casefold().split()
        await self._authorize()
        projects_read = asyncio.create_task(self._client.list_projects())
        tasks_read = asyncio.create_task(self._client.list_management_tasks())
        try:
            projects, tasks = await asyncio.gather(projects_read, tasks_read)
        finally:
            for pending in (projects_read, tasks_read):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(projects_read, tasks_read, return_exceptions=True)
        # Client names are available only for projects present in management tasks.
        clients = {task.project_id: task.client_name for task in tasks}
        candidates = []
        for project in projects:
            name = clients.get(project.id)
            fields = (project.name, project.sg_code or "", project.city, name or "")
            searchable = " ".join(" ".join(field.casefold().split()) for field in fields)
            if all(term in searchable for term in terms):
                candidates.append(
                    ProjectCandidate(
                        project_id=project.id,
                        project_name=project.name,
                        client_name=name,
                        sg_code=project.sg_code,
                        city=project.city,
                    )
                )
        normalized = " ".join(terms)
        exact_names = [
            candidate
            for candidate in candidates
            if " ".join(candidate.project_name.casefold().split()) == normalized
        ]
        if exact_names:
            # Exact identity is stronger than partial/auxiliary matches. Duplicate exact
            # names remain ambiguous; never choose an arbitrary candidate within the tier.
            return exact_names

        def priority(candidate: ProjectCandidate) -> int:
            name = " ".join(candidate.project_name.casefold().split())
            if name == normalized:
                return 0
            if all(term in name for term in terms):
                return 1
            auxiliary = f"{candidate.client_name or ''} {candidate.city}".casefold()
            if all(term in auxiliary for term in terms):
                return 2
            return 3  # SG remains a supported alternative.

        return sorted(candidates, key=priority)  # Stable within a tier; no first-result choice.

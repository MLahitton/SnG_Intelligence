import asyncio
from uuid import UUID

from app.auth.management_guard import require_management
from app.clients.sng_control import SnGControlClient, validate_project_id
from app.contracts.documents import ProjectDocuments
from app.contracts.management import ManagementOverview, ManagementTask
from app.contracts.notifications import ProjectNotification
from app.contracts.projects import ProjectCandidate, ProjectDetails
from app.core.errors import InvalidArguments


class ReadTools:
    """Internal request-scoped tools. Their arguments contain no identity or credentials.

    Every invocation checks management, including calls outside FastAPI dependency injection.
    """

    def __init__(self, client: SnGControlClient) -> None:
        self._client = client

    async def get_management_overview(self) -> ManagementOverview:
        await require_management(self._client)
        return await self._client.get_management_overview()

    async def list_management_tasks(self) -> list[ManagementTask]:
        await require_management(self._client)
        return await self._client.list_management_tasks()

    async def get_project_details(self, project_id: UUID | str) -> ProjectDetails:
        identifier = validate_project_id(project_id)
        await require_management(self._client)
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
        await require_management(self._client)
        return await self._client.list_project_documents(identifier)

    async def get_project_activity(self, project_id: UUID | str) -> list[ProjectNotification]:
        identifier = validate_project_id(project_id)
        await require_management(self._client)
        return await self._client.list_project_notifications(identifier)

    async def find_projects(self, query: str) -> list[ProjectCandidate]:
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise InvalidArguments()
        terms = query.casefold().split()
        await require_management(self._client)
        projects = await self._client.list_projects()
        tasks = await self._client.list_management_tasks()
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
        return candidates

from datetime import datetime
from uuid import UUID

from pydantic import ConfigDict

from app.contracts.base import BackendContract


class ProjectCreator(BackendContract):
    subject: str
    email: str | None
    name: str | None


class Project(BackendContract):
    id: UUID
    client_id: UUID
    name: str
    city: str
    product_line: str
    sg_code: str | None
    created_at: datetime
    updated_at: datetime
    created_by: ProjectCreator | None


class ProjectTask(BackendContract):
    id: UUID
    project_id: UUID
    area: str
    type: str
    status: str
    assigned_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    assigned_to_google_subject: str | None
    deadline_at: datetime | None
    was_overdue: bool | None
    leak_time_status: str


class ProjectDetails(BackendContract):
    project: Project
    tasks: list[ProjectTask]


class ProjectCandidate(BackendContract):
    model_config = ConfigDict(populate_by_name=True)
    project_id: UUID
    project_name: str
    client_name: str | None
    sg_code: str | None
    city: str

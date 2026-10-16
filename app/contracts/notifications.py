from datetime import datetime
from uuid import UUID

from app.contracts.base import BackendContract


class ProjectNotification(BackendContract):
    id: UUID
    project_id: UUID
    project_task_id: UUID
    recipient_google_subject: str
    task_type: str
    kind: str
    state: str
    title: str
    message: str
    scheduled_at: datetime
    created_at: datetime
    resolved_at: datetime | None
    deadline_at: datetime | None

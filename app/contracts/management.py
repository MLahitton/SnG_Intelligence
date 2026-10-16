from datetime import datetime
from uuid import UUID

from app.contracts.base import BackendContract


class TaskCounts(BackendContract):
    pending: int
    in_progress: int
    completed: int


class LeakTimeCounts(BackendContract):
    not_started: int
    on_track: int
    due_soon: int
    due_today: int
    overdue: int
    completed_on_time: int
    completed_late: int
    untracked_legacy: int


class AreaSummary(TaskCounts):
    area: str
    overdue: int
    due_today: int
    due_soon: int


class AttentionItem(BackendContract):
    task_id: UUID
    project_id: UUID
    project_name: str
    client_name: str
    sg_code: str | None
    area: str
    task_type: str
    task_status: str
    leak_time_status: str
    deadline_at: datetime | None
    started_at: datetime | None


class Attention(BackendContract):
    total: int
    items: list[AttentionItem]


class ManagementOverview(BackendContract):
    total_projects: int
    tasks: TaskCounts
    leak_time: LeakTimeCounts
    areas: list[AreaSummary]
    attention: Attention


class ManagementTask(BackendContract):
    task_id: UUID
    project_id: UUID
    project_name: str
    client_id: UUID
    client_name: str
    sg_code: str | None
    city: str
    product_line: str
    area: str
    type: str
    status: str
    assigned_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    deadline_at: datetime | None
    was_overdue: bool | None
    leak_time_status: str

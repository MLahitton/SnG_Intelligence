from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.contracts.base import BackendContract


class TaskCounts(BackendContract):
    pending: int
    in_progress: int
    completed: int


class LeadTimeCounts(BackendContract):
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
    lead_time_status: str
    deadline_at: datetime | None
    started_at: datetime | None


class Attention(BackendContract):
    total: int
    items: list[AttentionItem]


class ManagementOverview(BackendContract):
    total_projects: int
    tasks: TaskCounts
    lead_time: LeadTimeCounts
    areas: list[AreaSummary]
    attention: Attention


class ManagementTask(BackendContract):
    quotation_round: int = Field(gt=0)
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
    lead_time_status: str

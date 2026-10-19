"""Closed public presentation contracts, independent of any model provider."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

ResponseType = Literal[
    "direct",
    "management_brief",
    "project_status",
    "list",
    "comparison",
    "clarification",
    "unavailable",
    "document_analysis",
]


class PresentationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        hide_input_in_errors=True,
        alias_generator=to_camel,
        populate_by_name=True,
    )


class Metric(PresentationModel):
    key: str
    label: str
    value: int


class ProjectItem(PresentationModel):
    kind: Literal["project"] = "project"
    project_id: UUID
    project_name: str
    sg_code: str | None
    client_name: str | None
    city: str | None


class TaskItem(PresentationModel):
    kind: Literal["task"] = "task"
    project_id: UUID
    project_name: str
    sg_code: str | None
    task_id: UUID
    area: str | None
    task: str | None
    status: str | None
    leak_time_status: str | None
    deadline_at: datetime | None


class AreaItem(PresentationModel):
    area: str | None
    pending: int
    in_progress: int
    completed: int
    overdue: int
    due_today: int
    due_soon: int


class DocumentSource(PresentationModel):
    kind: Literal["document"] = "document"
    document_id: UUID
    project_id: UUID
    filename: str
    size_bytes: int
    uploaded_at: datetime
    is_current: bool
    in_initial_handoff: bool
    replaced_by_document_id: UUID | None


EntityItem = Annotated[ProjectItem | TaskItem | DocumentSource, Field(discriminator="kind")]


class DirectPresentation(PresentationModel):
    kind: Literal["direct"] = "direct"
    metric: Metric | None


class Coverage(PresentationModel):
    total: int = Field(ge=0)
    returned: int = Field(ge=0)
    has_more: bool


class BriefPresentation(PresentationModel):
    kind: Literal["management_brief"] = "management_brief"
    metrics: list[Metric]
    items: list[EntityItem]
    areas: list[AreaItem] = Field(default_factory=list)
    attention_coverage: Coverage | None = None
    tasks_coverage: Coverage | None = None


class ProjectPresentation(PresentationModel):
    kind: Literal["project_status"] = "project_status"
    project: ProjectItem
    tasks: list[TaskItem]


class ListPresentation(PresentationModel):
    kind: Literal["list"] = "list"
    items: list[EntityItem]


class ComparedProjectItem(PresentationModel):
    project: ProjectItem
    metrics: list[Metric]
    tasks: list[TaskItem]


class ComparisonPresentation(PresentationModel):
    kind: Literal["comparison"] = "comparison"
    areas: list[AreaItem] = Field(default_factory=list)
    projects: list[ComparedProjectItem] = Field(default_factory=list, max_length=4)


class ClarificationPresentation(PresentationModel):
    kind: Literal["clarification"] = "clarification"
    candidates: list[ProjectItem]
    documents: list[DocumentSource] = Field(default_factory=list)


class DocumentAnalysisPresentation(PresentationModel):
    kind: Literal["document_analysis"] = "document_analysis"
    project: ProjectItem
    related_projects: list[ProjectItem] = Field(default_factory=list, max_length=3)
    sources: list[DocumentSource] = Field(min_length=1, max_length=4)


class UnavailablePresentation(PresentationModel):
    kind: Literal["unavailable"] = "unavailable"
    reason: Literal["data_not_available"] = "data_not_available"


# Keep the union explicit so OpenAPI and all response consumers see the new variant.
Presentation = Annotated[
    DirectPresentation
    | BriefPresentation
    | ProjectPresentation
    | ListPresentation
    | ComparisonPresentation
    | ClarificationPresentation
    | UnavailablePresentation
    | DocumentAnalysisPresentation,
    Field(discriminator="kind"),
]

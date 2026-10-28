from datetime import datetime
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

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
    quotation_round: int | None = Field(gt=0)
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
    lead_time_status: str

    @model_validator(mode="after")
    def quotation_round_required(self) -> "ProjectTask":
        if (
            self.type in {"PrepareQuotation", "ReviewQuotation", "PresentQuotationToClient"}
            and self.quotation_round is None
        ):
            raise ValueError("Quotation task requires a round")
        if self.type == "RegisterInitialDeposit" and (
            self.quotation_round is not None
            or self.area != "Accounting"
            or self.deadline_at is not None
            or self.was_overdue is not None
        ):
            raise ValueError("Accounting task has no quotation round or SLA")
        return self


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

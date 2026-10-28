from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from app.contracts.projects import ProjectTask
from app.contracts.quotations import Price, QuotationContract


class RoundTask(ProjectTask):
    model_config = ConfigDict(extra="forbid")


class RoundBudget(QuotationContract):
    version_number: int = Field(gt=0)
    confirmed_at: AwareDatetime
    item_count: int = Field(ge=0)
    total_quantity: int = Field(ge=0)


class RoundCommercial(QuotationContract):
    version_number: int = Field(gt=0)
    confirmed_at: AwareDatetime
    project_total: Price
    currency_code: Literal["COP"]


class RoundDecision(QuotationContract):
    decision: Literal["Approved", "ChangesRequested", "Rejected"]
    note: str | None = Field(max_length=2000)
    recorded_at: AwareDatetime


class RoundRequest(QuotationContract):
    target_quotation_round: int = Field(gt=1)
    requested_changes: str = Field(min_length=1, max_length=4000)
    requested_at: AwareDatetime

    @model_validator(mode="after")
    def nonblank(self) -> "RoundRequest":
        if not self.requested_changes.strip():
            raise ValueError("Empty formal request")
        return self


class QuotationRound(QuotationContract):
    round_number: int = Field(gt=0)
    prepare_quotation_task: RoundTask | None
    budget_quotation: RoundBudget | None
    review_quotation_task: RoundTask | None
    commercial_quotation: RoundCommercial | None
    present_quotation_task: RoundTask | None
    client_decision: RoundDecision | None
    requotation_request: RoundRequest | None

    @model_validator(mode="after")
    def same_round(self) -> "QuotationRound":
        for task, kind in (
            (self.prepare_quotation_task, "PrepareQuotation"),
            (self.review_quotation_task, "ReviewQuotation"),
            (self.present_quotation_task, "PresentQuotationToClient"),
        ):
            if task and (task.quotation_round != self.round_number or task.type != kind):
                raise ValueError("Task round mismatch")
        for quotation in (self.budget_quotation, self.commercial_quotation):
            if quotation and quotation.version_number != self.round_number:
                raise ValueError("Version mismatch")
        if (
            self.requotation_request
            and self.requotation_request.target_quotation_round != self.round_number
        ):
            raise ValueError("Request round mismatch")
        if self.round_number == 1 and self.requotation_request is not None:
            raise ValueError("Initial round has no requotation request")
        return self


class QuotationRounds(QuotationContract):
    project_id: UUID
    total: int = Field(ge=0)
    returned: int = Field(ge=0, le=20)
    has_more: bool
    rounds: list[QuotationRound] = Field(max_length=20)

    @model_validator(mode="after")
    def coverage(self) -> "QuotationRounds":
        numbers = [r.round_number for r in self.rounds]
        if (
            self.returned != len(numbers)
            or self.returned != min(self.total, 20)
            or self.has_more != (self.total > self.returned)
            or numbers != sorted(set(numbers))
        ):
            raise ValueError("Invalid round coverage")
        for item in self.rounds:
            for task in (
                item.prepare_quotation_task,
                item.review_quotation_task,
                item.present_quotation_task,
            ):
                if task and task.project_id != self.project_id:
                    raise ValueError("Task project mismatch")
        return self

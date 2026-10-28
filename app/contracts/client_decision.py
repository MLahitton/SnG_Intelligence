from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.contracts.quotations import QuotationContract


class ClientQuotationDecision(QuotationContract):
    available: bool
    project_id: UUID
    commercial_quotation_id: UUID | None
    present_quotation_task_id: UUID | None
    decision: Literal["Approved", "ChangesRequested", "Rejected"] | None
    note: str | None = Field(max_length=2000)
    recorded_at: AwareDatetime | None

    @model_validator(mode="after")
    def consistent_availability(self) -> "ClientQuotationDecision":
        required = (
            self.commercial_quotation_id,
            self.present_quotation_task_id,
            self.decision,
            self.recorded_at,
        )
        if self.available:
            if any(value is None for value in required):
                raise ValueError("Incomplete recorded decision")
        elif any(value is not None for value in (*required, self.note)):
            raise ValueError("Unavailable decision contains data")
        return self

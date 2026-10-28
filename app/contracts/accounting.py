from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.contracts.quotations import Price, QuotationContract


class AccountingHandoffState(QuotationContract):
    available: bool
    quotation_round: int | None = Field(gt=0)
    commercial_quotation_id: UUID | None
    sent_at: AwareDatetime | None

    @model_validator(mode="after")
    def consistent(self) -> "AccountingHandoffState":
        values = (self.quotation_round, self.commercial_quotation_id, self.sent_at)
        if (self.available and any(v is None for v in values)) or (
            not self.available and any(v is not None for v in values)
        ):
            raise ValueError("Invalid handoff state")
        return self


class InitialDepositState(QuotationContract):
    available: bool
    amount: Price | None
    currency_code: Literal["COP"]
    received_at: AwareDatetime | None
    recorded_at: AwareDatetime | None
    reference: str | None = Field(max_length=200)
    note: str | None = Field(max_length=2000)

    @model_validator(mode="after")
    def consistent(self) -> "InitialDepositState":
        if self.available:
            if (
                self.amount is None
                or self.amount <= 0
                or self.received_at is None
                or self.recorded_at is None
            ):
                raise ValueError("Incomplete deposit record")
            if self.received_at > self.recorded_at:
                raise ValueError("Receipt after recording")
        elif any(
            v is not None
            for v in (self.amount, self.received_at, self.recorded_at, self.reference, self.note)
        ):
            raise ValueError("Absent deposit contains values")
        return self


class AccountingState(QuotationContract):
    project_id: UUID
    accounting_handoff: AccountingHandoffState
    deposit: InitialDepositState

    @model_validator(mode="after")
    def linked(self) -> "AccountingState":
        if self.deposit.available and not self.accounting_handoff.available:
            raise ValueError("Deposit without handoff")
        return self

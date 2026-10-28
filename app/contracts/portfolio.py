from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.contracts.quotations import Price, QuotationContract, Section


class PortfolioProject(QuotationContract):
    project_id: UUID
    project_name: str
    sg_code: str | None
    client_name: str


class PortfolioItem(PortfolioProject):
    section: Section
    source_row: int = Field(gt=0)
    item_number: str = Field(min_length=1)
    location: str
    system: str
    glass: str
    aluminum_finish: str
    quantity: int = Field(gt=0)
    unit_price: Price
    total_price: Price


class PortfolioProjectRank(PortfolioProject):
    confirmed_at: datetime
    project_total: Price


class PortfolioRanking[T](QuotationContract):
    total: int = Field(ge=0)
    returned: int = Field(ge=0, le=10)
    has_more: bool
    items: list[T] = Field(max_length=10)

    @model_validator(mode="after")
    def consistent_coverage(self) -> "PortfolioRanking[T]":
        if (
            self.returned != len(self.items)
            or self.returned != min(self.total, 10)
            or self.has_more != (self.total > self.returned)
        ):
            raise ValueError("Inconsistent ranking coverage")
        return self


class ManagementQuotationPortfolio(QuotationContract):
    currency_code: Literal["COP"]
    quotation_count: int = Field(ge=0)
    item_count: int = Field(ge=0)
    total_quantity: int = Field(ge=0)
    total_quoted_value: Price
    highest_value_items: PortfolioRanking[PortfolioItem]
    highest_unit_price_items: PortfolioRanking[PortfolioItem]
    highest_value_projects: PortfolioRanking[PortfolioProjectRank]

    @model_validator(mode="after")
    def matching_totals(self) -> "ManagementQuotationPortfolio":
        if (
            self.highest_value_items.total != self.item_count
            or self.highest_unit_price_items.total != self.item_count
            or self.highest_value_projects.total != self.quotation_count
        ):
            raise ValueError("Inconsistent portfolio coverage")
        return self

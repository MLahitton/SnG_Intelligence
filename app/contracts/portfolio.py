from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

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


class MixMeasures(QuotationContract):
    item_count: int = Field(gt=0)
    total_quantity: int = Field(gt=0)
    total_quoted_value: Price


class MixEntry(MixMeasures):
    value: str = Field(min_length=1)

    @field_validator("value")
    @classmethod
    def specified(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Unspecified category")
        return value


class MixCombination(MixMeasures):
    system: str = Field(min_length=1)
    glass: str = Field(min_length=1)

    @field_validator("system", "glass")
    @classmethod
    def specified(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Unspecified combination")
        return value


class MixDimension[T](QuotationContract):
    total_items: int = Field(ge=0)
    specified_items: int = Field(ge=0)
    unspecified_items: int = Field(ge=0)
    by_quantity: PortfolioRanking[T]
    by_item_count: PortfolioRanking[T]
    by_quoted_value: PortfolioRanking[T]

    @model_validator(mode="after")
    def consistent_coverage(self) -> "MixDimension[T]":
        if (
            self.specified_items + self.unspecified_items != self.total_items
            or self.by_quantity.total != self.by_item_count.total
            or self.by_quantity.total != self.by_quoted_value.total
            or self.by_quantity.total > self.specified_items
            or bool(self.by_quantity.total) != bool(self.specified_items)
        ):
            raise ValueError("Inconsistent mix coverage")
        return self


class QuotationMix(QuotationContract):
    systems: MixDimension[MixEntry]
    glass: MixDimension[MixEntry]
    aluminum_finishes: MixDimension[MixEntry]
    system_glass_combinations: MixDimension[MixCombination]


class ManagementQuotationPortfolio(QuotationContract):
    currency_code: Literal["COP"]
    quotation_count: int = Field(ge=0)
    item_count: int = Field(ge=0)
    total_quantity: int = Field(ge=0)
    total_quoted_value: Price
    highest_value_items: PortfolioRanking[PortfolioItem]
    highest_unit_price_items: PortfolioRanking[PortfolioItem]
    highest_value_projects: PortfolioRanking[PortfolioProjectRank]
    mix: QuotationMix

    @model_validator(mode="after")
    def matching_totals(self) -> "ManagementQuotationPortfolio":
        if (
            self.highest_value_items.total != self.item_count
            or self.highest_unit_price_items.total != self.item_count
            or self.highest_value_projects.total != self.quotation_count
            or any(
                d.total_items != self.item_count
                for d in (
                    self.mix.systems,
                    self.mix.glass,
                    self.mix.aluminum_finishes,
                    self.mix.system_glass_combinations,
                )
            )
        ):
            raise ValueError("Inconsistent portfolio coverage")
        return self

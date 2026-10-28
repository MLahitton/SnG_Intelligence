from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field

from app.contracts.base import BackendContract

Section = Literal["Main", "Additional"]
ImageStatus = Literal["Stored", "Missing", "Ambiguous"]
TechnicalField = Literal[
    "ItemNumber",
    "Location",
    "System",
    "Glass",
    "AluminumFinish",
    "LockType",
    "Observations",
    "Width",
    "Height",
    "SquareMeters",
    "Quantity",
]
Measure = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Price = Annotated[Decimal, Field(ge=0, allow_inf_nan=False, max_digits=20, decimal_places=2)]


class QuotationContract(BackendContract):
    model_config = ConfigDict(extra="forbid")


class CatalogItemValues(QuotationContract):
    item_number: str = Field(min_length=1)
    location: str
    system: str
    glass: str
    aluminum_finish: str
    lock_type: str
    observations: str
    width: Measure
    height: Measure
    square_meters: Measure
    quantity: int = Field(gt=0)


class TechnicalDifference(QuotationContract):
    field: TechnicalField
    previous_value: str
    current_value: str


class BudgetQuotationDocument(QuotationContract):
    file_name: str
    client_name: str
    project_name: str
    location: str
    client_line: str
    production_line: str
    prepared_by: str
    sg_code: str


class BudgetQuotationItem(QuotationContract):
    id: UUID
    section: Section
    source_row: int = Field(gt=0)
    confirmed: CatalogItemValues
    extracted: CatalogItemValues | None
    image_status: ImageStatus | None
    has_image: bool
    technical_differences: list[TechnicalDifference]


class ConfirmedBudgetQuotation(QuotationContract):
    project_id: UUID
    quotation_version_id: UUID
    version_number: int = Field(gt=0)
    confirmed_at: datetime
    document: BudgetQuotationDocument
    items: list[BudgetQuotationItem]


class CommercialQuotationItem(QuotationContract):
    section: Section
    source_row: int = Field(gt=0)
    commercial_catalog_item_id: UUID
    values: CatalogItemValues
    unit_price: Price
    total_price: Price
    technical_differences: list[TechnicalDifference]
    image_status: ImageStatus | None
    has_image: bool


class ConfirmedCommercialQuotation(QuotationContract):
    version_number: int = Field(gt=0)
    project_id: UUID
    commercial_quotation_id: UUID
    confirmed_at: datetime
    currency_code: Literal["COP"]
    project_total: Price
    items: list[CommercialQuotationItem]

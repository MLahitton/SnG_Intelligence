from typing import Literal
from uuid import UUID

from pydantic import JsonValue

from app.contracts.quotations import (
    BudgetQuotationItem,
    CommercialQuotationItem,
    ConfirmedBudgetQuotation,
    ConfirmedCommercialQuotation,
)


def quotation_data(
    project_id: UUID,
    source: Literal["budget", "commercial"],
    quotation: ConfirmedBudgetQuotation | ConfirmedCommercialQuotation | None,
) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {
        "projectId": str(project_id),
        "source": source,
        "available": quotation is not None,
    }
    if quotation is None:
        # Absence is not an empty quotation or a zero total.
        return data
    items: list[JsonValue] = []
    total_quantity = 0
    for item in quotation.items:
        values = item.confirmed if isinstance(item, BudgetQuotationItem) else item.values
        projected = values.model_dump(mode="json", by_alias=True)
        projected.update(
            section=item.section,
            sourceRow=item.source_row,
            imageStatus=item.image_status,
            hasImage=item.has_image,
            technicalDifferences=[
                d.model_dump(mode="json", by_alias=True) for d in item.technical_differences
            ],
        )
        if isinstance(item, CommercialQuotationItem):
            # Decimal strings preserve the official precision; no float conversion or totals.
            projected.update(unitPrice=str(item.unit_price), totalPrice=str(item.total_price))
        items.append(projected)
        total_quantity += values.quantity
    data.update(
        versionNumber=quotation.version_number,
        confirmedAt=quotation.confirmed_at.isoformat(),
        itemCount=len(quotation.items),
        totalQuantity=total_quantity,
        items=items,
    )
    if isinstance(quotation, ConfirmedBudgetQuotation):
        data["document"] = quotation.document.model_dump(mode="json", by_alias=True)
        data["versionNumber"] = quotation.version_number
    else:
        data["currencyCode"] = quotation.currency_code
        data["projectTotal"] = str(quotation.project_total)
    return data

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.auth.schemas import Currency, check_supported

# gt=0 rejects zero/negatives; decimal_places=2 rejects 10.001 instead of silently rounding money.
Amount = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]


class ExpenseCreate(BaseModel):
    category_id: UUID
    amount: Amount
    currency: Currency
    description: str = Field("", max_length=500)
    spent_on: date

    _supported = field_validator("currency")(check_supported)


class ExpensePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category_id: UUID | None = None
    amount: Amount | None = None
    currency: Currency | None = None
    description: str | None = Field(None, max_length=500)
    spent_on: date | None = None

    _supported = field_validator("currency")(lambda v: v if v is None else check_supported(v))

    @model_validator(mode="after")
    def _not_empty(self):
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        if any(getattr(self, f) is None for f in self.model_fields_set):
            raise ValueError("fields cannot be null")
        return self


class ExpenseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    category_id: UUID
    amount: Decimal
    currency: str
    description: str
    spent_on: date
    receipt_key: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class ExpensePage(BaseModel):
    items: list[ExpenseOut]
    next_cursor: str | None

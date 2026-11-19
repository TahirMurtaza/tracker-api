from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.deps import CurrentUser, DbSession
from app.reports.service import report_rows

router = APIRouter(prefix="/reports", tags=["reports"])


class CategoryLine(BaseModel):
    category_id: UUID
    name: str
    total: Decimal
    budget: Decimal | None
    percent_used: Decimal | None
    previous_total: Decimal
    change_amount: Decimal
    change_percent: Decimal | None
    unconverted_expenses: int  # expenses with no usable exchange rate (excluded from total)


class MonthlyReport(BaseModel):
    year: int
    month: int
    base_currency: str
    categories: list[CategoryLine]


@router.get("/monthly", response_model=MonthlyReport)
async def monthly(
    user: CurrentUser,
    session: DbSession,
    year: Annotated[int, Query(ge=2000, le=2100)],
    month: Annotated[int, Query(ge=1, le=12)],
):
    rows = await report_rows(session, user.id, user.base_currency, year, month)
    return MonthlyReport(
        year=year, month=month, base_currency=user.base_currency, categories=[CategoryLine(**r.__dict__) for r in rows]
    )

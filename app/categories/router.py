from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from app.deps import CurrentUser, DbSession
from app.errors import AppError
from app.models import Category
from app.reports.service import report_rows

router = APIRouter(prefix="/categories", tags=["categories"])


class CategoryIn(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    monthly_budget: Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)] | None = None


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    monthly_budget: Decimal | None


class CategorySpendOut(BaseModel):
    id: UUID
    name: str
    monthly_budget: Decimal | None
    spent_this_month: Decimal
    percent_used: Decimal | None


@router.post("", status_code=201, response_model=CategoryOut)
async def create_category(body: CategoryIn, user: CurrentUser, session: DbSession):
    cat = Category(user_id=user.id, name=body.name.strip(), monthly_budget=body.monthly_budget)
    session.add(cat)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise AppError(409, "category_exists", "A category with this name already exists") from None
    return cat


@router.get("", response_model=list[CategorySpendOut])
async def list_categories(user: CurrentUser, session: DbSession):
    today = datetime.now(timezone.utc).date()
    rows = await report_rows(session, user.id, user.base_currency, today.year, today.month)
    return [
        CategorySpendOut(
            id=r.category_id, name=r.name, monthly_budget=r.budget, spent_this_month=r.total, percent_used=r.percent_used
        )
        for r in rows
    ]

from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.deps import CurrentUser, DbSession
from app.models import Notification

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    category_id: UUID
    month: date
    level: int
    message: str
    created_at: datetime


class NotificationPage(BaseModel):
    items: list[NotificationOut]
    next_cursor: str | None


@router.get("", response_model=NotificationPage)
async def list_notifications(
    user: CurrentUser, session: DbSession, cursor: int | None = None, limit: Annotated[int, Query(ge=1, le=100)] = 20
):
    stmt = select(Notification).where(Notification.user_id == user.id).order_by(Notification.id.desc()).limit(limit + 1)
    if cursor is not None:
        stmt = stmt.where(Notification.id < cursor)
    rows = (await session.execute(stmt)).scalars().all()
    items = rows[:limit]
    return NotificationPage(items=items, next_cursor=str(items[-1].id) if len(rows) > limit else None)

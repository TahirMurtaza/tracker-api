"""Transactional outbox: producers enqueue in their own transaction; the worker drains it.

Delivery is at-least-once. Handlers must be idempotent (they are: see notifications' unique key).
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.models import Category, Notification, Outbox, User
from app.reports.service import report_rows

log = logging.getLogger(__name__)

BUDGET_CHECK = "budget.check"
S3_DELETE = "s3.delete"
LEVELS = (80, 100)


def enqueue(session: AsyncSession, type_: str, payload: dict[str, Any]) -> None:
    session.add(Outbox(type=type_, payload=payload))


def enqueue_budget_check(session: AsyncSession, user_id: UUID, category_id: UUID, spent_on: date) -> None:
    month = spent_on.replace(day=1).isoformat()
    enqueue(session, BUDGET_CHECK, {"user_id": str(user_id), "category_id": str(category_id), "month": month})


async def handle_budget_check(session: AsyncSession, p: dict) -> None:
    """Recomputes the category's month total from the DB, so replays and reordering are harmless."""
    user = await session.get(User, UUID(p["user_id"]))
    cat = await session.get(Category, UUID(p["category_id"]))
    if user is None or cat is None or cat.monthly_budget is None:
        return
    month = date.fromisoformat(p["month"])
    rows = await report_rows(session, user.id, user.base_currency, month.year, month.month, category_id=cat.id)
    if not rows:
        return
    percent = rows[0].total / cat.monthly_budget * 100
    for level in LEVELS:
        if percent >= level:
            msg = f"You have used {level}% of your '{cat.name}' budget for {month:%B %Y}."
            await session.execute(
                pg_insert(Notification)
                .values(user_id=user.id, category_id=cat.id, month=month, level=level, message=msg)
                .on_conflict_do_nothing(constraint="uq_notifications_once")
            )


async def handle_s3_delete(_: AsyncSession, p: dict) -> None:
    await storage.delete(p["key"])  # raising here means "retry later"


HANDLERS: dict[str, Callable[[AsyncSession, dict], Awaitable[None]]] = {
    BUDGET_CHECK: handle_budget_check,
    S3_DELETE: handle_s3_delete,
}


def _dedupe_key(ev: Outbox) -> tuple | None:
    if ev.type == BUDGET_CHECK:
        p = ev.payload
        return (ev.type, p["user_id"], p["category_id"], p["month"])
    return None


async def process_batch(session: AsyncSession, batch_size: int = 500) -> int:
    """Claim and process up to batch_size events. SKIP LOCKED lets any number of workers run safely.

    Only the latest total matters for budget checks, so duplicates of the same (user, category, month)
    inside a batch are handled once; this is what makes a 500k backlog drain quickly.
    """
    events = (
        (
            await session.execute(
                select(Outbox)
                .where(Outbox.processed_at.is_(None), Outbox.available_at <= datetime.now(timezone.utc))
                .order_by(Outbox.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    seen: set[tuple] = set()
    now = datetime.now(timezone.utc)
    for ev in events:
        key = _dedupe_key(ev)
        try:
            if key is None or key not in seen:
                async with session.begin_nested():  # a failing handler must not poison the batch
                    await HANDLERS[ev.type](session, ev.payload)
            if key is not None:
                seen.add(key)
            ev.processed_at = now
        except Exception:
            log.exception("outbox event %s failed", ev.id)
            ev.attempts += 1
            ev.available_at = now + timedelta(seconds=min(2**ev.attempts, 3600))
    await session.commit()
    return len(events)

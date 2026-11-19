import base64
import binascii
import json
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import outbox
from app.errors import AppError
from app.expenses.schemas import ExpenseCreate, ExpenseOut, ExpensePatch
from app.models import Category, Expense, User


async def get_owned_category(session: AsyncSession, user_id: UUID, category_id: UUID) -> Category:
    cat = (
        await session.execute(select(Category).where(Category.id == category_id, Category.user_id == user_id))
    ).scalar_one_or_none()
    if cat is None:
        raise AppError(404, "category_not_found", "Category not found")
    return cat


async def get_owned(session: AsyncSession, user_id: UUID, expense_id: int) -> Expense:
    # Another user's row is indistinguishable from a missing one: 404, never 403.
    exp = (
        await session.execute(select(Expense).where(Expense.id == expense_id, Expense.user_id == user_id))
    ).scalar_one_or_none()
    if exp is None:
        raise AppError(404, "expense_not_found", "Expense not found")
    return exp


async def create(session: AsyncSession, user: User, data: ExpenseCreate) -> ExpenseOut:
    cat = await get_owned_category(session, user.id, data.category_id)
    exp = Expense(user_id=user.id, **data.model_dump())
    session.add(exp)
    await session.flush()
    if cat.monthly_budget is not None:
        outbox.enqueue_budget_check(session, user.id, cat.id, exp.spent_on)  # same transaction as the insert
    await session.refresh(exp)
    return ExpenseOut.model_validate(exp)


async def update_expense(session: AsyncSession, user: User, expense_id: int, version: int, patch: ExpensePatch) -> ExpenseOut:
    changes = patch.model_dump(exclude_unset=True)
    if "category_id" in changes:
        await get_owned_category(session, user.id, changes["category_id"])
    # Check and write in ONE statement: the WHERE on version is the lock-free compare-and-swap.
    updated = (
        await session.execute(
            update(Expense)
            .where(Expense.id == expense_id, Expense.user_id == user.id, Expense.version == version)
            .values(**changes, version=Expense.version + 1, updated_at=func.now())
            .returning(Expense)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()
    if updated is None:
        await get_owned(session, user.id, expense_id)  # 404 if it isn't ours / doesn't exist
        raise AppError(412, "version_conflict", "The expense was modified by someone else; fetch it again")
    cat = await session.get(Category, updated.category_id)
    if cat.monthly_budget is not None:
        # Only the (new) category/month can newly cross a threshold; decreases never raise alerts.
        outbox.enqueue_budget_check(session, user.id, cat.id, updated.spent_on)
    return ExpenseOut.model_validate(updated)


async def delete_expense(session: AsyncSession, user_id: UUID, expense_id: int) -> None:
    row = (
        await session.execute(
            delete(Expense).where(Expense.id == expense_id, Expense.user_id == user_id).returning(Expense.receipt_key)
        )
    ).first()
    if row is None:
        raise AppError(404, "expense_not_found", "Expense not found")
    if row.receipt_key:
        # Durable cleanup: if S3 is down the event is retried until the file is gone.
        outbox.enqueue(session, outbox.S3_DELETE, {"key": row.receipt_key})


def encode_cursor(exp: Expense) -> str:
    return base64.urlsafe_b64encode(json.dumps([exp.spent_on.isoformat(), exp.id]).encode()).decode()


def decode_cursor(cursor: str) -> tuple[date, int]:
    try:
        d, i = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return date.fromisoformat(d), int(i)
    except (ValueError, TypeError, binascii.Error):
        raise AppError(400, "invalid_cursor", "Invalid cursor") from None


async def list_expenses(
    session: AsyncSession,
    user_id: UUID,
    *,
    limit: int,
    cursor: str | None,
    category_id: UUID | None,
    date_from: date | None,
    date_to: date | None,
    min_amount: Decimal | None,
    max_amount: Decimal | None,
    currency: str | None,
    q: str | None,
) -> tuple[list[Expense], str | None]:
    stmt = select(Expense).where(Expense.user_id == user_id)
    if category_id:
        stmt = stmt.where(Expense.category_id == category_id)
    if date_from:
        stmt = stmt.where(Expense.spent_on >= date_from)
    if date_to:
        stmt = stmt.where(Expense.spent_on <= date_to)
    if min_amount is not None:
        stmt = stmt.where(Expense.amount >= min_amount)
    if max_amount is not None:
        stmt = stmt.where(Expense.amount <= max_amount)
    if currency:
        stmt = stmt.where(Expense.currency == currency.upper())
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(Expense.description.ilike(f"%{escaped}%", escape="\\"))
    if cursor:
        # Keyset: rows strictly after the last seen (spent_on, id). Unaffected by rows inserted meanwhile.
        stmt = stmt.where(tuple_(Expense.spent_on, Expense.id) < tuple_(*decode_cursor(cursor)))
    stmt = stmt.order_by(Expense.spent_on.desc(), Expense.id.desc()).limit(limit + 1)
    rows = list((await session.execute(stmt)).scalars())
    items = rows[:limit]
    return items, (encode_cursor(items[-1]) if len(rows) > limit else None)

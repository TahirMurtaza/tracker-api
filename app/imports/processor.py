"""Background CSV import. Streams the file, validates row by row, inserts in batches."""

import asyncio
import csv
import logging
import os
import tempfile
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import outbox, storage
from app.config import settings
from app.models import Category, Expense, ImportJob

log = logging.getLogger(__name__)

MAX_ERRORS_STORED = 1000
STALE_AFTER = timedelta(minutes=5)  # a job "running" this long without progress is re-claimed (crash recovery)
REQUIRED = {"date", "amount", "currency", "category"}


def parse_row(row: dict, categories: dict[str, UUID]) -> tuple[dict | None, str | None]:
    """Return (clean_values, None) or (None, reason)."""
    try:
        spent_on = date.fromisoformat((row.get("date") or "").strip())
    except ValueError:
        return None, "invalid date (expected YYYY-MM-DD)"
    try:
        amount = Decimal((row.get("amount") or "").strip())
    except InvalidOperation:
        return None, "invalid amount"
    if not amount.is_finite() or amount <= 0:
        return None, "amount must be greater than 0"
    if amount.as_tuple().exponent < -2:
        return None, "amount has more than 2 decimal places"
    if amount >= Decimal(10) ** 12:
        return None, "amount too large"
    currency = (row.get("currency") or "").strip().upper()
    if currency not in settings.supported_currencies:
        return None, f"unsupported currency '{currency}'"
    name = (row.get("category") or "").strip()
    category_id = categories.get(name.lower())
    if category_id is None:
        return None, f"unknown category '{name}'"
    description = (row.get("description") or "").strip()
    if len(description) > 500:
        return None, "description longer than 500 characters"
    return {
        "category_id": category_id,
        "amount": amount,
        "currency": currency,
        "spent_on": spent_on,
        "description": description,
    }, None


async def claim_job(sessionmaker: async_sessionmaker[AsyncSession]) -> ImportJob | None:
    async with sessionmaker() as session:
        job = (
            await session.execute(
                select(ImportJob)
                .where(
                    (ImportJob.status == "pending")
                    | ((ImportJob.status == "running") & (ImportJob.updated_at < func.now() - STALE_AFTER))
                )
                .order_by(ImportJob.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if job is None:
            return None
        job.status, job.total, job.done, job.failed, job.errors = "running", 0, 0, 0, []
        job.updated_at = func.now()
        await session.commit()
        return job


async def process_job(sessionmaker: async_sessionmaker[AsyncSession], job: ImportJob) -> None:
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        await storage.download_file(job.s3_key, path)
        await _run(sessionmaker, job, path)
    except Exception as exc:
        log.exception("import %s failed", job.id)
        async with sessionmaker() as s:
            await s.execute(
                update(ImportJob)
                .where(ImportJob.id == job.id)
                .values(status="failed", errors=[{"row": None, "error": str(exc)[:300]}], updated_at=func.now())
            )
            await s.commit()
    finally:
        os.unlink(path)


async def _run(sessionmaker: async_sessionmaker[AsyncSession], job: ImportJob, path: str) -> None:
    async with sessionmaker() as s:
        cats = (await s.execute(select(Category).where(Category.user_id == job.user_id))).scalars().all()
    categories = {c.name.lower(): c.id for c in cats}
    budgeted = {c.id for c in cats if c.monthly_budget is not None}

    total = failed = 0
    errors: list[dict] = []
    batch: list[dict] = []

    async def flush() -> None:
        nonlocal total, failed
        async with sessionmaker() as s:
            if batch:
                await s.execute(
                    pg_insert(Expense)
                    .values(batch)
                    .on_conflict_do_nothing(index_elements=["import_id", "import_row"], index_where=Expense.import_id.isnot(None))
                )
                for cat_id, month in {
                    (r["category_id"], r["spent_on"].replace(day=1)) for r in batch if r["category_id"] in budgeted
                }:
                    outbox.enqueue_budget_check(s, job.user_id, cat_id, month)
            await s.execute(
                update(ImportJob)
                .where(ImportJob.id == job.id)
                .values(total=total, failed=failed, errors=errors[:MAX_ERRORS_STORED], updated_at=func.now())
            )
            await s.commit()
        batch.clear()
        await asyncio.sleep(0)  # yield to the loop between chunks

    # utf-8-sig tolerates the BOM Excel adds. The file is read line by line, never fully loaded.
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = {h.strip().lower() for h in (reader.fieldnames or [])}
        if not REQUIRED <= header:
            raise ValueError(f"CSV must have columns: {', '.join(sorted(REQUIRED))} (description optional)")
        reader.fieldnames = [h.strip().lower() for h in reader.fieldnames]
        for line_no, row in enumerate(reader, start=2):  # line 1 is the header
            total += 1
            values, err = parse_row(row, categories)
            if err:
                failed += 1
                if len(errors) < MAX_ERRORS_STORED:
                    errors.append({"row": line_no, "error": err})
                continue
            batch.append({**values, "user_id": job.user_id, "import_id": job.id, "import_row": line_no})
            if len(batch) >= settings.import_chunk_size:
                await flush()
    await flush()

    async with sessionmaker() as s:
        done = (await s.execute(select(func.count()).select_from(Expense).where(Expense.import_id == job.id))).scalar_one()
        await s.execute(
            update(ImportJob).where(ImportJob.id == job.id).values(done=done, status="completed", updated_at=func.now())
        )
        await s.commit()


async def run_once(sessionmaker: async_sessionmaker[AsyncSession]) -> bool:
    job = await claim_job(sessionmaker)
    if job is None:
        return False
    await process_job(sessionmaker, job)
    return True

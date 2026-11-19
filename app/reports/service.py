"""The single SQL statement behind the monthly report, category budgets and budget alerts."""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Conversion: amount * USD-per-unit(expense currency) / USD-per-unit(base currency), using for each the
# latest rate on or before the expense date (LATERAL ... ORDER BY valid_on DESC LIMIT 1). Expenses with no
# usable rate are excluded from totals and counted in `missing` so the caller can surface them.
# Month-over-month change is LAG() over each category's months: one pass, no Python loop.
_SQL = text(
    """
WITH conv AS (
  SELECT e.category_id,
         date_trunc('month', e.spent_on)::date AS month,
         e.amount * rc.rate_to_usd / rb.rate_to_usd AS amount_base
  FROM expenses e
  LEFT JOIN LATERAL (
    SELECT r.rate_to_usd FROM exchange_rates r
    WHERE r.currency = e.currency AND r.valid_on <= e.spent_on
    ORDER BY r.valid_on DESC LIMIT 1) rc ON true
  LEFT JOIN LATERAL (
    SELECT r.rate_to_usd FROM exchange_rates r
    WHERE r.currency = :base AND r.valid_on <= e.spent_on
    ORDER BY r.valid_on DESC LIMIT 1) rb ON true
  WHERE e.user_id = :uid
    AND e.spent_on >= :prev_start AND e.spent_on < :next_start
    AND (CAST(:cat AS uuid) IS NULL OR e.category_id = CAST(:cat AS uuid))
),
agg AS (
  SELECT category_id, month,
         COALESCE(SUM(amount_base), 0) AS total,
         COUNT(*) FILTER (WHERE amount_base IS NULL) AS missing
  FROM conv GROUP BY category_id, month
),
months(month) AS (VALUES (CAST(:prev_start AS date)), (CAST(:cur_start AS date))),
grid AS (
  SELECT c.id, c.name, c.monthly_budget, m.month,
         COALESCE(a.total, 0) AS total,
         COALESCE(a.missing, 0) AS missing,
         LAG(COALESCE(a.total, 0)) OVER (PARTITION BY c.id ORDER BY m.month) AS prev_total
  FROM categories c
  CROSS JOIN months m
  LEFT JOIN agg a ON a.category_id = c.id AND a.month = m.month
  WHERE c.user_id = :uid AND (CAST(:cat AS uuid) IS NULL OR c.id = CAST(:cat AS uuid))
)
SELECT id, name, monthly_budget, total, prev_total, missing
FROM grid WHERE month = CAST(:cur_start AS date)
ORDER BY name
"""
)

_CENT = Decimal("0.01")


@dataclass
class CategoryReport:
    category_id: UUID
    name: str
    budget: Decimal | None
    total: Decimal
    previous_total: Decimal
    percent_used: Decimal | None
    change_amount: Decimal
    change_percent: Decimal | None
    unconverted_expenses: int


def _q(d: Decimal) -> Decimal:
    return d.quantize(_CENT, rounding=ROUND_HALF_UP)


async def report_rows(
    session: AsyncSession, user_id: UUID, base_currency: str, year: int, month: int, category_id: UUID | None = None
) -> list[CategoryReport]:
    cur = date(year, month, 1)
    prev = date(year - 1, 12, 1) if month == 1 else date(year, month - 1, 1)
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    result = await session.execute(
        _SQL, {"uid": user_id, "base": base_currency, "cat": category_id, "prev_start": prev, "cur_start": cur, "next_start": nxt}
    )
    out = []
    for r in result:
        total, prev_total = _q(r.total), _q(r.prev_total)
        out.append(
            CategoryReport(
                category_id=r.id,
                name=r.name,
                budget=r.monthly_budget,
                total=total,
                previous_total=prev_total,
                percent_used=_q(total / r.monthly_budget * 100) if r.monthly_budget else None,
                change_amount=total - prev_total,
                change_percent=_q((total - prev_total) / prev_total * 100) if prev_total else None,
                unconverted_expenses=r.missing,
            )
        )
    return out

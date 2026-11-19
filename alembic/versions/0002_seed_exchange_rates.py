"""Seed 90 days of exchange rates (PKR, AED, USD, EUR).

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# rate_to_usd = USD per 1 unit of currency. Base values are approximate; a small deterministic
# wobble (+-0.3%) makes each day differ so conversion-by-date is observable. Dev/demo data only.
BASE = {"USD": "1", "EUR": "1.08", "AED": "0.27229", "PKR": "0.00358"}


def upgrade() -> None:
    for currency, base in BASE.items():
        wobble = "0" if currency == "USD" else "0.003 * sin(n)"
        op.execute(
            f"""
            INSERT INTO exchange_rates (currency, valid_on, rate_to_usd)
            SELECT '{currency}', (CURRENT_DATE - n)::date, round(({base} * (1 + {wobble}))::numeric, 8)
            FROM generate_series(0, 89) AS n
            ON CONFLICT DO NOTHING
            """
        )


def downgrade() -> None:
    op.execute("DELETE FROM exchange_rates WHERE currency IN ('USD','EUR','AED','PKR')")

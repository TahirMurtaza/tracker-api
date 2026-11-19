from datetime import date
from decimal import Decimal

from tests.conftest import set_rates

API = "/api/v1"


async def test_monthly_report_mixed_currencies_and_missing_rate_day(client, alice):
    # USD per unit. EUR has rates only on Oct 1 and Oct 5, so Oct 3 uses Oct 1's rate and Oct 6 uses Oct 5's.
    await set_rates({"PKR": "0.005", "USD": "1"}, start=date(2026, 8, 1), days=120)
    from sqlalchemy import text

    from app.db import SessionLocal

    async with SessionLocal() as s:
        await s.execute(text("INSERT INTO exchange_rates VALUES ('EUR','2026-10-01',1.25),('EUR','2026-10-05',1.5)"))
        await s.commit()

    food = await alice.category("Food", budget="15000")
    await alice.category("Empty")
    e = lambda amt, cur, d: alice.expense(food, amt, cur, date.fromisoformat(d))  # noqa: E731
    await e("10.00", "EUR", "2026-10-03")  # 10*1.25/0.005 = 2500
    await e("10.00", "EUR", "2026-10-06")  # 10*1.50/0.005 = 3000
    await e("1000.00", "PKR", "2026-10-10")  # 1000
    await e("5.00", "USD", "2026-10-02")  # 1000
    await e("5000.00", "PKR", "2026-09-15")  # last month
    await e("7.00", "AED", "2026-10-04")  # no AED rate at all -> excluded, flagged

    r = await client.get(f"{API}/reports/monthly", params={"year": 2026, "month": 10}, headers=alice.headers)
    assert r.status_code == 200
    body = r.json()
    assert body["base_currency"] == "PKR"
    lines = {c["name"]: c for c in body["categories"]}
    food_line = lines["Food"]
    assert Decimal(food_line["total"]) == Decimal("7500.00")
    assert Decimal(food_line["previous_total"]) == Decimal("5000.00")
    assert Decimal(food_line["change_amount"]) == Decimal("2500.00")
    assert Decimal(food_line["change_percent"]) == Decimal("50.00")
    assert Decimal(food_line["percent_used"]) == Decimal("50.00")
    assert food_line["unconverted_expenses"] == 1
    assert Decimal(lines["Empty"]["total"]) == 0 and lines["Empty"]["percent_used"] is None


async def test_report_january_compares_with_december(client, alice):
    await set_rates({"PKR": "0.005", "USD": "1"}, start=date(2025, 11, 1), days=120)
    cat = await alice.category("Food")
    await alice.expense(cat, "100.00", "PKR", date(2025, 12, 31))
    await alice.expense(cat, "300.00", "PKR", date(2026, 1, 1))
    r = await client.get(f"{API}/reports/monthly", params={"year": 2026, "month": 1}, headers=alice.headers)
    line = r.json()["categories"][0]
    assert Decimal(line["previous_total"]) == 100 and Decimal(line["change_percent"]) == 200


async def test_report_is_scoped_to_user(client, alice, bob):
    await alice.expense(await alice.category("Food"))
    r = await client.get(f"{API}/reports/monthly", params={"year": 2026, "month": 10}, headers=bob.headers)
    assert r.json()["categories"] == []


async def test_report_validates_month(client, alice):
    r = await client.get(f"{API}/reports/monthly", params={"year": 2026, "month": 13}, headers=alice.headers)
    assert r.status_code == 422


async def test_categories_list_shows_spend_vs_budget(client, alice):
    cat = await alice.category("Food", budget="1000")
    await alice.expense(cat, "250.00", "PKR")
    r = await client.get(f"{API}/categories", headers=alice.headers)
    line = r.json()[0]
    assert Decimal(line["spent_this_month"]) == 250 and Decimal(line["percent_used"]) == 25

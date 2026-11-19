import asyncio

from sqlalchemy import text

from app.db import SessionLocal
from app.imports import processor

API = "/api/v1"
HEADER = "date,amount,currency,category,description\n"


async def upload(client, acct, content: str, name="e.csv"):
    return await client.post(f"{API}/expenses/import", files={"file": (name, content.encode(), "text/csv")}, headers=acct.headers)


async def run_worker() -> None:
    while await processor.run_once(SessionLocal):
        pass


async def status(client, acct, job_id):
    return (await client.get(f"{API}/imports/{job_id}", headers=acct.headers)).json()


async def expense_count() -> int:
    async with SessionLocal() as s:
        return (await s.execute(text("SELECT count(*) FROM expenses"))).scalar_one()


async def test_import_with_good_and_bad_rows(client, alice):
    await alice.category("Food")
    csv_text = (
        HEADER
        + "\n".join(
            [
                "2026-10-01,10.50,PKR,Food,lunch",  # ok (line 2)
                "not-a-date,10.00,PKR,Food,x",  # bad date (3)
                "2026-10-02,0,PKR,Food,x",  # zero (4)
                "2026-10-02,-4,PKR,Food,x",  # negative (5)
                "2026-10-02,1.234,PKR,Food,x",  # 3 decimals (6)
                "2026-10-02,5.00,XYZ,Food,x",  # bad currency (7)
                "2026-10-02,5.00,PKR,Nope,x",  # unknown category (8)
                "2026-10-03,20.00,usd,food,ok lowercase",  # ok (9)
                "2026-10-03,abc,PKR,Food,x",  # bad amount (10)
            ]
        )
        + "\n"
    )
    r = await upload(client, alice, csv_text)
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert (await status(client, alice, job_id))["status"] == "pending"

    await run_worker()
    st = await status(client, alice, job_id)
    assert st["status"] == "completed"
    assert (st["total"], st["done"], st["failed"]) == (9, 2, 7)
    assert [e["row"] for e in st["errors"]] == [3, 4, 5, 6, 7, 8, 10]
    assert "decimal" in st["errors"][3]["error"] and "unknown category" in st["errors"][5]["error"]
    assert await expense_count() == 2


async def test_same_file_twice_creates_no_duplicates(client, alice):
    await alice.category("Food")
    csv_text = HEADER + "2026-10-01,10.00,PKR,Food,a\n2026-10-02,11.00,PKR,Food,b\n"
    j1 = (await upload(client, alice, csv_text)).json()
    await run_worker()
    j2 = (await upload(client, alice, csv_text)).json()
    await run_worker()
    assert j1["job_id"] == j2["job_id"] and j2["duplicate"] is True
    assert await expense_count() == 2


async def test_reprocessing_a_job_is_idempotent(client, alice):
    await alice.category("Food")
    job_id = (await upload(client, alice, HEADER + "2026-10-01,10.00,PKR,Food,a\n")).json()["job_id"]
    await run_worker()
    async with SessionLocal() as s:  # simulate a crashed worker whose job gets re-claimed
        await s.execute(text("UPDATE import_jobs SET status='running', updated_at = now() - interval '10 minutes'"))
        await s.commit()
    await run_worker()
    assert await expense_count() == 1
    assert (await status(client, alice, job_id))["done"] == 1


async def test_parallel_identical_uploads_make_one_job(client, alice):
    await alice.category("Food")
    csv_text = HEADER + "2026-10-01,10.00,PKR,Food,a\n"
    rs = await asyncio.gather(*[upload(client, alice, csv_text) for _ in range(4)])
    assert len({r.json()["job_id"] for r in rs}) == 1
    await run_worker()
    assert await expense_count() == 1


async def test_large_import_runs_in_chunks(client, alice, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "import_chunk_size", 200)
    await alice.category("Food")
    rows = [f"2026-10-{(i % 28) + 1:02d},{i % 90 + 1}.25,PKR,Food,row {i}" for i in range(1000)]
    rows[500] = "bad,1.00,PKR,Food,x"
    job_id = (await upload(client, alice, HEADER + "\n".join(rows) + "\n")).json()["job_id"]
    await run_worker()
    st = await status(client, alice, job_id)
    assert (st["total"], st["done"], st["failed"]) == (1000, 999, 1)
    assert st["errors"] == [{"row": 502, "error": "invalid date (expected YYYY-MM-DD)"}]


async def test_missing_columns_fails_the_job(client, alice):
    job_id = (await upload(client, alice, "when,howmuch\n2026-10-01,5\n")).json()["job_id"]
    await run_worker()
    st = await status(client, alice, job_id)
    assert st["status"] == "failed" and "columns" in st["errors"][0]["error"]


async def test_import_status_is_private_and_empty_file_rejected(client, alice, bob):
    await alice.category("Food")
    job_id = (await upload(client, alice, HEADER + "2026-10-01,1.00,PKR,Food,a\n")).json()["job_id"]
    assert (await client.get(f"{API}/imports/{job_id}", headers=bob.headers)).status_code == 404
    assert (await upload(client, alice, "")).status_code == 422


async def test_import_enqueues_budget_checks(client, alice):
    await alice.category("Food", budget="100")
    await upload(client, alice, HEADER + "2026-10-01,90.00,PKR,Food,a\n")
    await run_worker()
    async with SessionLocal() as s:
        assert (await s.execute(text("SELECT count(*) FROM outbox WHERE type='budget.check'"))).scalar_one() == 1

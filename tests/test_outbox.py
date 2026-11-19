import asyncio

from sqlalchemy import text

from app import outbox, storage
from app.config import settings
from app.db import SessionLocal

API = "/api/v1"


async def drain() -> int:
    total = 0
    while True:
        async with SessionLocal() as s:
            n = await outbox.process_batch(s)
        total += n
        if n == 0:
            return total


async def notifications(client, acct):
    return (await client.get(f"{API}/notifications", headers=acct.headers)).json()["items"]


async def test_crossing_80_percent_creates_one_alert(client, alice):
    cat = await alice.category("Food", budget="1000")
    await alice.expense(cat, "700.00")
    await drain()
    assert await notifications(client, alice) == []
    await alice.expense(cat, "150.00")  # 850 -> 85%
    await drain()
    items = await notifications(client, alice)
    assert [i["level"] for i in items] == [80]


async def test_crossing_100_percent_adds_second_level(client, alice):
    cat = await alice.category("Food", budget="1000")
    await alice.expense(cat, "850.00")
    await drain()
    await alice.expense(cat, "200.00")
    await drain()
    assert sorted(i["level"] for i in await notifications(client, alice)) == [80, 100]


async def test_alert_not_sent_inside_request(client, alice):
    cat = await alice.category("Food", budget="100")
    await alice.expense(cat, "95.00")
    assert await notifications(client, alice) == []  # only the outbox row exists until the worker runs
    async with SessionLocal() as s:
        assert (await s.execute(text("SELECT count(*) FROM outbox WHERE processed_at IS NULL"))).scalar_one() == 1


async def test_event_processed_twice_creates_one_notification(client, alice):
    cat = await alice.category("Food", budget="100")
    await alice.expense(cat, "90.00")
    await drain()
    async with SessionLocal() as s:  # simulate a crash after the work but before the ack
        await s.execute(text("UPDATE outbox SET processed_at = NULL"))
        await s.commit()
    await drain()
    assert len(await notifications(client, alice)) == 1


async def test_two_workers_at_once_create_one_notification(client, alice):
    cat = await alice.category("Food", budget="100")
    for _ in range(5):
        await alice.expense(cat, "30.00")

    async def worker():
        async with SessionLocal() as s:
            return await outbox.process_batch(s, batch_size=2)

    for _ in range(4):
        await asyncio.gather(worker(), worker(), worker())
    levels = [i["level"] for i in await notifications(client, alice)]
    assert sorted(levels) == [80, 100]


async def test_handler_replayed_directly_is_idempotent(client, alice):
    cat = await alice.category("Food", budget="100")
    await alice.expense(cat, "100.00")
    async with SessionLocal() as s:
        ev = (await s.execute(text("SELECT payload FROM outbox LIMIT 1"))).scalar_one()
    for _ in range(3):
        async with SessionLocal() as s:
            await outbox.handle_budget_check(s, ev)
            await s.commit()
    assert len(await notifications(client, alice)) == 2  # 80 and 100, once each


async def test_edit_that_crosses_threshold_alerts(client, alice):
    cat = await alice.category("Food", budget="1000")
    eid = (await alice.expense(cat, "100.00")).json()["id"]
    await drain()
    await client.patch(f"{API}/expenses/{eid}", json={"amount": "900.00"}, headers={**alice.headers, "If-Match": '"1"'})
    await drain()
    assert [i["level"] for i in await notifications(client, alice)] == [80]


async def test_delete_does_not_retract_or_realert(client, alice):
    cat = await alice.category("Food", budget="1000")
    eid = (await alice.expense(cat, "900.00")).json()["id"]
    await drain()
    await client.delete(f"{API}/expenses/{eid}", headers=alice.headers)
    await alice.expense(cat, "900.00")  # crosses 80% again after dropping below
    await drain()
    assert len(await notifications(client, alice)) == 1


async def test_category_without_budget_enqueues_nothing(alice):
    cat = await alice.category("Free")
    await alice.expense(cat, "999.00")
    async with SessionLocal() as s:
        assert (await s.execute(text("SELECT count(*) FROM outbox"))).scalar_one() == 0


async def test_failed_event_is_retried_with_backoff(alice, monkeypatch):
    storage_client = storage._client()
    key = "receipts/x/1/a.png"
    storage_client.put_object(Bucket=settings.s3_bucket, Key=key, Body=b"x")
    async with SessionLocal() as s:
        outbox.enqueue(s, outbox.S3_DELETE, {"key": key})
        await s.commit()

    real_delete = storage.delete

    async def boom(_):
        raise RuntimeError("s3 down")

    monkeypatch.setattr(storage, "delete", boom)
    async with SessionLocal() as s:
        await outbox.process_batch(s)
    async with SessionLocal() as s:
        attempts, processed = (await s.execute(text("SELECT attempts, processed_at FROM outbox"))).one()
    assert attempts == 1 and processed is None

    monkeypatch.setattr(storage, "delete", real_delete)
    async with SessionLocal() as s:
        await s.execute(text("UPDATE outbox SET available_at = now() - interval '1 second'"))
        await s.commit()
    await drain()
    assert "Contents" not in storage._client().list_objects_v2(Bucket=settings.s3_bucket)


async def test_batch_coalesces_duplicate_budget_checks(alice, monkeypatch):
    cat = await alice.category("Food", budget="1000")
    for _ in range(6):
        await alice.expense(cat, "10.00")
    calls = []
    real = outbox.handle_budget_check

    async def spy(session, p):
        calls.append(p)
        await real(session, p)

    monkeypatch.setitem(outbox.HANDLERS, outbox.BUDGET_CHECK, spy)
    async with SessionLocal() as s:
        assert await outbox.process_batch(s) == 6
    assert len(calls) == 1  # six events, one evaluation

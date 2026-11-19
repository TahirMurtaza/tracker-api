import asyncio

from app import worker

API = "/api/v1"


async def test_worker_loop_drains_outbox_and_imports_then_stops(client, alice):
    cat = await alice.category("Food", budget="100")
    await alice.expense(cat, "90.00")
    await client.post(
        f"{API}/expenses/import",
        files={"file": ("a.csv", b"date,amount,currency,category\n2026-10-01,5.00,PKR,Food\n", "text/csv")},
        headers=alice.headers,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    for _ in range(50):
        items = (await client.get(f"{API}/notifications", headers=alice.headers)).json()["items"]
        listed = (await client.get(f"{API}/expenses", headers=alice.headers)).json()["items"]
        if items and len(listed) == 2:
            break
        await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert [i["level"] for i in items] == [80] and len(listed) == 2


async def test_health(client):
    assert (await client.get("/health")).json() == {"status": "ok"}


async def test_unknown_route_uses_error_format(client):
    r = await client.get("/api/v1/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"

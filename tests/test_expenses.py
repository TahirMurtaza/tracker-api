import asyncio
import base64
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.db import SessionLocal
from tests.conftest import today

API = "/api/v1"


async def test_create_expense_returns_201_and_etag(alice):
    cat = await alice.category()
    r = await alice.expense(cat, "12.50", "PKR", description="lunch")
    assert r.status_code == 201
    assert r.headers["ETag"] == '"1"'
    body = r.json()
    assert body["amount"] == "12.50" and body["version"] == 1


@pytest.mark.parametrize("amount", ["0", "-5", "10.001", "abc"])
async def test_invalid_amounts_rejected(alice, amount):
    cat = await alice.category()
    r = await alice.expense(cat, amount)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


async def test_unsupported_currency_rejected(alice):
    cat = await alice.category()
    assert (await alice.expense(cat, "5.00", "XYZ")).status_code == 422


async def test_expense_in_foreign_category_is_404(alice, bob):
    bobs_cat = await bob.category()
    r = await alice.expense(bobs_cat)
    assert r.status_code == 404


async def test_other_users_expense_is_404_not_403(client, alice, bob):
    cat = await alice.category()
    eid = (await alice.expense(cat)).json()["id"]
    url = f"{API}/expenses/{eid}"
    assert (await client.get(url, headers=bob.headers)).status_code == 404
    assert (await client.patch(url, json={"description": "x"}, headers={**bob.headers, "If-Match": '"1"'})).status_code == 404
    assert (await client.delete(url, headers=bob.headers)).status_code == 404
    assert (await client.get(url, headers=alice.headers)).status_code == 200  # untouched


async def test_get_expense_has_etag(client, alice):
    cat = await alice.category()
    eid = (await alice.expense(cat)).json()["id"]
    r = await client.get(f"{API}/expenses/{eid}", headers=alice.headers)
    assert r.status_code == 200 and r.headers["ETag"] == '"1"'


async def test_delete_expense(client, alice):
    cat = await alice.category()
    eid = (await alice.expense(cat)).json()["id"]
    assert (await client.delete(f"{API}/expenses/{eid}", headers=alice.headers)).status_code == 204
    assert (await client.get(f"{API}/expenses/{eid}", headers=alice.headers)).status_code == 404


async def test_duplicate_category_name_409(client, alice):
    await alice.category("Food")
    r = await client.post(f"{API}/categories", json={"name": "Food"}, headers=alice.headers)
    assert r.status_code == 409


async def test_list_filters(client, alice):
    food, rent = await alice.category("Food"), await alice.category("Rent")
    d = today()
    await alice.expense(food, "10.00", "PKR", d - timedelta(days=10), "Coffee beans")
    await alice.expense(food, "50.00", "USD", d - timedelta(days=5), "Dinner out")
    await alice.expense(rent, "900.00", "PKR", d, "100% rent_due")

    async def ids(**params):
        r = await client.get(f"{API}/expenses", params=params, headers=alice.headers)
        assert r.status_code == 200
        return {i["description"] for i in r.json()["items"]}

    assert await ids(category_id=food) == {"Coffee beans", "Dinner out"}
    assert await ids(currency="usd") == {"Dinner out"}
    assert await ids(min_amount="20", max_amount="100") == {"Dinner out"}
    assert await ids(date_from=(d - timedelta(days=6)).isoformat()) == {"Dinner out", "100% rent_due"}
    assert await ids(date_to=(d - timedelta(days=6)).isoformat()) == {"Coffee beans"}
    assert await ids(q="COFFEE") == {"Coffee beans"}
    assert await ids(q="%") == {"100% rent_due"}  # wildcards are escaped, not interpreted
    assert await ids(q="rent_") == {"100% rent_due"}


async def test_list_only_returns_own_expenses(client, alice, bob):
    await alice.expense(await alice.category())
    r = await client.get(f"{API}/expenses", headers=bob.headers)
    assert r.json() == {"items": [], "next_cursor": None}


async def test_cursor_pagination_stable_while_rows_are_inserted(client, alice):
    cat = await alice.category()
    base = today() - timedelta(days=100)
    original = [(await alice.expense(cat, "1.00", spent_on=base + timedelta(days=i // 2))).json()["id"] for i in range(24)]

    async def page(cursor=None):
        r = await client.get(
            f"{API}/expenses", params={"limit": 5, **({"cursor": cursor} if cursor else {})}, headers=alice.headers
        )
        assert r.status_code == 200
        return r.json()

    seen = []
    p = await page()
    seen += [i["id"] for i in p["items"]]
    # New rows land between page requests: some newer than the cursor (must not appear),
    # some older than it (must appear exactly once, later).
    newer = [(await alice.expense(cat, "2.00", spent_on=today())).json()["id"] for _ in range(3)]
    older = [(await alice.expense(cat, "3.00", spent_on=base - timedelta(days=1))).json()["id"] for _ in range(2)]
    while p["next_cursor"]:
        p = await page(p["next_cursor"])
        seen += [i["id"] for i in p["items"]]

    assert len(seen) == len(set(seen)), "duplicate rows across pages"
    assert set(original) <= set(seen), "missing rows"
    assert set(older) <= set(seen)
    assert not set(newer) & set(seen)


async def test_pagination_order_is_spent_on_desc_then_id_desc(client, alice):
    cat = await alice.category()
    d = today()
    a = (await alice.expense(cat, spent_on=d)).json()["id"]
    b = (await alice.expense(cat, spent_on=d)).json()["id"]
    c = (await alice.expense(cat, spent_on=d - timedelta(days=1))).json()["id"]
    items = (await client.get(f"{API}/expenses", headers=alice.headers)).json()["items"]
    assert [i["id"] for i in items] == [b, a, c]


async def test_invalid_cursor_is_400(client, alice):
    r = await client.get(f"{API}/expenses", params={"cursor": "%%%"}, headers=alice.headers)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_cursor"
    forged = base64.urlsafe_b64encode(b'["nope", 1]').decode()
    assert (await client.get(f"{API}/expenses", params={"cursor": forged}, headers=alice.headers)).status_code == 400


# ---- optimistic locking ----------------------------------------------------------------------


async def _new(alice):
    cat = await alice.category()
    return (await alice.expense(cat, "10.00")).json()["id"]


async def test_patch_requires_if_match(client, alice):
    eid = await _new(alice)
    r = await client.patch(f"{API}/expenses/{eid}", json={"description": "x"}, headers=alice.headers)
    assert r.status_code == 428


async def test_patch_with_current_version_bumps_etag(client, alice):
    eid = await _new(alice)
    r = await client.patch(
        f"{API}/expenses/{eid}", json={"description": "new", "amount": "20.00"}, headers={**alice.headers, "If-Match": '"1"'}
    )
    assert r.status_code == 200
    assert r.headers["ETag"] == '"2"' and r.json()["description"] == "new" and r.json()["amount"] == "20.00"


async def test_patch_with_stale_version_is_412(client, alice):
    eid = await _new(alice)
    h = {**alice.headers, "If-Match": '"1"'}
    assert (await client.patch(f"{API}/expenses/{eid}", json={"description": "a"}, headers=h)).status_code == 200
    r = await client.patch(f"{API}/expenses/{eid}", json={"description": "b"}, headers=h)
    assert r.status_code == 412 and r.json()["error"]["code"] == "version_conflict"
    assert (await client.get(f"{API}/expenses/{eid}", headers=alice.headers)).json()["description"] == "a"


async def test_patch_validation(client, alice):
    eid = await _new(alice)
    h = {**alice.headers, "If-Match": '"1"'}
    assert (await client.patch(f"{API}/expenses/{eid}", json={}, headers=h)).status_code == 422
    assert (await client.patch(f"{API}/expenses/{eid}", json={"amount": "0"}, headers=h)).status_code == 422
    assert (await client.patch(f"{API}/expenses/{eid}", json={"version": 9}, headers=h)).status_code == 422


async def test_parallel_patches_with_same_etag_one_wins(client, alice):
    eid = await _new(alice)
    h = {**alice.headers, "If-Match": '"1"'}
    rs = await asyncio.gather(
        client.patch(f"{API}/expenses/{eid}", json={"description": "phone-1"}, headers=h),
        client.patch(f"{API}/expenses/{eid}", json={"description": "phone-2"}, headers=h),
    )
    assert sorted(r.status_code for r in rs) == [200, 412]
    final = (await client.get(f"{API}/expenses/{eid}", headers=alice.headers)).json()
    assert final["version"] == 2


# ---- idempotency -----------------------------------------------------------------------------


async def _count_expenses() -> int:
    async with SessionLocal() as s:
        return (await s.execute(text("SELECT count(*) FROM expenses"))).scalar_one()


async def test_same_key_same_body_replays_first_response(alice):
    cat = await alice.category()
    r1 = await alice.expense(cat, "10.00", **{"Idempotency-Key": "k1"})
    r2 = await alice.expense(cat, "10.00", **{"Idempotency-Key": "k1"})
    assert r1.status_code == r2.status_code == 201
    assert r1.json() == r2.json()
    assert r2.headers["Idempotent-Replayed"] == "true"
    assert await _count_expenses() == 1


async def test_same_key_different_body_is_422(alice):
    cat = await alice.category()
    await alice.expense(cat, "10.00", **{"Idempotency-Key": "k1"})
    r = await alice.expense(cat, "11.00", **{"Idempotency-Key": "k1"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "idempotency_key_reuse"
    assert await _count_expenses() == 1


async def test_parallel_requests_with_same_key_create_one_expense(alice):
    cat = await alice.category()
    rs = await asyncio.gather(*[alice.expense(cat, "10.00", **{"Idempotency-Key": "race"}) for _ in range(8)])
    assert {r.status_code for r in rs} == {201}
    assert len({r.json()["id"] for r in rs}) == 1
    assert await _count_expenses() == 1


async def test_idempotency_keys_are_scoped_per_user(alice, bob):
    a, b = await alice.category(), await bob.category()
    r1 = await alice.expense(a, "10.00", **{"Idempotency-Key": "shared"})
    r2 = await bob.expense(b, "10.00", "AED", **{"Idempotency-Key": "shared"})
    assert r1.json()["id"] != r2.json()["id"]


async def test_expired_idempotency_key_can_be_reused(alice):
    cat = await alice.category()
    r1 = await alice.expense(cat, "10.00", **{"Idempotency-Key": "old"})
    async with SessionLocal() as s:
        await s.execute(text("UPDATE idempotency_keys SET created_at = now() - interval '25 hours'"))
        await s.commit()
    r2 = await alice.expense(cat, "10.00", **{"Idempotency-Key": "old"})
    assert r2.status_code == 201 and r2.json()["id"] != r1.json()["id"]


async def test_failed_request_does_not_burn_the_key(alice):
    r = await alice.expense("00000000-0000-0000-0000-000000000000", "10.00", **{"Idempotency-Key": "k"})
    assert r.status_code == 404
    cat = await alice.category()
    assert (await alice.expense(cat, "10.00", **{"Idempotency-Key": "k"})).status_code == 201

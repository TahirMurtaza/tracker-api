from app import storage
from app.config import settings
from tests.test_outbox import drain

API = "/api/v1"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
JPG = b"\xff\xd8\xff\xe0" + b"0" * 64
PDF = b"%PDF-1.7\n" + b"0" * 64
EXE = b"MZ\x90\x00\x03" + b"0" * 64


def put(key: str, body: bytes):
    storage._client().put_object(Bucket=settings.s3_bucket, Key=key, Body=body)


def exists(key: str) -> bool:
    return any(o["Key"] == key for o in storage._client().list_objects_v2(Bucket=settings.s3_bucket).get("Contents", []))


async def _expense(acct):
    cats = (await acct.client.get(f"{API}/categories", headers=acct.headers)).json()
    cat = cats[0]["id"] if cats else await acct.category()
    return (await acct.expense(cat)).json()["id"]


async def _upload_url(client, alice, eid, ct="image/png"):
    return await client.post(f"{API}/expenses/{eid}/receipt/upload-url", json={"content_type": ct}, headers=alice.headers)


async def test_upload_url_is_a_presigned_post_with_size_limit(client, alice):
    eid = await _expense(alice)
    r = await _upload_url(client, alice, eid)
    assert r.status_code == 200
    body = r.json()
    assert body["key"].startswith("receipts/") and body["key"].endswith(".png")
    assert f"/{eid}/" in body["key"]
    assert "policy" in body["fields"] and body["max_bytes"] == 5 * 1024 * 1024


async def test_upload_url_rejects_other_types(client, alice):
    eid = await _expense(alice)
    assert (await _upload_url(client, alice, eid, "application/x-msdownload")).status_code == 422


async def test_upload_url_for_foreign_expense_is_404(client, alice, bob):
    eid = await _expense(alice)
    assert (await _upload_url(client, bob, eid)).status_code == 404


async def test_confirm_valid_files(client, alice):
    for ct, body in [("image/png", PNG), ("image/jpeg", JPG), ("application/pdf", PDF)]:
        eid = await _expense(alice)
        key = (await _upload_url(client, alice, eid, ct)).json()["key"]
        put(key, body)
        r = await client.post(f"{API}/expenses/{eid}/receipt/confirm", json={"key": key}, headers=alice.headers)
        assert r.status_code == 200 and r.json()["receipt_key"] == key
        assert (await client.get(f"{API}/expenses/{eid}", headers=alice.headers)).json()["receipt_key"] == key


async def test_confirm_rejects_exe_renamed_to_png_and_deletes_it(client, alice):
    eid = await _expense(alice)
    key = (await _upload_url(client, alice, eid)).json()["key"]
    put(key, EXE)
    r = await client.post(f"{API}/expenses/{eid}/receipt/confirm", json={"key": key}, headers=alice.headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_file_type"
    assert not exists(key)
    assert (await client.get(f"{API}/expenses/{eid}", headers=alice.headers)).json()["receipt_key"] is None


async def test_confirm_rejects_real_png_with_pdf_extension(client, alice):
    eid = await _expense(alice)
    key = (await _upload_url(client, alice, eid, "application/pdf")).json()["key"]
    put(key, PNG)
    assert (
        await client.post(f"{API}/expenses/{eid}/receipt/confirm", json={"key": key}, headers=alice.headers)
    ).status_code == 422


async def test_confirm_without_upload_is_422(client, alice):
    eid = await _expense(alice)
    key = (await _upload_url(client, alice, eid)).json()["key"]
    r = await client.post(f"{API}/expenses/{eid}/receipt/confirm", json={"key": key}, headers=alice.headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "receipt_not_uploaded"


async def test_confirm_rejects_oversized_file(client, alice):
    eid = await _expense(alice)
    key = (await _upload_url(client, alice, eid)).json()["key"]
    put(key, PNG + b"0" * (5 * 1024 * 1024))
    r = await client.post(f"{API}/expenses/{eid}/receipt/confirm", json={"key": key}, headers=alice.headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "receipt_too_large"
    assert not exists(key)


async def test_confirm_rejects_keys_outside_the_expense_prefix(client, alice, bob):
    eid = await _expense(alice)
    other = await _expense(bob)
    key = (await _upload_url(client, bob, other)).json()["key"]
    put(key, PNG)
    r = await client.post(f"{API}/expenses/{eid}/receipt/confirm", json={"key": key}, headers=alice.headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_receipt_key"
    assert exists(key)  # someone else's file is never touched


async def test_download_link(client, alice):
    eid = await _expense(alice)
    assert (await client.get(f"{API}/expenses/{eid}/receipt", headers=alice.headers)).status_code == 404
    key = (await _upload_url(client, alice, eid)).json()["key"]
    put(key, PNG)
    await client.post(f"{API}/expenses/{eid}/receipt/confirm", json={"key": key}, headers=alice.headers)
    r = await client.get(f"{API}/expenses/{eid}/receipt", headers=alice.headers)
    assert r.status_code == 200 and "X-Amz-Expires=900" in r.json()["url"]


async def test_deleting_expense_removes_file_via_outbox(client, alice):
    eid = await _expense(alice)
    key = (await _upload_url(client, alice, eid)).json()["key"]
    put(key, PNG)
    await client.post(f"{API}/expenses/{eid}/receipt/confirm", json={"key": key}, headers=alice.headers)
    await client.delete(f"{API}/expenses/{eid}", headers=alice.headers)
    assert exists(key)  # queued, not yet deleted
    await drain()
    assert not exists(key)


async def test_replacing_receipt_cleans_up_old_file(client, alice):
    eid = await _expense(alice)
    keys = []
    for _ in range(2):
        key = (await _upload_url(client, alice, eid)).json()["key"]
        put(key, PNG)
        await client.post(f"{API}/expenses/{eid}/receipt/confirm", json={"key": key}, headers=alice.headers)
        keys.append(key)
    await drain()
    assert not exists(keys[0]) and exists(keys[1])


def test_sniff_detects_types():
    assert storage.sniff(PNG) == "png" and storage.sniff(JPG) == "jpg" and storage.sniff(PDF) == "pdf"
    assert storage.sniff(EXE) is None and storage.sniff(b"") is None

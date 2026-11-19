import pytest
from sqlalchemy import text

from app.db import SessionLocal

API = "/api/v1/auth"


async def test_register_returns_user_without_password(client):
    r = await client.post(f"{API}/register", json={"email": "New@Example.com", "password": "longenough1", "base_currency": "AED"})
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == "new@example.com" and body["base_currency"] == "AED"
    assert "password" not in str(body)


async def test_register_duplicate_email_is_409(client, alice):
    r = await client.post(
        f"{API}/register", json={"email": "ALICE@example.com", "password": "longenough1", "base_currency": "PKR"}
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "email_taken"


@pytest.mark.parametrize(
    "body",
    [
        {"email": "not-an-email", "password": "longenough1", "base_currency": "PKR"},
        {"email": "a@b.com", "password": "short", "base_currency": "PKR"},
        {"email": "a@b.com", "password": "longenough1", "base_currency": "XYZ"},
    ],
)
async def test_register_validation_uses_error_format(client, body):
    r = await client.post(f"{API}/register", json=body)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error" and err["details"]


async def test_login_wrong_password_401(client, alice):
    r = await client.post(f"{API}/login", json={"email": alice.email, "password": "wrong-password"})
    assert r.status_code == 401


async def test_password_is_hashed_with_argon2(alice):
    async with SessionLocal() as s:
        h = (await s.execute(text("SELECT password_hash FROM users"))).scalar_one()
    assert h.startswith("$argon2")


async def test_protected_endpoint_needs_valid_token(client):
    assert (await client.get("/api/v1/expenses")).status_code == 401
    r = await client.get("/api/v1/expenses", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"


async def test_refresh_rotates_token(client, alice):
    r = await client.post(f"{API}/refresh", json={"refresh_token": alice.refresh_token})
    assert r.status_code == 200
    assert r.json()["refresh_token"] != alice.refresh_token


async def test_reusing_refresh_token_revokes_all_tokens(client, alice):
    first = alice.refresh_token
    second = (await client.post(f"{API}/refresh", json={"refresh_token": first})).json()["refresh_token"]
    # Replaying the old token = theft signal.
    reuse = await client.post(f"{API}/refresh", json={"refresh_token": first})
    assert reuse.status_code == 401 and reuse.json()["error"]["code"] == "refresh_token_reused"
    # The legitimately rotated token is dead too.
    assert (await client.post(f"{API}/refresh", json={"refresh_token": second})).status_code == 401


async def test_refresh_with_unknown_token_401(client):
    r = await client.post(f"{API}/refresh", json={"refresh_token": "nope"})
    assert r.status_code == 401


async def test_logout_revokes_refresh_token(client, alice):
    assert (await client.post(f"{API}/logout", json={"refresh_token": alice.refresh_token})).status_code == 204
    assert (await client.post(f"{API}/refresh", json={"refresh_token": alice.refresh_token})).status_code == 401


async def test_sixth_failed_login_is_429_with_retry_after(client, alice):
    bad = {"email": alice.email, "password": "wrong-password"}
    for _ in range(5):
        assert (await client.post(f"{API}/login", json=bad)).status_code == 401
    r = await client.post(f"{API}/login", json=bad)
    assert r.status_code == 429
    assert 0 < int(r.headers["Retry-After"]) <= 900
    # Even the right password is refused while locked out.
    ok = await client.post(f"{API}/login", json={"email": alice.email, "password": alice.password})
    assert ok.status_code == 429


async def test_rate_limit_is_per_email(client, alice, bob):
    for _ in range(6):
        await client.post(f"{API}/login", json={"email": alice.email, "password": "wrong-password"})
    r = await client.post(f"{API}/login", json={"email": bob.email, "password": bob.password})
    assert r.status_code == 200


async def test_successful_login_resets_counter(client, alice):
    for _ in range(3):
        await client.post(f"{API}/login", json={"email": alice.email, "password": "wrong-password"})
    assert (await client.post(f"{API}/login", json={"email": alice.email, "password": alice.password})).status_code == 200
    for _ in range(5):
        assert (await client.post(f"{API}/login", json={"email": alice.email, "password": "wrong-password"})).status_code == 401

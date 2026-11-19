import asyncio
import os
from datetime import date, datetime, timedelta, timezone

# Must be set before the app is imported: settings are read once at import time.
os.environ.update(
    DATABASE_URL=os.getenv("TEST_DATABASE_URL", "postgresql+asyncpg://tracker:tracker@localhost:5433/tracker_test"),
    REDIS_URL=os.getenv("TEST_REDIS_URL", "redis://localhost:6380/15"),
    JWT_SECRET="test-secret-only-for-tests-0123456789abcdef",
    S3_BUCKET="test-receipts",
    S3_ENDPOINT_URL="",
    AWS_ACCESS_KEY_ID="testing",
    AWS_SECRET_ACCESS_KEY="testing",
    AWS_DEFAULT_REGION="us-east-1",
)

import asyncpg  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from alembic.config import Config  # noqa: E402
from moto import mock_aws  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from alembic import command  # noqa: E402
from app import storage  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.redis import redis  # noqa: E402

TABLES = "users, exchange_rates, outbox, import_jobs"  # CASCADE clears everything that references them


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _database():
    url = make_url(settings.database_url)
    admin = await asyncpg.connect(user=url.username, password=url.password, host=url.host, port=url.port, database="postgres")
    if not await admin.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", url.database):
        await admin.execute(f'CREATE DATABASE "{url.database}"')
    await admin.close()
    # Real migrations, not create_all: the migrations themselves are under test.
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    yield
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _clean_state():
    async with SessionLocal() as s:
        await s.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        await s.commit()
    await redis.flushdb()
    await set_rates({"USD": "1", "EUR": "1.25", "AED": "0.25", "PKR": "0.005"})
    yield


@pytest.fixture(autouse=True)
def _s3():
    with mock_aws():
        storage.reset_client()
        storage._client().create_bucket(Bucket=settings.s3_bucket)
        yield
    storage.reset_client()


async def set_rates(rates: dict[str, str], start: date | None = None, days: int = 120) -> None:
    """Replace all rates with a constant rate per currency for `days` days ending today (or from `start`)."""
    start = start or (datetime.now(timezone.utc).date() - timedelta(days=days))
    async with SessionLocal() as s:
        await s.execute(text("DELETE FROM exchange_rates"))
        for cur, rate in rates.items():
            await s.execute(
                text("INSERT INTO exchange_rates VALUES (:c, :d, :r)"),
                [{"c": cur, "d": start + timedelta(days=i), "r": rate} for i in range(days + 2)],
            )
        await s.commit()


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def today() -> date:
    return datetime.now(timezone.utc).date()


class Account:
    def __init__(self, client: httpx.AsyncClient, email: str, base_currency: str):
        self.client, self.email, self.base_currency = client, email, base_currency
        self.password = "correct-horse-battery"
        self.headers: dict = {}
        self.refresh_token = ""

    async def signup(self):
        r = await self.client.post(
            "/api/v1/auth/register", json={"email": self.email, "password": self.password, "base_currency": self.base_currency}
        )
        assert r.status_code == 201, r.text
        r = await self.client.post("/api/v1/auth/login", json={"email": self.email, "password": self.password})
        assert r.status_code == 200, r.text
        self.headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
        self.refresh_token = r.json()["refresh_token"]
        return self

    async def category(self, name="Food", budget=None) -> str:
        body = {"name": name, **({"monthly_budget": str(budget)} if budget is not None else {})}
        r = await self.client.post("/api/v1/categories", json=body, headers=self.headers)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    async def expense(self, category_id, amount="10.00", currency="PKR", spent_on=None, description="", **headers):
        body = {
            "category_id": category_id,
            "amount": amount,
            "currency": currency,
            "spent_on": (spent_on or today()).isoformat(),
            "description": description,
        }
        return await self.client.post("/api/v1/expenses", json=body, headers={**self.headers, **headers})


@pytest_asyncio.fixture
async def alice(client):
    return await Account(client, "alice@example.com", "PKR").signup()


@pytest_asyncio.fixture
async def bob(client):
    return await Account(client, "bob@example.com", "AED").signup()

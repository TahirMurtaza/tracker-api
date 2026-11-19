"""Idempotency-Key handling.

The key row is written in the SAME transaction as the expense, so they commit or roll back together.
Concurrency is settled by the (user_id, key) primary key: when two requests race, the second INSERT
blocks on the first one's uncommitted row, then sees the committed key and replays the stored response.
No Redis lock is needed, and Redis being down cannot cause duplicates.
"""

import hashlib
import json
from uuid import UUID

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.errors import AppError
from app.models import IdempotencyKey

# Claims the key if it is new, or if the previous one is older than the TTL (expired keys are reusable).
_CLAIM = text(
    """
INSERT INTO idempotency_keys (user_id, key, request_hash) VALUES (:uid, :key, :hash)
ON CONFLICT (user_id, key) DO UPDATE
  SET request_hash = EXCLUDED.request_hash, response_code = NULL, response_body = NULL, created_at = now()
  WHERE idempotency_keys.created_at < now() - make_interval(hours => :ttl)
RETURNING 1
"""
)


def request_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


async def claim(session: AsyncSession, user_id: UUID, key: str, body_hash: str) -> tuple[int, dict] | None:
    """None means "you own this key, do the work". Otherwise the stored (status, body) to replay."""
    claimed = await session.execute(
        _CLAIM, {"uid": user_id, "key": key, "hash": body_hash, "ttl": settings.idempotency_ttl_hours}
    )
    if claimed.first() is not None:
        return None
    existing = await session.get(IdempotencyKey, (user_id, key), populate_existing=True)
    if existing.request_hash != body_hash:
        raise AppError(422, "idempotency_key_reuse", "This Idempotency-Key was already used with a different request body")
    return existing.response_code, existing.response_body


async def store(session: AsyncSession, user_id: UUID, key: str, status: int, body: dict) -> None:
    await session.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.user_id == user_id, IdempotencyKey.key == key)
        .values(response_code=status, response_body=body)
    )

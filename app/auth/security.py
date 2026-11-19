import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.config import settings

_ph = PasswordHasher()
# Verified against when the email is unknown, so response time does not reveal which emails exist.
_DUMMY_HASH = _ph.hash("not-a-real-password")


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_ph.hash, password)  # argon2 is CPU heavy: keep it off the event loop


async def verify_password(password_hash: str | None, password: str) -> bool:
    def _verify() -> bool:
        try:
            return _ph.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
        except (VerificationError, InvalidHashError):
            return False

    return await asyncio.to_thread(_verify)


def create_access_token(user_id: str) -> str:
    now = datetime.now(timezone.utc)
    claims = {"sub": user_id, "iat": now, "exp": now + timedelta(minutes=settings.access_ttl_minutes), "typ": "access"}
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256")


def decode_access_token(token: str) -> str | None:
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"], options={"require": ["exp", "sub"]})
    except jwt.PyJWTError:
        return None
    return claims["sub"] if claims.get("typ") == "access" else None


def new_refresh_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(48)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()

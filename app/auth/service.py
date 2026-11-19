from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ratelimit
from app.auth.schemas import RegisterIn, TokenOut
from app.auth.security import (
    create_access_token,
    hash_password,
    hash_token,
    new_refresh_token,
    verify_password,
)
from app.config import settings
from app.errors import AppError
from app.models import RefreshToken, User


async def register(session: AsyncSession, data: RegisterIn) -> User:
    user = User(email=data.email.lower(), password_hash=await hash_password(data.password), base_currency=data.base_currency)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise AppError(409, "email_taken", "An account with this email already exists") from None
    return user


async def _issue(session: AsyncSession, user_id: UUID) -> TokenOut:
    raw, digest = new_refresh_token()
    session.add(
        RefreshToken(
            user_id=user_id, token_hash=digest, expires_at=datetime.now(timezone.utc) + timedelta(days=settings.refresh_ttl_days)
        )
    )
    await session.commit()
    return TokenOut(
        access_token=create_access_token(str(user_id)), refresh_token=raw, expires_in=settings.access_ttl_minutes * 60
    )


async def login(session: AsyncSession, email: str, password: str) -> TokenOut:
    email = email.lower()
    await ratelimit.register_attempt(email)  # counted before the password check, so concurrent guesses can't race past the limit
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if not await verify_password(user.password_hash if user else None, password):
        raise AppError(401, "invalid_credentials", "Invalid email or password")
    await ratelimit.reset(email)
    return await _issue(session, user.id)


async def refresh(session: AsyncSession, raw_token: str) -> TokenOut:
    digest = hash_token(raw_token)
    # Atomic single-use: only one concurrent caller can flip used_at from NULL.
    row = (
        await session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.token_hash == digest,
                RefreshToken.used_at.is_(None),
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > datetime.now(timezone.utc),
            )
            .values(used_at=datetime.now(timezone.utc))
            .returning(RefreshToken.user_id)
        )
    ).first()
    if row is not None:
        return await _issue(session, row.user_id)

    known = (await session.execute(select(RefreshToken).where(RefreshToken.token_hash == digest))).scalar_one_or_none()
    if known is not None and known.expires_at > datetime.now(timezone.utc) and known.revoked_at is None:
        # A rotated (used) token came back: assume theft and kill every session of this user.
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == known.user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(timezone.utc))
        )
        await session.commit()  # must persist even though we answer with an error
        raise AppError(401, "refresh_token_reused", "Refresh token was already used; all sessions were revoked")
    raise AppError(401, "invalid_refresh_token", "Invalid or expired refresh token")


async def logout(session: AsyncSession, raw_token: str) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == hash_token(raw_token), RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )
    await session.commit()

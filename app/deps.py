from typing import Annotated
from uuid import UUID

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import decode_access_token
from app.db import get_session
from app.errors import AppError
from app.models import User

_bearer = HTTPBearer(auto_error=False)
DbSession = Annotated[AsyncSession, Depends(get_session)]


async def get_current_user(session: DbSession, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]) -> User:
    unauthorized = AppError(401, "unauthorized", "Invalid or missing access token", headers={"WWW-Authenticate": "Bearer"})
    if creds is None:
        raise unauthorized
    sub = decode_access_token(creds.credentials)
    if sub is None:
        raise unauthorized
    user = await session.get(User, UUID(sub))
    if user is None:
        raise unauthorized
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]

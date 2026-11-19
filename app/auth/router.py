from fastapi import APIRouter, Response

from app.auth import service
from app.auth.schemas import LoginIn, RefreshIn, RegisterIn, TokenOut, UserOut
from app.deps import DbSession

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=201, response_model=UserOut)
async def register(body: RegisterIn, session: DbSession):
    return await service.register(session, body)


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, session: DbSession):
    return await service.login(session, body.email, body.password)


@router.post("/refresh", response_model=TokenOut)
async def refresh(body: RefreshIn, session: DbSession):
    return await service.refresh(session, body.refresh_token)


@router.post("/logout", status_code=204)
async def logout(body: RefreshIn, session: DbSession):
    await service.logout(session, body.refresh_token)
    return Response(status_code=204)

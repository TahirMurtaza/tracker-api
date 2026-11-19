from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.config import settings

Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


def check_supported(v: str) -> str:
    if v not in settings.supported_currencies:
        raise ValueError(f"unsupported currency; use one of {', '.join(settings.supported_currencies)}")
    return v


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    base_currency: Currency

    _supported = field_validator("base_currency")(check_supported)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=128)


class RefreshIn(BaseModel):
    refresh_token: str = Field(max_length=256)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    email: str
    base_currency: str
    created_at: datetime


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str
    jwt_secret: str  # required: fail fast instead of shipping a default secret

    access_ttl_minutes: int = 15
    refresh_ttl_days: int = 7
    login_max_attempts: int = 5
    login_window_seconds: int = 15 * 60

    idempotency_ttl_hours: int = 24
    supported_currencies: tuple[str, ...] = ("PKR", "AED", "USD", "EUR")

    s3_bucket: str = "tracker-receipts"
    s3_endpoint_url: str | None = None  # set for MinIO/LocalStack; unset on AWS
    aws_region: str = "us-east-1"
    # Dev-only. In AWS leave unset so boto3 uses the instance/task IAM role.
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    max_receipt_bytes: int = 5 * 1024 * 1024
    receipt_download_ttl_seconds: int = 15 * 60
    max_import_bytes: int = 50 * 1024 * 1024
    import_chunk_size: int = 1000

    @field_validator("s3_endpoint_url", "aws_access_key_id", "aws_secret_access_key", mode="before")
    @classmethod
    def _empty_is_none(cls, v):
        return v or None


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

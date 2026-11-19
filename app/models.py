import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Money is NUMERIC end to end; never float.
Money = Numeric(14, 2)


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()"))


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    base_currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = _now()


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_categories_user_name"),
        CheckConstraint("monthly_budget IS NULL OR monthly_budget > 0", name="ck_categories_budget_positive"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    monthly_budget: Mapped[Decimal | None] = mapped_column(Money)  # in the user's base currency
    created_at: Mapped[datetime] = _now()


class ImportJob(Base):
    __tablename__ = "import_jobs"
    # Same file uploaded twice by the same user maps to the same job.
    __table_args__ = (
        UniqueConstraint("user_id", "file_sha256", name="uq_import_jobs_user_file"),
        Index("ix_import_jobs_claimable", "status", "updated_at"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    file_sha256: Mapped[str] = mapped_column(String(64))
    s3_key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    total: Mapped[int] = mapped_column(Integer, default=0)
    done: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Expense(Base):
    __tablename__ = "expenses"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_expenses_amount_positive"),
        # Keyset listing: WHERE user_id=? ORDER BY spent_on DESC, id DESC.
        Index("ix_expenses_user_spent_id", "user_id", text("spent_on DESC"), text("id DESC")),
        # Category-filtered listing and per-category month sums (report, budget checks).
        Index("ix_expenses_user_cat_spent_id", "user_id", "category_id", text("spent_on DESC"), text("id DESC")),
        # Makes CSV re-processing idempotent: one row per (import, line).
        Index("uq_expenses_import_row", "import_id", "import_row", unique=True, postgresql_where=text("import_id IS NOT NULL")),
        # ILIKE '%q%' search on description.
        Index(
            "ix_expenses_description_trgm", "description", postgresql_using="gin", postgresql_ops={"description": "gin_trgm_ops"}
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("categories.id", ondelete="RESTRICT"))
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    spent_on: Mapped[date] = mapped_column(Date)
    receipt_key: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    import_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("import_jobs.id", ondelete="SET NULL"))
    import_row: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class ExchangeRate(Base):
    """rate_to_usd = USD per 1 unit of `currency` (so USD itself is 1)."""

    __tablename__ = "exchange_rates"
    __table_args__ = (CheckConstraint("rate_to_usd > 0", name="ck_rates_positive"),)
    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    valid_on: Mapped[date] = mapped_column(Date, primary_key=True)
    rate_to_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8))


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    __table_args__ = (Index("ix_refresh_tokens_user", "user_id"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256; the raw token is never stored
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (Index("ix_idempotency_created", "created_at"),)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    response_code: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        # Tiny index containing only pending events, so the worker's claim query stays cheap during backlogs.
        Index("ix_outbox_pending", "id", postgresql_where=text("processed_at IS NULL")),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    type: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        # The exactly-once guarantee: one alert per user, category, month and level.
        UniqueConstraint("user_id", "category_id", "month", "level", name="uq_notifications_once"),
        Index("ix_notifications_user_id_desc", "user_id", text("id DESC")),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    month: Mapped[date] = mapped_column(Date)
    level: Mapped[int] = mapped_column(SmallInteger)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()

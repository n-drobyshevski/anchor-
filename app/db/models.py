"""SQLAlchemy 2.0 declarative models.

1a defined only TelegramUpdate — the inbound queue and dedup table. 1b
adds the rest of the Phase 1 schema (plan section 5): message,
user_state, state_change, persona_version, spend_ledger.

All "text" columns from the plan's SQL use SQLAlchemy's unlimited
String, matching TelegramUpdate.status/error above, rather than Text —
same Postgres column type, kept for style consistency.
"""

from __future__ import annotations

import datetime
import decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TelegramUpdate(Base):
    __tablename__ = "telegram_update"

    # Telegram's own update_id, provided explicitly on insert — not a
    # generated identity column.
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_telegram_update_status_update_id", "status", "update_id"),)


class Message(Base):
    """A stored chat message, user or assistant side (plan section 5).

    1b only ever wrote role="user" rows. 1c's idempotent turn
    (app/core/turn.py) writes both: the user row (moved there from
    router.py) and the assistant row (reply_to_update, sent_at, usage,
    cost).
    """

    __tablename__ = "message"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    role: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(String, nullable=False)
    ooc: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    update_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("telegram_update.update_id")
    )
    # Assistant rows' idempotency key (plan section 8 step 2) — unused
    # until 1c writes assistant rows.
    reply_to_update: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    sent_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    model: Mapped[str | None] = mapped_column(String)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_cached: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    usd_cost: Mapped[decimal.Decimal | None] = mapped_column(Numeric(10, 6))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserState(Base):
    """The singleton row of live bot state (plan section 5).

    id is pinned to 1 by a default and a check constraint, so there is
    always exactly one row and it is never ambiguous which one to read.
    autoincrement=False mirrors TelegramUpdate.update_id: this id is a
    fixed sentinel, not a generated identity, so Alembic must not invent
    a sequence for it.
    """

    __tablename__ = "user_state"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=False, server_default=text("1")
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    persona_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    intensity: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default=text("3")
    )
    timezone: Mapped[str] = mapped_column(
        String, nullable=False, default="Europe/Paris", server_default=text("'Europe/Paris'")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_user_state_id_singleton"),
        CheckConstraint("intensity between 1 and 5", name="ck_user_state_intensity_range"),
    )


class StateChange(Base):
    """Audit log for every user_state field write (plan section 5)."""

    __tablename__ = "state_change"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    field: Mapped[str | None] = mapped_column(String)
    old_value: Mapped[str | None] = mapped_column(String)
    new_value: Mapped[str | None] = mapped_column(String)
    source: Mapped[str | None] = mapped_column(String)  # command|pause|system
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PersonaVersion(Base):
    """One row per distinct persona.md content, keyed by its sha256 (plan section 5)."""

    __tablename__ = "persona_version"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sha256: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    body: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SpendLedger(Base):
    """Per-call spend rows (plan section 5); today_usd() sums these for /state.

    Written by app/core/turn.py alongside each assistant message row,
    in the same transaction. Cost calculation and cap enforcement both
    live in app/core/spend.py (compute_cost, check_cap).
    """

    __tablename__ = "spend_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    local_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)  # chat|ooc
    model: Mapped[str | None] = mapped_column(String)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_cached: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    usd_cost: Mapped[decimal.Decimal] = mapped_column(Numeric(10, 6), nullable=False)

    __table_args__ = (Index("ix_spend_ledger_local_date", "local_date"),)

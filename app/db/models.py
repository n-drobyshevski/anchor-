"""SQLAlchemy 2.0 declarative models.

1a defined only TelegramUpdate — the inbound queue and dedup table. 1b
adds the rest of the Phase 1 schema (plan section 5): message,
user_state, state_change, persona_version, spend_ledger.

2b adds `memory` and `pending_memory` (phase-2 plan sections 4 and 11).

2a adds the phase-2 plan's generic `job` queue (section 3) and `scene`
(section 4), plus two columns on `message`: `scene_id` and `kind`. The
`kind` check constraint is not in the plan's SQL, which leaves it as a
comment -- it is added here because every other enum-shaped column in
that plan (memory.kind, checkin.due_result, proposal.field/status) does
carry one, and because a mistyped kind would silently defeat the
welfare/canned exclusion that scene summaries and the transcript rely
on. That exclusion is a privacy property, so it gets a constraint.

All "text" columns from the plan's SQL use SQLAlchemy's unlimited
String, matching TelegramUpdate.status/error above, rather than Text —
same Postgres column type, kept for style consistency.
"""

from __future__ import annotations

import datetime
import decimal

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Float,
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


class Job(Base):
    """A unit of deferred background work (phase-2 plan section 3).

    Deliberately the same column vocabulary as TelegramUpdate above --
    status/attempts/locked_at/error -- so app/db/queue.py's generic
    mechanics drive both tables with one implementation.

    `dedup_key` is nullable and unique: an ON CONFLICT DO NOTHING insert
    keyed on it makes enqueueing idempotent (e.g. 'scene:<id>' can only
    ever queue one summary), while a NULL key means "no deduplication"
    and always inserts, since NULLs do not conflict in a unique index.

    `run_after` gates claiming; phase 3 will use it for scheduled kinds.
    """

    __tablename__ = "job"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # extract|summarize_scene
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    dedup_key: Mapped[str | None] = mapped_column(String, unique=True)
    run_after: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", server_default=text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    locked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_job_status_run_after", "status", "run_after"),)


class Scene(Base):
    """One conversational session (phase-2 plan section 4).

    Closed by app/core/scene.py after SCENE_IDLE_HOURS of silence, at
    which point a `summarize_scene` job fills `summary`. A scene with
    fewer than 3 summarizable messages is never summarized and keeps
    summary=NULL, which is a valid terminal state, not a pending one.
    """

    __tablename__ = "scene"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[str | None] = mapped_column(String)
    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


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

    # 2a (phase-2 plan section 4). scene_id is nullable because Phase 1
    # rows predate scenes; kind defaults to 'chat' for the same reason.
    scene_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("scene.id"))
    kind: Mapped[str] = mapped_column(
        String, nullable=False, default="chat", server_default=text("'chat'")
    )

    __table_args__ = (
        CheckConstraint(
            "kind in ('chat', 'checkin', 'welfare', 'canned', 'system')",
            name="ck_message_kind",
        ),
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
    # chat|ooc (Phase 1) plus 2a's extractor|summary|welfare|checkin
    # (phase-2 plan section 2). Deliberately unconstrained, as in Phase 1:
    # the ledger is an append-only record of money already spent, and a
    # constraint that rejected an unrecognized category would lose the
    # row rather than the label.
    category: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str | None] = mapped_column(String)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_cached: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    usd_cost: Mapped[decimal.Decimal] = mapped_column(Numeric(10, 6), nullable=False)

    __table_args__ = (Index("ix_spend_ledger_local_date", "local_date"),)


class Memory(Base):
    """A durable fact about the user (phase-2 plan section 4).

    Active means `superseded_by IS NULL`. A fact is never edited in
    place: a correction is a new row whose predecessor is pointed at it,
    so the history of what the bot believed stays readable.

    **The `text` column shadows sqlalchemy's `text()`** for the rest of
    this class body, which is why `sa.text(...)` is used for the
    server_defaults below rather than the bare `text(...)` every other
    model in this file uses. Declaring the column last would also work,
    but would leave a trap for whoever adds the next column.

    Two constraints beyond the plan's SQL. `ck_memory_no_self_supersede`
    stops a row from retiring itself, which would make it permanently
    invisible with no way to find it. Chains are kept linear by
    app/core/memory.py, which only ever supersedes an active row -- that
    is what makes /forget's relink (see hard_delete) unambiguous.
    """

    __tablename__ = "memory"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    text: Mapped[str] = mapped_column(String, nullable=False)
    pinned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa.text("false")
    )
    source: Mapped[str] = mapped_column(String, nullable=False)  # user|extractor|adopt
    confidence: Mapped[float | None] = mapped_column(Float)
    superseded_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("memory.id"))
    last_used_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    use_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "kind in ('identity', 'preference', 'event', 'rule', 'technique')",
            name="ck_memory_kind",
        ),
        # char_length, not octet_length: 300 characters, which for
        # Cyrillic is not 300 bytes.
        CheckConstraint('char_length("text") <= 300', name="ck_memory_text_length"),
        CheckConstraint("superseded_by <> id", name="ck_memory_no_self_supersede"),
        Index("memory_trgm", "text", postgresql_using="gin", postgresql_ops={"text": "gin_trgm_ops"}),
    )


class PendingMemory(Base):
    """Text from /remember, parked until the user picks a kind (plan section 11).

    Section 11 offers `user_state.awaiting_ref` or "a small
    pending_memory row if simpler". This is the simpler one twice over:
    `awaiting`/`awaiting_ref` are milestone 2d columns and would be dead
    state here, and a single scalar would clobber the first text if the
    user sent `/remember A` and `/remember B` before pressing either
    keyboard. A table keeps both, and each keyboard's callback carries
    its own row id.

    The row is deleted when its kind button is pressed, which is what
    makes a replayed callback idempotent: the second press finds nothing
    and is answered "Устарело".
    """

    __tablename__ = "pending_memory"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint('char_length("text") <= 300', name="ck_pending_memory_text_length"),
    )

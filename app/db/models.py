"""SQLAlchemy 2.0 declarative models.

3a adds `outbound` (phase-3 plan section 4), five `user_state`
counters, and `message.outbound_id`. `message.kind` gains 'outbound',
because plan section 7 puts proactive messages *in* the persona
transcript -- Anchor has to remember what it said unprompted, or it
will repeat itself.

1a defined only TelegramUpdate — the inbound queue and dedup table. 1b
adds the rest of the Phase 1 schema (plan section 5): message,
user_state, state_change, persona_version, spend_ledger.

2d adds `checkin` (phase-2 plan sections 4 and 9) and the last four
`user_state` columns.

2c adds `journal` and `proposal` (phase-2 plan sections 4 and 8), and
four `user_state` columns.

Those four -- focus_on/focus_since/due_action/due_set_at -- are listed
under milestone 2d in plan section 15, but section 8's proposal-accept
path writes them, and that path ships in 2c. They arrive here with only
the button as a writer; 2d adds the /due and /focus commands that also
write them, plus the streak/check-in columns 2c has no use for.

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
    UniqueConstraint,
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

    # 3a (phase-3 plan section 4). The idempotency key for a proactive
    # send, exactly as reply_to_update is for a reply: unique, so the
    # "did I already generate this?" check in plan section 7 step 2 is
    # a lookup the database enforces rather than a race the worker's
    # concurrency-of-1 happens to hide.
    outbound_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("outbound.id"), unique=True
    )

    __table_args__ = (
        CheckConstraint(
            "kind in ('chat', 'checkin', 'welfare', 'canned', 'system', 'outbound')",
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

    # 2c (phase-2 plan section 4). Written only by an accepted proposal
    # button today; 2d adds /due and /focus as the other writers. The
    # extractor can never reach them -- see app/core/extract.py.
    focus_on: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    focus_since: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    due_action: Mapped[str | None] = mapped_column(String)
    due_set_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    # 2d (phase-2 plan section 4). `awaiting` names a pending
    # conversational step -- only 'checkin_note' exists today -- and
    # `awaiting_ref` is the row it refers to (a checkin.id). Both are
    # cleared by a pause word or any slash command (plan section 9), so
    # they can never strand the plain-text path.
    streak: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_checkin_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    awaiting: Mapped[str | None] = mapped_column(String)
    awaiting_ref: Mapped[int | None] = mapped_column(BigInteger)

    # 3a (phase-3 plan section 4). The counters the outbound gate
    # reads. None of them are written by a model -- `quiet_until` by
    # /quiet, `welfare_at` by the welfare trigger, and the other three
    # by app/core/outbound.py's two counter functions.
    #
    # `last_user_msg_at` is *any* inbound update (text, command or
    # button press), not just a message that produced a turn: pressing
    # [Чек-ин] is the user being present, and a bot that nagged
    # someone mid-check-in would be obviously broken.
    #
    # `ignored_in_row` is the back-off. It counts sent-but-unanswered
    # outbound messages and resets to 0 on any inbound update, so the
    # bot notices it is being ignored and stops -- fixed intents
    # included (plan section 11).
    quiet_until: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    last_user_msg_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    last_outbound_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    ignored_in_row: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    welfare_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_user_state_id_singleton"),
        CheckConstraint("intensity between 1 and 5", name="ck_user_state_intensity_range"),
        CheckConstraint("ignored_in_row >= 0", name="ck_user_state_ignored_non_negative"),
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


class Journal(Base):
    """One neutral sentence per notable exchange (phase-2 plan section 8).

    Written by the extractor and nothing else. Unlike `memory` it is
    never retrieved into a prompt -- it exists so the user can read back
    what happened, and so later milestones have a factual spine for
    digests. 240 characters because section 8's schema says so.
    """

    __tablename__ = "journal"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    local_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    text: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint('char_length("text") <= 240', name="ck_journal_text_length"),
        Index("ix_journal_local_date", "local_date"),
    )


class Proposal(Base):
    """A change the extractor suggested and the user has not agreed to yet.

    This table is the whole reason the extractor is safe (plan sections
    8 and 13). Model output never reaches `user_state` or a rule memory
    directly; it lands here as `pending` and only a button press applies
    it. `tg_message_id` is kept so an expired proposal's buttons can be
    edited away rather than left live on a decision that no longer
    stands.

    Exactly one proposal is pending at a time -- see
    app/core/proposal.py.
    """

    __tablename__ = "proposal"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    field: Mapped[str] = mapped_column(String, nullable=False)  # due_action|focus_on|rule
    value: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", server_default=text("'pending'")
    )
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decided_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "field in ('due_action', 'focus_on', 'rule')", name="ck_proposal_field"
        ),
        CheckConstraint(
            "status in ('pending', 'accepted', 'rejected', 'expired')",
            name="ck_proposal_status",
        ),
        Index("ix_proposal_status", "status"),
    )


class Checkin(Base):
    """One day's structured check-in (phase-2 plan sections 4 and 9).

    The row **is** the state machine for the flow: /checkin upserts
    today's row with the answer fields nulled, and each button fills one
    of them. That is also why section 9's "a second check-in the same day
    overwrites the first" needs no special handling -- `local_date` is
    unique, so starting again simply resets the day's row.

    `tg_message_id` is not in the plan's SQL, but section 9's stale-button
    rule cannot be implemented without it: the callback data it specifies
    (`c:r:<n>`) carries no check-in id, so "is this button from the
    current check-in?" can only be answered by comparing message ids.
    `proposal` carries the same column for the same reason.
    """

    __tablename__ = "checkin"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    local_date: Mapped[datetime.date] = mapped_column(Date, nullable=False, unique=True)
    day_rating: Mapped[int | None] = mapped_column(Integer)
    due_result: Mapped[str | None] = mapped_column(String)  # done|partial|no|none
    note: Mapped[str | None] = mapped_column(String)
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("day_rating between 1 and 5", name="ck_checkin_day_rating"),
        CheckConstraint(
            "due_result in ('done', 'partial', 'no', 'none')", name="ck_checkin_due_result"
        ),
        CheckConstraint('char_length("note") <= 500', name="ck_checkin_note_length"),
    )


class Outbound(Base):
    """One planned, sent, skipped or cancelled proactive message (phase-3 plan section 4).

    The row is the unit of exactly-once delivery. `unique (kind,
    local_date, bucket)` is what makes a duplicate heartbeat, a
    redeploy mid-morning, or two processes overlapping during a Railway
    rollout all collapse into one message: the second insert conflicts
    and does nothing. Nothing in the send path depends on the worker
    being single-threaded.

    `bucket` disambiguates several rows of the same kind on the same
    day. For a tick it is the local hour it was decided in; for the
    fixed intents and the silence nudge it is 0, because there is at
    most one of each per local date by definition. The 48-hour rule in
    plan section 5 is what keeps silence nudges apart -- local_date
    alone would permit one every midnight.

    **`status` is the whole lifecycle**, and only `planned` is live:

    - `planned`  -- a row exists and a send_outbound job is queued.
    - `sent`     -- delivered; `message_id`, `sent_at` are set.
    - `skipped`  -- the send-time gate refused it; `skip_reason` says
                    which check, and /state shows it.
    - `cancelled`-- a pause, welfare trigger, /quiet or /delete revoked
                    it before it went out (plan section 6).
    - `failed`   -- generation failed after retries. Nothing is sent.
                    There is deliberately no canned fallback: a
                    proactive message the user did not ask for has to
                    earn its place, and boilerplate does not.

    `message_id` points at the delivered message; `message.outbound_id`
    points back. Both directions are in the plan's SQL, and the
    message-side one is the idempotency key the send job actually
    reads. The FK here is `use_alter` because the two tables reference
    each other -- without it, metadata sorting cannot order the CREATEs.
    """

    __tablename__ = "outbound"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    local_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    bucket: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    planned_for: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="planned", server_default=text("'planned'")
    )
    skip_reason: Mapped[str | None] = mapped_column(String)
    tick_note: Mapped[str | None] = mapped_column(String)
    message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("message.id", use_alter=True, name="fk_outbound_message_id")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("kind", "local_date", "bucket", name="uq_outbound_kind_date_bucket"),
        CheckConstraint(
            "kind in ('morning', 'evening_nag', 'silence', 'tick')", name="ck_outbound_kind"
        ),
        CheckConstraint(
            "status in ('planned', 'sent', 'skipped', 'cancelled', 'failed')",
            name="ck_outbound_status",
        ),
        CheckConstraint('char_length("tick_note") <= 120', name="ck_outbound_tick_note_length"),
        # Both live queries are "planned rows, by when they are due":
        # cancel_outbound sweeps them, /state shows the next one.
        Index("ix_outbound_status_planned_for", "status", "planned_for"),
        Index("ix_outbound_local_date", "local_date"),
    )

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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TelegramUpdate(Base):
    """The inbound queue (plan section 2 / 6.3), plus the web-chat transport.

    Origin is derived from the *sign* of `update_id`, not a stored
    column: Telegram's own ids are always non-negative, and every
    web-origin row gets a negative id from `web_update_seq`
    (app/db/queue.py's `enqueue_web`), so `update_id < 0` is exactly
    "this came from the browser" with no column that could ever drift
    out of sync with it. This replaces an earlier design (web-chat plan
    track 1) that added `source`/`client_key` columns and two CHECK
    constraints directly on this table via an ALTER; that ALTER takes an
    ACCESS EXCLUSIVE lock and hung a Railway deploy exactly the way
    migration f7da7c8741fd did (see that migration's docstring and
    commits 2cd24c2/068e7e3), so the web-chat plan's own schema was
    reworked the same way: no ALTER on this hot table. The idempotency
    key for a web-origin row lives on `WebUpdate.client_key` instead, in
    a table this one has no foreign key into (an FK would itself take a
    SHARE ROW EXCLUSIVE lock here) -- see `app/db/queue.py`'s
    `enqueue_web` for how the two rows are written together.
    """

    __tablename__ = "telegram_update"

    # Telegram's own update_id, provided explicitly on insert — not a
    # generated identity column. Web rows get a negative id from
    # web_update_seq instead (app/db/queue.py's enqueue_web).
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    # 6e (migration f7da7c8741fd, already applied in production): the
    # column is nullable in the schema, but the retention sweep (app/core/
    # retention.py's forget_update_payloads) blanks old payloads to {}
    # rather than NULL, so nothing ever writes a NULL here.
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_telegram_update_status_update_id", "status", "update_id"),
    )


class WebUpdate(Base):
    """The web-chat transport's own idempotency marker (web-chat plan
    track 2, reworked to avoid an ALTER on `telegram_update` -- see that
    model's docstring for why).

    One row per web-origin update, `update_id`-keyed to the matching
    `telegram_update` row but with **no foreign key** to it: an FK
    constraint takes a SHARE ROW EXCLUSIVE lock on the referenced table
    at creation time, which is exactly the kind of lock this rework
    exists to avoid taking on `telegram_update`. The two rows are
    written together, in one transaction, by `app/db/queue.py`'s
    `enqueue_web` -- application code keeps them in sync since the
    database no longer does.

    `client_key` is POST /api/send's idempotency key, nullable because a
    retried request may not always carry one, with a partial unique
    index (see the migration) rather than a plain one, so NULL rows are
    never compared against each other for uniqueness. `enqueue_web`
    conflicts on this index (`ON CONFLICT (client_key) WHERE client_key
    IS NOT NULL DO NOTHING`), then re-selects on a conflict, so a
    retried POST with the same key returns the same `update_id`.

    Holds no conversation content -- see app/core/purge.py's
    PURGED_TABLES (it is purged like `web_session`) and
    tests/test_export.py's NOT_EXPORTED (it is not exported, for the
    same reason).
    """

    __tablename__ = "web_update"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    client_key: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "uq_web_update_client_key",
            "client_key",
            unique=True,
            postgresql_where=text("client_key IS NOT NULL"),
        ),
    )


class WebSession(Base):
    """A logged-in web-chat session (web-chat plan section 4, track 1).

    `token_hash` is the primary key rather than a surrogate id: the only
    read this table ever serves is "does this cookie's hashed token name
    a live session" (app/web/auth.py, track 2), so a surrogate id would
    be a second key nothing looks up by. Only sha256(token) is ever
    stored -- never the token itself -- the same shape as
    TELEGRAM_SECRET_TOKEN's hmac.compare_digest check in
    app/tg/webhook.py: a leaked row cannot be replayed as a cookie.

    `expires_at` is the absolute session ceiling (WEB_SESSION_MAX_DAYS);
    `last_seen_at` is the idle timeout's clock (WEB_SESSION_IDLE_HOURS),
    both enforced by track 2's session validation, not by a database
    constraint -- there is no CHECK here for the same reason
    `user_state.quiet_until` has none: "is this still valid" depends on
    the current time, which a CHECK constraint cannot read.
    """

    __tablename__ = "web_session"

    token_hash: Mapped[bytes] = mapped_column(sa.LargeBinary, primary_key=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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

    # 5a (phase-5 plan sections 2 and 5). The one piece of nickname
    # state that survives between turns: which nickname app/core/
    # voice.py used last, so choose_nickname() never repeats it back to
    # back. Written only by voice.remember_nickname()'s targeted
    # UPDATE -- never through update_state(), so a nickname rotation
    # leaves no state_change row (it is not a decision worth auditing,
    # the same reasoning set_counters() already applies to the traffic
    # counters above).
    nickname_last: Mapped[str | None] = mapped_column(String)

    # 5e (phase-5 plan sections 2, 3 and 11a). The last scene that got a
    # callback ("## Можно вспомнить"), so app/core/callbacks.py can tell
    # "this scene already had its one callback" from "this is a new
    # scene, check again" without a second table. Written only by
    # app/core/callbacks.py's own targeted UPDATE (`mark_delivered`) --
    # never through update_state(), the same narrow-writer pattern
    # app/core/voice.py's `nickname_last` already established.
    #
    # **No foreign key**, deliberately, though the plan allows one: this
    # column lives on `user_state`, a KEPT table (app/core/purge.py),
    # while `scene` is PURGED. purge.py's own TRUNCATE is intentionally
    # CASCADE-free (see that module's docstring -- "if a future table
    # ever references a purged one and is not itself listed here, the
    # statement fails loudly"), and Postgres enforces that at the
    # statement level regardless of the FK's ON DELETE action: TRUNCATE
    # refuses outright when a table outside the statement references one
    # inside it. A bare bigint avoids reintroducing exactly the failure
    # mode that invariant exists to catch -- `reset_values` below already
    # nulls this column on every `/delete`, which is what an ON DELETE
    # SET NULL would have bought anyway.
    callback_scene: Mapped[int | None] = mapped_column(BigInteger)

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
    # H4: 'vendor' when OpenRouter reported the cost itself, 'computed'
    # when usd_cost is our arithmetic over token counts at config prices.
    # They are not the same kind of number -- one is what was charged,
    # the other is an estimate that a stale price setting can silently
    # skew -- and a row that does not say which it is cannot be audited.
    #
    # Nullable, with no backfill: rows written before H4 genuinely do not
    # know, and inventing a value for them would be the one thing this
    # column exists to prevent.
    cost_source: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(
            "cost_source is null or cost_source in ('vendor', 'computed')",
            name="ck_spend_ledger_cost_source",
        ),
        Index("ix_spend_ledger_local_date", "local_date"),
    )


class SafetyEvent(Base):
    """One row per safety-model call outcome (H2). Never any content.

    Separate from spend_ledger, not a column on it, for a reason the
    code makes concrete: app/core/turn.py's _ledger_only() returns early
    when the response is None, so a classifier that *timed out* writes no
    ledger row at all -- the table is structurally unable to record the
    outcome that matters most. The converse is just as bad: a timeout, an
    error and a fallback_hit cost nothing, so recording them as
    zero-cost ledger rows would pollute today_by_category() and the
    daily-cap query, and the cap is row 5 of the outbound gate -- noise
    there silences proactive messages.

    Constrained, unlike spend_ledger.category. That column is
    deliberately open because it records money already spent and a
    rejected row would lose the record. Here both vocabularies are
    closed sets of module constants (app/core/welfare.py), an
    unrecognized value is a bug rather than a new label, and
    tests/test_safety_event.py pins the constants against these
    constraints. The write itself is best-effort at the call site:
    observability must never be able to fail the turn it observes.
    """

    __tablename__ = "safety_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    local_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    # `kind`, not `check`: `check` is a reserved word in Postgres, and
    # the name would need quoting in every raw constraint expression.
    # `kind` also matches job.kind / message.kind / outbound.kind.
    kind: Mapped[str] = mapped_column(String, nullable=False)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(
            "kind in ('welfare', 'extractor', 'tick', 'distill', 'search', 'notebook', 'review')",
            name="ck_safety_event_kind",
        ),
        CheckConstraint(
            "outcome in ('ok', 'parse_fail', 'timeout', 'error', 'fallback_hit')",
            name="ck_safety_event_outcome",
        ),
        # The only query is /state's "last 7 local days, grouped by
        # outcome", the same shape as the ledger's daily sum.
        Index("ix_safety_event_local_date", "local_date"),
    )


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

    Written by the extractor and nothing else.

    **Amended in 3d.** This docstring used to say the journal is "never
    retrieved into a prompt". That is no longer true: phase-3 plan
    section 8 puts the last three lines into the tick decision's input,
    because "what has actually been happening" is most of what makes a
    reason to write first natural rather than invented. It is still
    never retrieved into the *persona* prompt -- only into the cheap
    model's decision, which produces a boolean and a note, never a
    reply. Unlike `memory` it is not ranked or retrieved by similarity;
    the tick takes the most recent three, full stop.

    240 characters because section 8's schema says so.
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
        # 5c widens this with 'standing_order' -- the extractor's own
        # proposal item (app/core/extract.py) can carry that field, even
        # though a proposal row with it is never actually inserted:
        # _apply() routes a standing_order proposal to app/core/
        # orders.propose() instead, which writes its own `standing_order`
        # row with the negotiation statuses that field needs (§"Where
        # proposals live"). The widening keeps proposal.FIELDS -- the
        # enum this constraint mirrors -- and the schema in step, so a
        # future caller cannot construct a Proposal this constraint
        # would then reject for a reason unrelated to the one enforced
        # in code.
        CheckConstraint(
            "field in ('due_action', 'focus_on', 'rule', 'standing_order')",
            name="ck_proposal_field",
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
            "kind in ('morning', 'evening_nag', 'silence', 'tick', 'weekly_review')",
            name="ck_outbound_kind",
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


class StudyJob(Base):
    """One research request: a /study topic or a /read URL (phase-4 plan section 4).

    `error_code` is a code from app/research/errors.py, never a message
    from a stranger's web server -- plan section 12 keeps free text from
    the web out of the database as firmly as it keeps it out of the logs.

    `local_date` is the day the quota counts against, stamped from the
    user's timezone by the caller rather than derived here, because
    "today" is a clock question and app/core/clock.py owns those.
    """

    __tablename__ = "study_job"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    packet: Mapped[str | None] = mapped_column(String)
    query: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="queued", server_default=text("'queued'")
    )
    searches_used: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    pins_used: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    usd_cost: Mapped[decimal.Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=decimal.Decimal("0"), server_default=text("0")
    )
    error_code: Mapped[str | None] = mapped_column(String)
    local_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("kind in ('study', 'read')", name="ck_study_job_kind"),
        CheckConstraint(
            "status in ('queued', 'searching', 'fetching', 'distilling', "
            "'done', 'failed', 'cancelled')",
            name="ck_study_job_status",
        ),
        CheckConstraint("char_length(query) <= 200", name="ck_study_job_query_length"),
        # /study's daily quota counts rows for one local date; /notes and
        # the completion message look up a job by id. Nothing else reads
        # this table, so one index is one more than none and enough.
        Index("ix_study_job_local_date", "local_date"),
    )


class StudyClip(Base):
    """One page we fetched, with its extracted main text (plan section 4).

    `url` is the URL *after* redirects -- the page we actually read, not
    the one we were pointed at -- because that is what a card's
    `source_url` has to mean and what the packet allowlist was checked
    against on the final hop.

    `text` is nulled 30 days after `fetched_at` by the retention sweep
    (plan section 4). The metadata stays: a clip's domain and status are
    how a later job knows not to re-read the same page, and they carry
    nothing from the page itself. An adopted card keeps its own copy of
    the sentence it needed.

    `fetch_error` and `text` are mutually exclusive in practice but not
    by constraint: a fetch that failed after reading a partial body is
    still a fetch we want a record of.
    """

    __tablename__ = "study_clip"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("study_job.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(String, nullable=False)
    domain: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str | None] = mapped_column(String)
    text: Mapped[str | None] = mapped_column(String)
    text_sha256: Mapped[str | None] = mapped_column(String)
    http_status: Mapped[int | None] = mapped_column(Integer)
    fetch_error: Mapped[str | None] = mapped_column(String)
    fetched_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("char_length(title) <= 300", name="ck_study_clip_title_length"),
        # The dedupe query in plan section 6 is "have we clipped this URL
        # in the last 30 days"; the retention sweep is "clips older than
        # 30 days that still have text". Both are (url|fetched_at)-shaped.
        Index("ix_study_clip_url_fetched_at", "url", "fetched_at"),
        Index("ix_study_clip_job_id", "job_id"),
    )


class StudyCard(Base):
    """A candidate technique, pending the user's decision (plan section 4).

    Three risk columns, not one, because they answer different questions
    and the difference is the audit trail. `risk_model` is what the
    distill model claimed; `risk_rules` is what app/research/risk.py
    found; `risk_final` is the max of the two. Keeping the model's claim
    means a later look can tell "the rules caught something the model
    missed" from "both agreed", which is the only way to know whether
    the rule list is earning its keep.

    `source_url` is copied from the clip by code and is never taken from
    model output (plan section 12). `rule_hits` holds rule ids only --
    never the matched text, which would put page content in a column
    that /export dumps.

    As in `Memory` above, the `text` column shadows sqlalchemy's
    `text()` for the rest of this class body, so the server_defaults use
    `sa.text(...)`.

    A `risk_final='high'` card is stored with `status='hidden'`: never
    listed, never adoptable. Stored rather than dropped so that "the
    filter is working" is observable in /export rather than inferred
    from an absence.
    """

    __tablename__ = "study_card"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("study_job.id", ondelete="CASCADE"), nullable=False
    )
    clip_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("study_clip.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    text: Mapped[str] = mapped_column(String, nullable=False)
    quote: Mapped[str] = mapped_column(String, nullable=False)
    source_url: Mapped[str] = mapped_column(String, nullable=False)
    risk_model: Mapped[str] = mapped_column(String, nullable=False)
    risk_rules: Mapped[str] = mapped_column(String, nullable=False)
    risk_final: Mapped[str] = mapped_column(String, nullable=False)
    rule_hits: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list, server_default=sa.text("'{}'")
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", server_default=sa.text("'pending'")
    )
    memory_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("memory.id"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decided_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "kind in ('technique', 'routine', 'checkin_format', 'definition')",
            name="ck_study_card_kind",
        ),
        CheckConstraint('char_length("text") <= 300', name="ck_study_card_text_length"),
        CheckConstraint("char_length(quote) <= 240", name="ck_study_card_quote_length"),
        CheckConstraint(
            "risk_model in ('low', 'medium', 'high')", name="ck_study_card_risk_model"
        ),
        CheckConstraint(
            "risk_rules in ('low', 'medium', 'high')", name="ck_study_card_risk_rules"
        ),
        CheckConstraint(
            "risk_final in ('low', 'medium', 'high')", name="ck_study_card_risk_final"
        ),
        CheckConstraint(
            "status in ('pending', 'adopted', 'rejected', 'hidden', 'expired')",
            name="ck_study_card_status",
        ),
        # Both invariants the schema can state: a high card is hidden,
        # and an adopted card has the memory it wrote. Stating them here
        # means a bug in app/research/ cannot leave an adoptable card
        # that section 12 says must never exist.
        CheckConstraint(
            "risk_final <> 'high' or status = 'hidden'", name="ck_study_card_high_is_hidden"
        ),
        CheckConstraint(
            "status <> 'adopted' or memory_id is not null",
            name="ck_study_card_adopted_has_memory",
        ),
        # /notes pages pending cards newest first; the expiry sweep reads
        # the same two columns.
        Index("ix_study_card_status_created_at", "status", "created_at"),
        Index("ix_study_card_job_id", "job_id"),
    )


class NotebookEntry(Base):
    """One of Anchor's own working notes (phase-5 plan sections 3 and 6).

    `kind` is one of `intention` (written only by the user or, from 5d,
    the weekly review -- never by `notebook_reflect`), `observation` or
    `open_thread` (both written by `notebook_reflect`, never by the
    user directly). `source` says who actually wrote this row --
    `anchor`, `user` or `review` -- and app/core/notebook.py's own
    validation, not this table, is what enforces that Anchor can never
    close or edit a `user`- or `review`-sourced row (`ck_notebook_entry_
    closed_consistent` only keeps `closed_by`/`closed_at` in step with
    `active`, it says nothing about who may set them).

    `active=false` is the only closed state; `closed_by` records who
    closed it (`anchor`, `user` or `expiry`) and is required exactly
    when `active` is false, never when it is true -- a closed row with
    no author, or an active one that already carries a closer, are both
    the kind of bug this constraint turns into a failed insert instead
    of a silent data quality problem months later.

    `scene_id` is the reflection job's own scene, kept for the
    idempotency check (`run_notebook_reflect` looks for an existing row
    with this `scene_id` before calling the model again) and for
    nothing else -- ON DELETE SET NULL because purging that scene must
    never fail the notebook wipe or leave a dangling reference.
    """

    __tablename__ = "notebook_entry"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    text: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    # `sa.text(...)`, not the bare `text(...)` every other model in this
    # file uses: this class also has a `text` *column*, and by the time
    # this line runs, that name is already bound in the class body to
    # the mapped_column() above -- the exact trap `Memory`'s own
    # docstring warns about.
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa.text("true")
    )
    closed_by: Mapped[str | None] = mapped_column(String)
    scene_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("scene.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    closed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "kind in ('intention', 'observation', 'open_thread')",
            name="ck_notebook_entry_kind",
        ),
        CheckConstraint('char_length("text") <= 240', name="ck_notebook_entry_text_length"),
        CheckConstraint(
            "source in ('anchor', 'user', 'review')", name="ck_notebook_entry_source"
        ),
        CheckConstraint(
            "closed_by is null or closed_by in ('anchor', 'user', 'expiry')",
            name="ck_notebook_entry_closed_by",
        ),
        # Plan (implementation plan §"Design decisions"): active rows
        # carry no closer, closed rows always do.
        CheckConstraint(
            "(active and closed_by is null and closed_at is null) or "
            "(not active and closed_by is not null and closed_at is not null)",
            name="ck_notebook_entry_closed_consistent",
        ),
        # The one query shape every reader needs: active rows of one
        # kind (`/mind`'s grouping, the reflection job's per-kind cap,
        # the prompt's three lines).
        Index("ix_notebook_entry_active_kind", "active", "kind"),
    )


class StandingOrder(Base):
    """A negotiated recurring commitment (phase-5 plan sections 3 and 7;
    milestone 5c).

    The row **is** the negotiation's state machine, the same shape
    `Checkin` and `Proposal` already use for theirs:
    `proposed` -> (`awaiting_counter` -> `countered`) -> `active` |
    `declined`, or `expired` off the two waiting states after
    `PROPOSAL_TTL_DAYS` (app/core/orders.py). `retired` is the terminal
    state for an order the user had accepted and later removed with
    `/orders`' [Снять].

    `counter_of` is what makes "one round only" checkable in the
    database, not just in code: a row with `counter_of` set is itself a
    counter and can never be countered again (`ck_standing_order_not_
    self_counter` only rules out the degenerate self-reference; the "a
    counter can't be countered" rule is enforced in app/core/orders.py,
    because it depends on the *original* row's own `counter_of` being
    null, which a single-row CHECK cannot see).

    `weekday` is required exactly when `cadence='weekly'` and forbidden
    otherwise -- `ck_standing_order_weekly_needs_weekday` -- because a
    `weekly` order with no weekday would have nothing for
    `app/core/orders.py`'s `due_today()` to compare against, and a
    `daily`/`weekdays`/`once` order with one would silently carry a
    number nothing ever reads.

    `tg_message_id` mirrors `Checkin.tg_message_id` and `Proposal.
    tg_message_id`: the proposal card's message, so its buttons can be
    edited away once the row is decided.
    """

    __tablename__ = "standing_order"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(String, nullable=False)
    cadence: Mapped[str] = mapped_column(String, nullable=False)
    weekday: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    counter_of: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("standing_order.id")
    )
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decided_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    # 5d (phase-5 plan section 3): links a review-proposed order back to
    # its review_proposal row, so app/tg/orders.py's so:a/so:r callbacks
    # can call review.mark_proposal() on it -- see that migration's own
    # docstring for why ON DELETE SET NULL. Written only by
    # app/core/orders.py's own targeted UPDATE (link_review_proposal),
    # never by app/core/review.py directly.
    review_proposal_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("review_proposal.id", ondelete="SET NULL")
    )

    __table_args__ = (
        CheckConstraint('char_length("text") <= 200', name="ck_standing_order_text_length"),
        CheckConstraint(
            "cadence in ('daily', 'weekdays', 'weekly', 'once')",
            name="ck_standing_order_cadence",
        ),
        CheckConstraint(
            "status in ('proposed', 'awaiting_counter', 'countered', 'active', "
            "'declined', 'retired', 'expired')",
            name="ck_standing_order_status",
        ),
        CheckConstraint(
            "source in ('anchor', 'user', 'review')", name="ck_standing_order_source"
        ),
        CheckConstraint(
            "(cadence = 'weekly') = (weekday is not null)",
            name="ck_standing_order_weekly_needs_weekday",
        ),
        CheckConstraint(
            "weekday is null or weekday between 1 and 7", name="ck_standing_order_weekday_range"
        ),
        CheckConstraint("counter_of is null or counter_of <> id", name="ck_standing_order_not_self_counter"),
        Index("ix_standing_order_status", "status"),
    )


class CheckinOrderResult(Base):
    """One order's answer within one day's check-in (phase-5 plan
    sections 3 and 7; milestone 5c).

    `(checkin_id, order_id)` is the primary key, not a surrogate id:
    exactly one answer per order per check-in, and `app/core/orders.py`'s
    `record_result` upserts against it -- a replayed `c:o:<id>:<d|n>`
    callback overwrites its own answer rather than adding a second row,
    the same idempotency shape `Checkin`'s own upsert gives the day's
    three fields.

    Both foreign keys cascade: purging a check-in or an order (this
    table is truncated ahead of both in app/core/purge.py, so the
    cascade never actually fires there) must not leave an orphaned
    result behind.
    """

    __tablename__ = "checkin_order_result"

    checkin_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("checkin.id", ondelete="CASCADE"), primary_key=True
    )
    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("standing_order.id", ondelete="CASCADE"), primary_key=True
    )
    result: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        CheckConstraint("result in ('done', 'no')", name="ck_checkin_order_result_result"),
    )


class WeeklyReview(Base):
    """One local week's safety-model analysis (phase-5 plan sections 3
    and 8; milestone 5d).

    `week_start` is the local Monday, unique -- the scheduled review
    skips any week that already has a row (enforced by the outbound
    gate's own `weekly_review` kind rule), and `/review` upserts against
    it to regenerate. `analysis` is the validated JSON
    (wins/misses/patterns/intentions/proposals), never the raw model
    output. `message_id` points at the persona message that carried the
    summary -- nullable because the row is written by
    app/core/review.py before that message exists yet (the send path
    generates the message after the analysis, then calls
    `review.set_message_id`).
    """

    __tablename__ = "weekly_review"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    week_start: Mapped[datetime.date] = mapped_column(Date, nullable=False, unique=True)
    analysis: Mapped[dict] = mapped_column(JSONB, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("message.id"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReviewProposal(Base):
    """One suggestion from a weekly review, sent as its own card (phase-5
    plan sections 3 and 8; milestone 5d).

    `kind='standing_order'` also gets its own `standing_order` row (via
    `app/core/orders.py`'s `propose(..., source='review')`, linked back
    by `standing_order.review_proposal_id`); `kind='persona_note'`
    becomes a `persona_amendment` on adoption. `status` mirrors
    `Proposal`'s own vocabulary (pending/adopted/rejected/expired) --
    the same shape, a different table, because a review proposal is not
    the extractor's pending-change-to-user_state kind of thing.
    """

    __tablename__ = "review_proposal"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    review_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("weekly_review.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    # `sa.text(...)`, not the bare `text(...)` every other model in this
    # file uses: this class has a `text` *column* (Memory's and
    # NotebookEntry's own docstrings warn about exactly this trap), so
    # by the time `status`'s server_default below runs, `text` already
    # names the mapped_column() above, not sqlalchemy's `text()`.
    text: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", server_default=sa.text("'pending'")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decided_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "kind in ('standing_order', 'persona_note')", name="ck_review_proposal_kind"
        ),
        CheckConstraint(
            "status in ('pending', 'adopted', 'rejected', 'expired')",
            name="ck_review_proposal_status",
        ),
        CheckConstraint('char_length("text") <= 200', name="ck_review_proposal_text_length"),
        CheckConstraint(
            'reason is null or char_length(reason) <= 160', name="ck_review_proposal_reason_length"
        ),
        Index("ix_review_proposal_status", "status"),
    )


class IdleRun(Base):
    """One idle-work attempt: planned, run, and its outcome (Phase 6 plan
    section 3; milestone 6a).

    `status` is the run's own lifecycle -- `queued` (planned by
    app/core/idle/planner.py) -> `running` (claimed by app/core/idle/
    runner.py) -> `done` | `failed` | `skipped`, and `done` rows with
    `reversible=true` may additionally move to `undone` via /digest's
    undo button (app/core/idle/undo.py). `skip_reason` doubles as the
    failure code on a `failed` row -- there is deliberately no separate
    error_code column, since the two never both apply to one row and the
    plan's data model gives this table only one.

    `summary` is counts, codes and cost only, never text -- app/core/
    idle/'s whole privacy property (plan section 8's "Logs and idle_run.
    summary contain only IDs, counts, codes, cost -- never text").
    """

    __tablename__ = "idle_run"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    local_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="queued", server_default=text("'queued'")
    )
    skip_reason: Mapped[str | None] = mapped_column(String)
    usd_cost: Mapped[decimal.Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=decimal.Decimal("0"), server_default=text("0")
    )
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'"))
    reversible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    undone_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "kind in ('backfill', 'consolidate', 'reflect', 'prebrief', 'critique', "
            "'research', 'canary')",
            name="ck_idle_run_kind",
        ),
        CheckConstraint(
            "status in ('queued', 'running', 'done', 'failed', 'skipped', 'undone')",
            name="ck_idle_run_status",
        ),
        # /digest's two live queries: "runs in the last N hours/days" and
        # per-day per-kind counts for the planner's max-per-day checks.
        Index("ix_idle_run_local_date_kind", "local_date", "kind"),
    )


class IdleChange(Base):
    """The undo log for one idle_run's writes (Phase 6 plan section 3;
    milestone 6a; `after` is 6a's own addition to the plan's SQL).

    Reversed in app/core/idle/undo.py by replaying rows in reverse `id`
    order: `insert` -> delete, `supersede` -> clear the pointer,
    `close` -> reactivate, `update` -> restore `before`. `after` is the
    row's state right after the change (including on `insert`, where
    there is no `before`) -- undo.py compares it against the row's
    *current* state before touching it, and skips (reports) that row on
    a mismatch, which is what makes undo safe against something else
    having changed the row since.

    Only `memory` and `notebook_entry` in 6a: nothing else is
    idle-reversible yet (the plan's consolidate/reflect kinds, 6b).
    """

    __tablename__ = "idle_change"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("idle_run.id", ondelete="CASCADE"), nullable=False
    )
    table_name: Mapped[str] = mapped_column(String, nullable=False)
    row_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    op: Mapped[str] = mapped_column(String, nullable=False)
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "table_name in ('memory', 'notebook_entry')", name="ck_idle_change_table_name"
        ),
        CheckConstraint(
            "op in ('insert', 'supersede', 'close', 'update')", name="ck_idle_change_op"
        ),
        Index("ix_idle_change_run_id", "run_id"),
    )


class BriefNote(Base):
    """Tomorrow's pre-drafted morning notes (Phase 6 plan section 3;
    milestone 6a's table, milestone 6c's writer and reader).

    Keyed on the morning it is *for*, not on when it was written -- the
    prebrief kind writes tonight for tomorrow's `local_date`, and the
    morning outbound (6c) reads today's row and stamps `used_at`. A row
    never used by its own date is simply stale and ignored, not deleted
    -- the daily retention sweep (6e) is what actually clears it.
    """

    __tablename__ = "brief_note"

    local_date: Mapped[datetime.date] = mapped_column(Date, primary_key=True)
    notes: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    used_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))


class InterestTopic(Base):
    """A user-picked research topic for idle `research` (Phase 6 plan
    sections 3 and 6.5; milestone 6a's table, milestone 6d's writer and
    reader -- `/interests add <packet> <тема>`).

    Topics come only from the user, never chosen by the model (plan
    section 1's "Out of scope": "Autonomous topic choice for research").
    `active=false` is how `/interests`' [✖] retires one without losing
    its history.

    **The `text` column shadows sqlalchemy's `text()`** for the rest of
    this class body, exactly the trap `Memory`'s own docstring warns
    about -- `sa.text(...)` is used below rather than the bare
    `text(...)` every other model in this file uses.
    """

    __tablename__ = "interest_topic"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(String, nullable=False)
    packet: Mapped[str] = mapped_column(String, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa.text("true")
    )
    last_run_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint('char_length("text") <= 100', name="ck_interest_topic_text_length"),
        CheckConstraint(
            "packet in ('forums', 'guides', 'ref')", name="ck_interest_topic_packet"
        ),
    )


class BackupLog(Base):
    """One encrypted-backup attempt (Phase 6 plan section 9.1; milestone
    6a's table only -- 6e writes and reads it).

    No content ever: `object_key`, `bytes` and `sha256` describe the
    ciphertext object on the bucket, never what is inside it.
    """

    __tablename__ = "backup_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    object_key: Mapped[str | None] = mapped_column(String)
    bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(
            "status in ('ok', 'failed', 'pruned', 'purged')", name="ck_backup_log_status"
        ),
    )


class HeartbeatState(Base):
    """A single-row marker of when the heartbeat last ran (Phase 6 plan
    section 2; milestone 6a; 6e's `/readyz` reads it).

    Its own tiny table rather than a `user_state` column, by decision
    (approved plan §7): `user_state`'s columns are audited or narrowly-
    written traffic counters, and a liveness timestamp bumped every 60s
    by the heartbeat loop is neither. Singleton shape copied from
    `UserState.id` -- pinned to 1 by a default and a check constraint, so
    there is always exactly one row and it is never ambiguous which one
    to read or write.
    """

    __tablename__ = "heartbeat_state"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=False, server_default=text("1")
    )
    heartbeat_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (CheckConstraint("id = 1", name="ck_heartbeat_state_id_singleton"),)


class PersonaAmendment(Base):
    """A `persona_note` proposal the user adopted (phase-5 plan sections
    3 and 9; milestone 5d).

    `status` is `trial` (the `amendment_trial` job is running or
    queued) -> `active` | `failed`, or `active` -> `revoked` via
    `/amendments`' own button. `persona_sha` is `persona.md`'s hash at
    adoption -- **persona.md is never written by this codebase**; an
    amendment only ever changes what the live prompt carries under
    `## Поправки (одобрены тобой)` (app/core/prompt.py), never the file
    itself. If a later manual edit changes the hash, the amendment stays
    active but `/amendments` flags it "(персона изменилась — проверь)".

    `eval_report` holds pass/fail per blocking case only -- no model
    text, per the implementation plan's non-negotiable on that point.
    """

    __tablename__ = "persona_amendment"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    proposal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("review_proposal.id"))
    eval_report: Mapped[dict | None] = mapped_column(JSONB)
    persona_sha: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    activated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status in ('trial', 'active', 'failed', 'revoked')",
            name="ck_persona_amendment_status",
        ),
        CheckConstraint('char_length("text") <= 200', name="ck_persona_amendment_text_length"),
        Index("ix_persona_amendment_status", "status"),
    )

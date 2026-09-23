"""The inbound queue and the generic claim/complete/fail/recover mechanics.

This is the whole exactly-once story (plan section 2 / 6.3). Concurrency
of the worker loop is 1; _claim() uses FOR UPDATE SKIP LOCKED so multiple
callers never grab the same row, though only one worker ever runs.

2a generalizes the Phase 1 code rather than copying it (phase-2 plan
section 3). Every mechanic now lives in a private `_`-prefixed function
parameterized by a `QueueSpec`; the five public functions below are the
`telegram_update` spec bound to those mechanics, with their Phase 1
signatures **unchanged**. That is deliberate and load-bearing: tests/
test_queue.py and tests/test_worker.py were written against Phase 1 and
must keep passing untouched, which is the only proof that the
generalization changed no behaviour. app/db/jobs.py is the second
binding, over the `job` table.

The only mechanical difference between the two queues is `due_column`:
jobs are not claimable until `run_after <= now()`, updates have no such
gate. Everything else -- the status vocabulary, the attempts counter,
the lock timestamp, the retry ceiling, the stuck sweep -- is shared.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import Sequence, func, select, update as sql_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import InstrumentedAttribute

from app.db.models import TelegramUpdate, WebUpdate

MAX_ATTEMPTS = 3
STUCK_AFTER = datetime.timedelta(minutes=5)

# Web-chat plan track 1: web update_ids are always negative, so they can
# never collide with a Telegram update_id (always non-negative) or with
# each other's key space. Subtracting this offset from a strictly
# increasing Postgres sequence keeps them both negative and increasing
# over time -- FIFO among web rows -- with headroom no realistic
# nextval() will ever close: 2**53 is JavaScript's/Postgres bigint's safe
# integer ceiling, a number web_update_seq would need trillions of years
# of continuous traffic to approach.
WEB_ID_OFFSET = 2**53


@dataclass(frozen=True)
class QueueSpec:
    """Everything the generic mechanics need to work one queue table.

    `id_column` is what complete/fail/recover key on -- the natural key
    of the row, which is Telegram's own update_id for the inbound queue
    and a surrogate bigserial for jobs.

    `order_by` is the claim order. For updates it is (created_at,
    update_id): `created_at` is arrival order across *both* transports,
    and `update_id` is the tiebreaker for two rows inserted in the same
    instant. Web-chat plan track 1 changed this from `update_id` alone
    -- Telegram's ids are a monotonic arrival sequence only within their
    own transport, and web ids (always negative, see WEB_ID_OFFSET
    above) would otherwise always sort first regardless of when either
    message actually arrived, reordering the single conversation the
    whole synthetic-update trick depends on the moment both queues have
    a backlog at once. For jobs it is (run_after, id): due-soonest
    first, then insertion order among rows due at the same instant.

    `due_column`, when set, adds `<= now()` to the claim predicate.
    `now()` here is Postgres' transaction_timestamp, not Python's
    clock, so a row scheduled inside the claiming transaction cannot be
    claimed by it.
    """

    model: type
    id_column: InstrumentedAttribute
    order_by: tuple[InstrumentedAttribute, ...]
    due_column: InstrumentedAttribute | None = None


UPDATE_SPEC = QueueSpec(
    model=TelegramUpdate,
    id_column=TelegramUpdate.update_id,
    order_by=(TelegramUpdate.created_at, TelegramUpdate.update_id),
)


async def _claim(session: AsyncSession, spec: QueueSpec) -> Any | None:
    """Claim the oldest claimable row for `spec`, or None if there is none.

    Commits before returning, releasing the row lock before the caller
    does anything slow (e.g. feed_update, a Telegram API call, an LLM
    call). Never hold a DB lock across a network call.
    """
    stmt = select(spec.model).where(spec.model.status == "pending")
    if spec.due_column is not None:
        stmt = stmt.where(spec.due_column <= func.now())
    stmt = stmt.order_by(*spec.order_by).limit(1).with_for_update(skip_locked=True)

    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        await session.commit()  # release the (empty) transaction cleanly
        return None

    row.status = "processing"
    row.locked_at = datetime.datetime.now(datetime.timezone.utc)
    row.attempts += 1
    await session.commit()
    return row


async def _complete(session: AsyncSession, spec: QueueSpec, row_id: int) -> None:
    await session.execute(
        sql_update(spec.model).where(spec.id_column == row_id).values(status="done")
    )
    await session.commit()


async def _fail(session: AsyncSession, spec: QueueSpec, row_id: int, error: str) -> None:
    """Return a failed row to pending, or to failed after MAX_ATTEMPTS.

    `error` must be an exception type/message only — never payload content.
    """
    result = await session.execute(
        select(spec.model.attempts).where(spec.id_column == row_id)
    )
    attempts = result.scalar_one()
    next_status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
    await session.execute(
        sql_update(spec.model)
        .where(spec.id_column == row_id)
        .values(status=next_status, error=error)
    )
    await session.commit()


async def _recover_stuck(
    session: AsyncSession, spec: QueueSpec, older_than: datetime.timedelta
) -> int:
    """Reset processing rows whose lock is older than `older_than` to pending.

    Returns the number of rows recovered.
    """
    cutoff = datetime.datetime.now(datetime.timezone.utc) - older_than
    result = await session.execute(
        sql_update(spec.model)
        .where(spec.model.status == "processing")
        .where(spec.model.locked_at < cutoff)
        .values(status="pending")
        .returning(spec.id_column)
    )
    recovered = result.fetchall()
    await session.commit()
    return len(recovered)


# --- the telegram_update binding (Phase 1 signatures, unchanged) ---


async def enqueue(session: AsyncSession, update_id: int, payload: dict) -> bool:
    """Insert a new update; returns True iff a row was actually inserted.

    ON CONFLICT DO NOTHING on the primary key makes a duplicate update_id
    a no-op instead of an error, giving exactly-once storage.

    Not generalized: the inbound queue's dedup key is its primary key
    and is supplied by Telegram, while a job's is a nullable unique
    column we mint ourselves. Sharing one insert helper would mean a
    parameter that is always the primary key here and never there.
    """
    stmt = (
        pg_insert(TelegramUpdate)
        .values(update_id=update_id, payload=payload, status="pending", attempts=0)
        .on_conflict_do_nothing(index_elements=["update_id"])
        .returning(TelegramUpdate.update_id)
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.first() is not None


async def reserve_web_id(session: AsyncSession) -> int:
    """Reserve the next negative id from web_update_seq. Public (W4) so a
    caller that needs a web-origin id *without* a queued row -- app/web/
    panels/checkin.py mints the id it stores as a web check-in's
    `tg_message_id`, which the synthetic `c:n:skip` callback then echoes
    back as its `message_id` -- draws from the same always-negative,
    never-colliding id space `enqueue_web` itself uses, rather than
    inventing a second one. Does not commit: `nextval()` is
    non-transactional in Postgres, so the reservation holds regardless.
    """
    result = await session.execute(select(Sequence("web_update_seq").next_value()))
    web_id = result.scalar_one() - WEB_ID_OFFSET
    assert web_id < 0, f"web id must be negative, got {web_id}"
    return web_id


async def web_callback_pending(session: AsyncSession, message_id: int, data: str) -> bool:
    """Whether a web-origin callback row with this (message_id, data) is
    still queued or in flight (W4: the web check-in's "a submission is
    waiting for the worker" -- its completion row carries the check-in's
    own minted message id). Read-only."""
    callback = TelegramUpdate.payload["callback_query"]
    result = await session.execute(
        select(func.count())
        .select_from(TelegramUpdate)
        .where(TelegramUpdate.update_id < 0)
        .where(TelegramUpdate.status.in_(("pending", "processing")))
        .where(callback["data"].astext == data)
        .where(callback["message"]["message_id"].astext == str(message_id))
    )
    return result.scalar_one() > 0


async def _next_web_update_id(session: AsyncSession) -> int:
    """Reserve the next negative web update_id from web_update_seq."""
    return await reserve_web_id(session)


async def enqueue_web(
    session: AsyncSession, build_payload: Callable[[int], dict], client_key: str | None
) -> int:
    """Insert a synthetic web update; idempotent on `client_key`. Returns its update_id.

    `client_key=None` (app/web/ingress.py's `press`, whose HTTP contract
    carries no client_key) always inserts a fresh row: NULL is never
    equal to another NULL under the partial unique index the migration
    creates (`WHERE client_key IS NOT NULL`), so a NULL-keyed insert can
    never conflict via that arbiter and the fallback SELECT below is
    unreachable for it.

    Web-chat plan track 1 (design section 8, "enqueue_web's retry
    contract is underspecified" in the second adversarial critique).
    Not generalized alongside `enqueue` above for the same reason that
    one is not generalized either: the two dedup keys are shaped
    differently (Telegram's is the primary key itself and arrives from
    the caller; this one is a nullable unique column we mint a fresh
    negative id for), so a shared insert helper would need a parameter
    that is always meaningful here and never there.

    **Two tables, one transaction, no foreign key between them** (see
    `app/db/models.py`'s `TelegramUpdate`/`WebUpdate` docstrings for
    why: an FK into `telegram_update` would take a lock this rework
    exists to avoid). The idempotency arbiter is now `WebUpdate.
    client_key`, checked *first* so a conflicting retry never leaves a
    wasted `telegram_update` row behind: on a `client_key` replay the
    `web_update` insert's ON CONFLICT DO NOTHING fires, RETURNING yields
    nothing, and the matching `telegram_update` insert is skipped
    entirely -- a fallback SELECT reads back the update_id the *first*
    call used. That id is what POST /api/send hands back on every
    retry: same client_key in, same update_id out, every time.

    A fresh update_id is reserved from web_update_seq first, then handed
    to `build_payload` so the synthetic Update's own message_id/update_id
    fields (app/web/ingress.py) can embed it -- the payload cannot be
    built before the id exists. It is always negative (Telegram's own
    ids are always non-negative); asserted here in code rather than by a
    database CHECK, since the sign-of-update_id invariant is no longer
    enforced by the schema at all (see `TelegramUpdate`'s docstring).
    A reserved id that loses the client_key race is simply never used --
    a gap in a sequence costs nothing, and Postgres sequences are not
    gapless by design.
    """
    update_id = await _next_web_update_id(session)
    assert update_id < 0, f"web update_id must be negative, got {update_id}"

    web_stmt = (
        pg_insert(WebUpdate)
        .values(update_id=update_id, client_key=client_key)
        .on_conflict_do_nothing(
            index_elements=[WebUpdate.client_key],
            index_where=WebUpdate.client_key.isnot(None),
        )
        .returning(WebUpdate.update_id)
    )
    web_result = await session.execute(web_stmt)
    web_row = web_result.first()
    if web_row is None:
        # client_key already claimed by an earlier call -- the reserved
        # id above goes unused, and no telegram_update row is inserted
        # for it.
        existing = await session.execute(
            select(WebUpdate.update_id).where(WebUpdate.client_key == client_key)
        )
        await session.commit()
        return existing.scalar_one()

    payload = build_payload(update_id)
    await session.execute(
        pg_insert(TelegramUpdate).values(
            update_id=update_id, payload=payload, status="pending", attempts=0
        )
    )
    await session.commit()
    return update_id


async def claim(session: AsyncSession) -> TelegramUpdate | None:
    """Claim the oldest pending update, or None if the queue is empty."""
    return await _claim(session, UPDATE_SPEC)


async def complete(session: AsyncSession, update_id: int) -> None:
    await _complete(session, UPDATE_SPEC, update_id)


async def fail(session: AsyncSession, update_id: int, error: str) -> None:
    await _fail(session, UPDATE_SPEC, update_id, error)


async def recover_stuck(session: AsyncSession, older_than: datetime.timedelta = STUCK_AFTER) -> int:
    return await _recover_stuck(session, UPDATE_SPEC, older_than)

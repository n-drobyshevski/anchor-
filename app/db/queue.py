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
from typing import Any

from sqlalchemy import func, select, update as sql_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import InstrumentedAttribute

from app.db.models import TelegramUpdate

MAX_ATTEMPTS = 3
STUCK_AFTER = datetime.timedelta(minutes=5)


@dataclass(frozen=True)
class QueueSpec:
    """Everything the generic mechanics need to work one queue table.

    `id_column` is what complete/fail/recover key on -- the natural key
    of the row, which is Telegram's own update_id for the inbound queue
    and a surrogate bigserial for jobs.

    `order_by` is the claim order. For updates it is update_id alone,
    which is Telegram's own monotonic sequence and therefore arrival
    order. For jobs it is (run_after, id): due-soonest first, then
    insertion order among rows due at the same instant.

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
    order_by=(TelegramUpdate.update_id,),
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


async def claim(session: AsyncSession) -> TelegramUpdate | None:
    """Claim the oldest pending update, or None if the queue is empty."""
    return await _claim(session, UPDATE_SPEC)


async def complete(session: AsyncSession, update_id: int) -> None:
    await _complete(session, UPDATE_SPEC, update_id)


async def fail(session: AsyncSession, update_id: int, error: str) -> None:
    await _fail(session, UPDATE_SPEC, update_id, error)


async def recover_stuck(session: AsyncSession, older_than: datetime.timedelta = STUCK_AFTER) -> int:
    return await _recover_stuck(session, UPDATE_SPEC, older_than)

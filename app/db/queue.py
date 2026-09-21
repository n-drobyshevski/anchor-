"""The inbound queue: enqueue / claim / complete / fail / recover_stuck.

This is the whole exactly-once story (plan section 2 / 6.3). Concurrency
of the worker loop is 1; claim() uses FOR UPDATE SKIP LOCKED so multiple
callers never grab the same row, though 1a only ever runs one worker.
"""

from __future__ import annotations

import datetime

from sqlalchemy import select, update as sql_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TelegramUpdate

MAX_ATTEMPTS = 3
STUCK_AFTER = datetime.timedelta(minutes=5)


async def enqueue(session: AsyncSession, update_id: int, payload: dict) -> bool:
    """Insert a new update; returns True iff a row was actually inserted.

    ON CONFLICT DO NOTHING on the primary key makes a duplicate update_id
    a no-op instead of an error, giving exactly-once storage.
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
    """Claim the oldest pending row, or None if the queue is empty.

    Commits before returning, releasing the row lock before the caller
    does anything slow (e.g. feed_update / a Telegram API call). Never
    hold a DB lock across a network call.
    """
    stmt = (
        select(TelegramUpdate)
        .where(TelegramUpdate.status == "pending")
        .order_by(TelegramUpdate.update_id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
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


async def complete(session: AsyncSession, update_id: int) -> None:
    await session.execute(
        sql_update(TelegramUpdate)
        .where(TelegramUpdate.update_id == update_id)
        .values(status="done")
    )
    await session.commit()


async def fail(session: AsyncSession, update_id: int, error: str) -> None:
    """Return a failed row to pending, or to failed after MAX_ATTEMPTS.

    `error` must be an exception type/message only — never payload content.
    """
    result = await session.execute(
        select(TelegramUpdate.attempts).where(TelegramUpdate.update_id == update_id)
    )
    attempts = result.scalar_one()
    next_status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
    await session.execute(
        sql_update(TelegramUpdate)
        .where(TelegramUpdate.update_id == update_id)
        .values(status=next_status, error=error)
    )
    await session.commit()


async def recover_stuck(session: AsyncSession, older_than: datetime.timedelta = STUCK_AFTER) -> int:
    """Reset processing rows whose lock is older than `older_than` to pending.

    Returns the number of rows recovered.
    """
    cutoff = datetime.datetime.now(datetime.timezone.utc) - older_than
    result = await session.execute(
        sql_update(TelegramUpdate)
        .where(TelegramUpdate.status == "processing")
        .where(TelegramUpdate.locked_at < cutoff)
        .values(status="pending")
        .returning(TelegramUpdate.update_id)
    )
    recovered = result.fetchall()
    await session.commit()
    return len(recovered)

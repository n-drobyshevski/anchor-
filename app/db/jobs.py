"""The background job queue: the second binding of app/db/queue.py's mechanics.

Everything about claiming, completing, failing and recovering a job is
the same code that drives the inbound `telegram_update` queue (phase-2
plan section 3) -- this module only supplies the `QueueSpec` and the
enqueue/defer operations that are genuinely specific to jobs.

Two things differ from the inbound queue, and only two:

1. `run_after` gates claiming (`JOB_SPEC.due_column`). A job is
   invisible to claim_job() until it is due.
2. `dedup_key` is a nullable unique column we mint ourselves, rather
   than a primary key Telegram supplies, so enqueue_job() has its own
   ON CONFLICT DO NOTHING insert.

Ordering is (run_after, id): due-soonest first, insertion order among
rows due at the same instant. With worker concurrency at 1 that makes
job execution fully deterministic.
"""

from __future__ import annotations

import datetime

from sqlalchemy import update as sql_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Job
from app.db.queue import (
    MAX_ATTEMPTS,
    STUCK_AFTER,
    QueueSpec,
    _claim,
    _complete,
    _fail,
    _recover_stuck,
)

__all__ = [
    "MAX_ATTEMPTS",
    "STUCK_AFTER",
    "JOB_SPEC",
    "enqueue_job",
    "claim_job",
    "complete_job",
    "fail_job",
    "defer_job",
    "recover_stuck_jobs",
]

JOB_SPEC = QueueSpec(
    model=Job,
    id_column=Job.id,
    order_by=(Job.run_after, Job.id),
    due_column=Job.run_after,
)


async def enqueue_job(
    session: AsyncSession,
    kind: str,
    payload: dict,
    *,
    dedup_key: str | None = None,
    run_after: datetime.datetime | None = None,
    commit: bool = True,
) -> bool:
    """Insert a job; returns True iff a row was actually inserted.

    With a `dedup_key`, ON CONFLICT DO NOTHING makes re-enqueueing a
    no-op, so callers never have to check first -- 'scene:<id>' can be
    offered as many times as the code happens to reach it and will
    queue exactly one summary.

    With `dedup_key=None` the insert always succeeds: NULLs do not
    conflict in a unique index, which is the correct reading of "this
    job is not deduplicated".

    `commit=False` (5b) leaves the insert in the caller's transaction:
    /delete queues `vault_purge` inside its single wipe transaction, and
    a commit here would split it in two.
    """
    values: dict = {"kind": kind, "payload": payload, "dedup_key": dedup_key}
    if run_after is not None:
        values["run_after"] = run_after

    stmt = (
        pg_insert(Job)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["dedup_key"])
        .returning(Job.id)
    )
    result = await session.execute(stmt)
    inserted = result.first() is not None
    if commit:
        await session.commit()
    return inserted


async def claim_job(session: AsyncSession) -> Job | None:
    """Claim the oldest *due* pending job, or None if none is due."""
    return await _claim(session, JOB_SPEC)


async def complete_job(session: AsyncSession, job_id: int) -> None:
    await _complete(session, JOB_SPEC, job_id)


async def fail_job(session: AsyncSession, job_id: int, error: str) -> None:
    await _fail(session, JOB_SPEC, job_id, error)


async def defer_job(session: AsyncSession, job_id: int, run_after: datetime.datetime) -> None:
    """Return a claimed job to pending, due at `run_after`.

    Not a failure: this is the spend-cap path (phase-2 plan section 12,
    "summaries are re-queued with run_after = next local midnight"), so
    it must not consume the retry budget. `attempts` is reset to 0 for
    exactly that reason -- a job deferred once a day for a week would
    otherwise exhaust MAX_ATTEMPTS and be marked failed without ever
    having errored.
    """
    await session.execute(
        sql_update(Job)
        .where(Job.id == job_id)
        .values(status="pending", run_after=run_after, attempts=0, locked_at=None)
    )
    await session.commit()


async def recover_stuck_jobs(
    session: AsyncSession, older_than: datetime.timedelta = STUCK_AFTER
) -> int:
    return await _recover_stuck(session, JOB_SPEC, older_than)

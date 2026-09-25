"""Daily retention sweeps (Phase 6 plan section 9.4; milestone 6e).

Three independent rules, run as one job (`RETENTION_SWEEP`) the same
way app/research/sweeps.py's `run_daily_sweep` runs its two -- same
cadence (once a local day), same injected Clock, same plain-SQL-no-
provider-call shape, same "a bug here is a bug in a WHERE clause"
failure mode:

1. `telegram_update.payload := '{}'` (an empty object) after
   `UPDATE_PAYLOAD_RETENTION_DAYS`, for rows that are done or failed.
2. Terminal `job` rows (`done`/`failed`) deleted after `JOB_RETENTION_DAYS`.
3. If `MESSAGE_RETENTION_DAYS > 0`: messages older than that, **only**
   if their scene has a summary, are deleted.

**Existing sweeps are unchanged.** The Phase 4 clip-text/card-expiry
sweep (`RESEARCH_SWEEP`) and the Phase 5 notebook/orders/review
expiries keep their own job kinds and their own daily enqueue calls in
app/worker.py's `_heartbeat_loop` -- this module adds a fourth sibling,
it does not fold into any of them.

**Message deletion and foreign keys.** Two tables carry a nullable FK
into `message.id` with no `ON DELETE` action: `outbound.message_id`
(the proactive message a send delivered) and `weekly_review.message_id`
(the persona message that carried a review). Deleting a referenced
`message` row would raise a foreign-key violation and fail the whole
sweep. Rather than nulling those FKs out from under two tables this
module does not otherwise own, the sweep simply **excludes** any
message still referenced by either one from the delete -- a proactive
message or the message that carried a weekly review is exactly the
kind of message worth keeping past an ordinary retention window
anyway, and excluding a handful of rows from a bulk delete is safe by
construction rather than a promise this module has to keep some other
way.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import delete as sql_delete, literal_column, select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.clock import Clock
from app.db.models import Job, Message, Outbound, Scene, TelegramUpdate, WeeklyReview

logger = logging.getLogger(__name__)

RETENTION_SWEEP = "retention_sweep"

FORGET_UPDATE_PAYLOAD = "forget_update_payload"
FORGET_TERMINAL_JOBS = "forget_terminal_jobs"
FORGET_OLD_MESSAGES = "forget_old_messages"

# Mirrors app/db/queue.py's own terminal statuses for the job table
# (app/db/jobs.py reuses the same _complete/_fail as telegram_update):
# a job row only ever reaches 'done' or 'failed', never anything else
# once claiming has finished with it.
_JOB_TERMINAL_STATUSES = ("done", "failed")
_UPDATE_TERMINAL_STATUSES = ("done", "failed")


async def forget_update_payloads(session: AsyncSession, settings: Settings, clock: Clock) -> int:
    """Blank `telegram_update.payload` for finished rows older than the
    retention window. Row identity, status and attempts all survive --
    only the Telegram envelope (which can carry message text) is cleared.

    Blanked to an empty object rather than NULL: the privacy outcome is
    the same (the Telegram content is gone), and a JSON literal avoids
    SQLAlchemy's JSON-null-vs-SQL-NULL ambiguity entirely.

    Pending and processing rows are never touched -- the worker still
    needs their payload to handle them.
    """
    cutoff = clock.now_utc() - datetime.timedelta(days=settings.UPDATE_PAYLOAD_RETENTION_DAYS)
    # A literal, not a bound parameter: SQLAlchemy would bind "{}" as a
    # JSON *string*, not an empty object.
    empty = literal_column("'{}'::jsonb")
    result = await session.execute(
        sql_update(TelegramUpdate)
        .where(TelegramUpdate.created_at < cutoff)
        .where(TelegramUpdate.status.in_(_UPDATE_TERMINAL_STATUSES))
        .where(TelegramUpdate.payload != empty)
        .values(payload=empty)
    )
    await session.commit()
    count = result.rowcount or 0
    logger.info("update payloads forgotten", extra={"event": FORGET_UPDATE_PAYLOAD, "count": count})
    return count


async def forget_terminal_jobs(session: AsyncSession, settings: Settings, clock: Clock) -> int:
    """Delete `done`/`failed` job rows older than the retention window.

    Nothing else has a foreign key into `job.id`, so this is a plain
    DELETE with no reference to worry about.
    """
    cutoff = clock.now_utc() - datetime.timedelta(days=settings.JOB_RETENTION_DAYS)
    result = await session.execute(
        sql_delete(Job)
        .where(Job.status.in_(_JOB_TERMINAL_STATUSES))
        .where(Job.created_at < cutoff)
    )
    await session.commit()
    count = result.rowcount or 0
    logger.info("terminal jobs forgotten", extra={"event": FORGET_TERMINAL_JOBS, "count": count})
    return count


async def forget_old_messages(session: AsyncSession, settings: Settings, clock: Clock) -> int:
    """Delete messages older than `MESSAGE_RETENTION_DAYS`, only when
    their scene has a summary. A no-op when the setting is 0 ("keep
    forever" -- see app/config.py's own docstring on the field).

    Excludes any message still referenced by `outbound.message_id` or
    `weekly_review.message_id` -- see the module docstring for why.
    """
    if settings.MESSAGE_RETENTION_DAYS <= 0:
        return 0

    cutoff = clock.now_utc() - datetime.timedelta(days=settings.MESSAGE_RETENTION_DAYS)
    summarized_scenes = select(Scene.id).where(Scene.summary.is_not(None))
    referenced_by_outbound = select(Outbound.message_id).where(Outbound.message_id.is_not(None))
    referenced_by_review = select(WeeklyReview.message_id).where(
        WeeklyReview.message_id.is_not(None)
    )

    result = await session.execute(
        sql_delete(Message)
        .where(Message.created_at < cutoff)
        .where(Message.scene_id.in_(summarized_scenes))
        .where(Message.id.not_in(referenced_by_outbound))
        .where(Message.id.not_in(referenced_by_review))
    )
    await session.commit()
    count = result.rowcount or 0
    logger.info("old messages forgotten", extra={"event": FORGET_OLD_MESSAGES, "count": count})
    return count


async def run_retention_sweep(session: AsyncSession, settings: Settings, clock: Clock) -> None:
    """The one job (`RETENTION_SWEEP`) app/worker.py dispatches all three
    rules through -- same "one job, several unrelated cheap SQL
    statements" shape as app/research/sweeps.py's `run_daily_sweep`."""
    await forget_update_payloads(session, settings, clock)
    await forget_terminal_jobs(session, settings, clock)
    await forget_old_messages(session, settings, clock)


def retention_sweep_dedup_key(local_date: datetime.date) -> str:
    """One sweep per local date, ever -- mirrors research_sweep_dedup_key."""
    return f"retention_sweep:{local_date.isoformat()}"

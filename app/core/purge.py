"""Deleting everything (plan section 11, build rule 7: "/delete must really delete").

One transaction: cancel research work in flight, one TRUNCATE, then a
reset of the singleton state row. 4a adds the TRUNCATE targets research
data landed in; 4d adds the cancel step -- see `cancel_research_jobs`.

**Why TRUNCATE without CASCADE.** Nothing outside PURGED_TABLES points
into it: every foreign key in the schema either stays inside that list
(message -> telegram_update, message -> scene, memory -> memory,
study_card -> study_clip -> study_job, study_card -> memory) or belongs
to a table that is itself listed. So one TRUNCATE over the whole list succeeds. Leaving CASCADE
off is deliberate: if a future table ever references a purged one and is
not itself listed here, the statement fails loudly instead of silently
skipping it -- which is the direction rule 7 wants. A delete that
quietly misses a table is the worst outcome available.

RESTART IDENTITY is not only tidiness. After "delete everything",
/memories showing `#47` would be a lie about what is in there.

**The two invariants below are the real safety property**, and they are
asserted in tests/test_delete.py rather than here: every table is either
purged or explicitly kept, and every user_state column is either
preserved or explicitly reset. Both fail the moment somebody adds a
table or a column and forgets about this file, which is the only moment
anyone would notice.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete as sql_delete, text as sql_text, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.config import Settings
from app.core.state import STATE_ID, record_change
from app.db.models import Job, StudyJob, UserState

logger = logging.getLogger(__name__)

# job.kind for research background jobs -- app/research/jobs.py's own
# RESEARCH constant, spelled out here rather than imported. purge.py is
# on the hot path of every /delete regardless of whether the research
# feature has ever been used, and app/research/jobs.py's import chain
# pulls in the fetcher and its httpx/trafilatura dependencies for one
# string; a literal is cheaper and the two are pinned together by
# test_delete.py asserting this matches app.research.jobs.RESEARCH.
_RESEARCH_JOB_KIND = "research"

# study_job.status values that are not yet a final outcome
# (ck_study_job_status: queued|searching|fetching|distilling|done|
# failed|cancelled -- the first four are what cancel_research_jobs
# below flips to 'cancelled').
_STUDY_JOB_NON_TERMINAL = ("queued", "searching", "fetching", "distilling")

# Plan section 11's delete list, plus pending_memory. Section 11 omits
# that one, and it belongs: it holds text the user typed at /remember
# and never classified. Leaving it behind after "delete all my data" is
# exactly the bug rule 7 names.
PURGED_TABLES = (
    "message",
    "memory",
    "scene",
    "checkin",
    "proposal",
    "journal",
    "state_change",
    "spend_ledger",
    "job",
    "telegram_update",
    "pending_memory",
    # 3a: every proactive message ever planned, sent, skipped or
    # cancelled. Purged, not kept: it is a record of what the bot said
    # to this user and when, which is exactly what "delete all my data"
    # means. It also FKs to `message`, so leaving it out made Postgres
    # refuse the whole TRUNCATE -- /delete failed outright rather than
    # partially succeeding.
    "outbound",
    # H2: one row per safety-model call outcome. No content, but it is a
    # record of when this user was talked to and how the checks behaved
    # while they were -- which "delete all my data" covers.
    "safety_event",
    # 4a: the research loop's three tables (phase-4 plan section 4).
    # study_clip holds text fetched from the web and study_card holds
    # quotes from it, so this is the most content-bearing addition since
    # `message`. Listed child-first, which TRUNCATE does not require but
    # which keeps the order readable against the foreign keys.
    #
    # Their FKs do carry ondelete=CASCADE, unlike the three the docstring
    # above describes -- that cascade is for deleting one job, not for
    # this statement, which names all three tables anyway.
    "study_card",
    "study_clip",
    "study_job",
)

# user_state is reset in place, never dropped. persona_version is a
# hash of a file in this repo, not user data, and startup rebuilds it
# anyway (plan section 11: "Keep persona_version").
KEPT_TABLES = ("user_state", "persona_version")

# Columns that survive a wipe. Everything else on user_state must appear
# in reset_values() below.
PRESERVED_STATE_COLUMNS = ("id", "chat_id")


def reset_values(settings: Settings, clock: Clock) -> dict:
    """Every user_state column except the preserved two, at its default.

    Reset explicitly rather than left to the next boot: startup's
    upsert_user_state only refreshes chat_id and timezone on conflict,
    by explicit design, so nothing else would ever come back to its
    default on its own.

    `updated_at` is in here because the column has a server_default but
    no onupdate -- without it an in-place reset would leave the row
    claiming it was last touched when the bot first booted.
    """
    return {
        "persona_active": True,
        "intensity": 3,
        "timezone": settings.TZ_DEFAULT,
        "focus_on": False,
        "focus_since": None,
        "due_action": None,
        "due_set_at": None,
        "streak": 0,
        "last_checkin_at": None,
        "awaiting": None,
        "awaiting_ref": None,
        # 3a: the outbound counters (phase-3 plan section 4). A
        # /delete that left ignored_in_row at 3 would leave the bot
        # silent after a wipe that is meant to return it to factory
        # state, and a stale quiet_until would keep it muted.
        "quiet_until": None,
        "last_user_msg_at": None,
        "last_outbound_at": None,
        "ignored_in_row": 0,
        "welfare_at": None,
        "updated_at": clock.now_utc(),
    }


async def cancel_research_jobs(session: AsyncSession) -> None:
    """Cancel research work in flight (plan section 9: "/delete cancels
    queued and running jobs, then purges").

    Two writes, both scoped to research work only:

    - every non-terminal `study_job` becomes `'cancelled'`;
    - every still-`'pending'` (unclaimed) `job` row of kind `'research'`
      is deleted outright, so it can never be claimed at all.

    **Why this matters even though the TRUNCATE right after it would
    remove these same rows anyway.** By the time `delete_everything`
    returns, every row this function touched is gone regardless -- both
    `study_job` and `job` are in `PURGED_TABLES`. What this step
    actually defends against is not what `/export` or `/notes` would
    show afterwards, but a **second, concurrent** worker process (the
    scheduler module's own docstring notes two can be briefly live
    during a Railway rollout) that has already claimed a research job
    and started running it. app/db/queue.py's `claim()` commits and
    releases its row lock before the caller does anything slow, so a
    job in flight holds **no lock** the TRUNCATE below would ever wait
    on -- it can be mid-fetch or mid-distill, writing `study_clip` and
    `study_card` rows, at the exact instant this transaction wipes the
    tables it is writing to.

    app/research/jobs.py's `run_research_job` reads `study_job.status`
    exactly once, at the very top, and treats anything other than
    `'queued'` as already finished (the idempotent-rerun branch, added
    so a redelivered "already done" job is a no-op). So flipping every
    non-terminal `study_job` to `'cancelled'` here means: a job that has
    been claimed by the queue but has not yet reached that first read
    becomes a no-op the moment it does reach it, and a job that has not
    even been claimed yet -- still sitting in `job` with `status =
    'pending'` -- is deleted before anything can claim it and never
    starts. Research jobs are short (one or two provider calls plus a
    handful of fetches), so the window in which one is claimed but has
    not yet done that first read is narrow; the far more common case by
    volume is a job still waiting in the queue, which this closes
    completely.

    **What this does NOT close, honestly.** A job already past that
    first read -- already fetching or distilling when `/delete` runs --
    keeps running regardless of this UPDATE: `_run_study`'s loop never
    rechecks `study_job.status` between iterations, only the spend cap,
    and changing that is app/research/jobs.py's call, not this file's
    (it is outside this milestone's file list). Its writes can land
    *after* `RESTART IDENTITY` has reset the id sequences, which means a
    `StudyClip`/`StudyCard` insert still carrying the old `job_id` could
    attach stray rows to a **different, brand-new** `study_job` the user
    starts moments later and that happens to be issued the same,
    now-reused id. Cancelling first does not close that window -- it
    only shrinks it from "every research job in flight" down to "the
    rare one already past its first status read when the wipe lands".
    A complete fix needs the job's own loop to recheck its status, which
    is why this comment says "shrinks", not "eliminates".
    """
    await session.execute(
        sql_update(StudyJob)
        .where(StudyJob.status.in_(_STUDY_JOB_NON_TERMINAL))
        .values(status="cancelled")
    )
    await session.execute(
        sql_delete(Job)
        .where(Job.kind == _RESEARCH_JOB_KIND)
        .where(Job.status == "pending")
    )


async def delete_everything(
    session: AsyncSession, settings: Settings, clock: Clock
) -> None:
    """Wipe every content table and reset user_state, in one transaction.

    user_state is updated, never deleted and reinserted: get_state()
    raises NoResultFound on a missing row, so any window without it
    would crash every message until the next restart.

    One state_change row is written afterwards, outside the wipe. The
    reset changes six state fields, and this would otherwise be the only
    state mutation in the codebase with no audit behind it. It carries
    the fact and the time and no content whatsoever -- so an /export run
    straight after a delete shows exactly one row, which is mildly
    surprising and more honest than a log with a hole in it.
    """
    # 4d: cancel research work in flight before the wipe -- see
    # cancel_research_jobs' own docstring for what this does and does
    # not guarantee against a concurrent worker.
    await cancel_research_jobs(session)
    await session.execute(
        sql_text(f"TRUNCATE TABLE {', '.join(PURGED_TABLES)} RESTART IDENTITY")
    )
    await session.execute(
        sql_update(UserState).where(UserState.id == STATE_ID).values(**reset_values(settings, clock))
    )
    await session.commit()

    await record_change(
        session, field="data", old_value=None, new_value="deleted", source="command"
    )
    logger.info("all user data deleted", extra={"event": "delete"})

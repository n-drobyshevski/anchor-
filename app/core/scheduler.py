"""The heartbeat: deciding what to plan, and when (phase-3 plan section 6).

**No APScheduler, and no second process.** Scheduling is the Phase 2
`job` table's `run_after` plus a 60-second loop in the existing worker
(app/worker.py). One scheduling system, no new dependency.

The division of labour matters more than it looks:

    heartbeat()  plans   -- a few indexed SELECTs, at most one INSERT
    send_outbound  does  -- the model call, the Telegram send

The heartbeat never generates and never sends. That is what keeps it
from starving inbound message handling: it runs on the same event loop
as the claim loop, so anything slow in here delays a reply the user is
waiting on. Everything slow goes in the job queue instead, where
app/worker.py's existing rule -- claim updates first, jobs only when
the update queue is empty -- already guarantees an inbound message
outranks a proactive one.

**Duplicate planning is free.** The insert is ON CONFLICT DO NOTHING
against `unique (kind, local_date, bucket)`, so two heartbeats racing
during a Railway rollout produce one row and one message. Nothing here
relies on there being a single worker.

**Priority.** At most one intent is planned per heartbeat, and it is
the highest-priority one whose *timing* is due:
`evening_nag > morning > silence`. If that one's gate refuses, nothing
is planned this tick and the lower ones wait -- plan section 6 is
explicit that the others retry on later heartbeats and usually hit
`min_gap` or the budget, "which is intended".

In practice the two fixed intents' windows never overlap, so the rule
mostly decides whether the silence nudge may be considered this
minute. The nudge has no time window at all -- it is evaluated on
every heartbeat and gated entirely on elapsed silence (48h with focus
on) -- so without the priority rule it would race the morning message
for the same minute on a day the user has been quiet.

**A failed planning gate inserts nothing.** Not a `skipped` row: the
next heartbeat tries again, so lifting `/quiet` at 10:00 still lets the
morning message go out inside its grace window. A row would make that
impossible.

**The tick (3d) is not in PRIORITY.** Plan section 6 lists it as its
own bullet, and for a good reason: the heartbeat does not decide
anything about it. It enqueues a `tick_decide` job and moves on; that
job runs the gate, asks the cheap model, and only then plans a row.
So the tick enqueue is an independent side effect that happens
*before* the priority loop -- the loop returns early when a gate
refuses, and that must not silently suppress the tick as well.
"""

from __future__ import annotations

import datetime
import logging
import random

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import Clock
from app.core.outbound import PLANNED, load_gate_inputs
from app.core.outbound_gate import (
    EVENING_NAG,
    MORNING,
    OK,
    SILENCE,
    Kind,
    config_from_settings,
    gate,
)
from app.core.outbound_send import SEND_OUTBOUND, outbound_dedup_key
from app.core.state import get_state
from app.db.jobs import enqueue_job
from app.db.models import Outbound

logger = logging.getLogger(__name__)

# Highest first (plan section 6).
PRIORITY: tuple[Kind, ...] = (EVENING_NAG, MORNING, SILENCE)

# Fixed intents and the silence nudge are one-per-local-date, so their
# bucket is always 0. Only the tick (3d) uses it, for the local hour.
FIXED_BUCKET = 0

# 3d: the tick's decision job.
#
# The repo convention is that a job-kind constant lives with the job
# body -- EXTRACT in extract.py, SUMMARIZE_SCENE in scene.py,
# SEND_OUTBOUND in outbound_send.py. This one deviates, deliberately.
# app/core/tick.py needs plan() and planned_for() from this module, so
# if this module also imported tick.py for the constant the two would
# form an import cycle. It lands here rather than there because the
# tick is the one job kind enqueued on a *clock rule* by the scheduler
# rather than by whoever happens to need the work done -- the
# scheduler genuinely owns when it exists.
TICK_DECIDE = "tick_decide"

# Plan section 6: "When the local hour is in TICK_HOURS and minute < 5".
# Five minutes rather than one so a worker that restarts at :03 still
# catches the hour; the dedup key collapses all five into one job.
TICK_MINUTE_WINDOW = 5


def tick_dedup_key(local_date: datetime.date, hour: int) -> str:
    """One decision per (local date, hour), ever."""
    return f"tick:{local_date.isoformat()}:{hour}"


def _window(
    kind: Kind, settings: Settings, clock: Clock, timezone: str
) -> tuple[datetime.datetime, datetime.datetime] | None:
    """The [target, grace_end) window a fixed intent may be planned in.

    None for kinds that have no clock window at all -- the silence
    nudge is evaluated on every heartbeat and is gated by elapsed
    silence, not by a time of day.

    The evening nag's window is clamped to `QUIET_START`: a nag that
    lands during quiet hours is exactly what quiet hours exist to
    prevent, and plan section 2 says so outright. The morning message
    keeps the full `SEND_GRACE_MIN`.
    """
    if kind == MORNING:
        time_of_day = settings.MORNING_TIME
    elif kind == EVENING_NAG:
        time_of_day = settings.EVENING_TIME
    else:
        return None

    today = clock_module.local_date(clock, timezone)
    target = clock_module.combine_local(today, time_of_day, timezone)
    grace_end = target + datetime.timedelta(minutes=settings.SEND_GRACE_MIN)

    if kind == EVENING_NAG:
        quiet_start = clock_module.combine_local(today, settings.QUIET_START, timezone)
        grace_end = min(grace_end, quiet_start)

    return target, grace_end


def _is_due(
    kind: Kind, settings: Settings, clock: Clock, timezone: str
) -> bool:
    """Is `kind` inside its planning window right now?"""
    window = _window(kind, settings, clock, timezone)
    if window is None:
        # The silence nudge has no window; the gate decides entirely.
        return True
    target, grace_end = window
    now = clock.now_utc()
    return target <= now < grace_end


def _ceiling(
    kind: Kind, settings: Settings, clock: Clock, timezone: str
) -> datetime.datetime:
    """The latest instant a send may be scheduled for.

    Two limits, whichever is tighter:

    - the kind's own grace window, for the fixed intents;
    - the start of quiet hours, for **every** kind.

    Quiet hours bound everything because planning only happens outside
    them (gate row 4), so a plan made at 22:25 is legitimate -- but its
    jitter must not carry the send across the boundary, where the
    send-time gate would refuse it.
    """
    today = clock_module.local_date(clock, timezone)
    ceiling = clock_module.combine_local(today, settings.QUIET_START, timezone)

    window = _window(kind, settings, clock, timezone)
    if window is not None:
        ceiling = min(ceiling, window[1])
    return ceiling


def planned_for(
    kind: Kind, settings: Settings, clock: Clock, timezone: str
) -> datetime.datetime:
    """When to actually send: now plus jitter, clamped to the ceiling.

    The jitter exists so Anchor does not arrive at exactly 09:00:00
    every single day. The clamp exists because without it a plan made
    at 22:29 with 15 minutes of jitter would be scheduled for 22:44 --
    inside quiet hours, where the send-time gate would refuse it. The
    message would be silently skipped rather than sent late, and late
    is the better of the two.
    """
    now = clock.now_utc()
    jitter_seconds = random.randint(0, max(0, settings.JITTER_MAX_MIN) * 60)
    planned_for = now + datetime.timedelta(seconds=jitter_seconds)

    latest = _ceiling(kind, settings, clock, timezone) - datetime.timedelta(seconds=1)
    return min(planned_for, max(now, latest))


async def _already_exists(
    session: AsyncSession, kind: Kind, local_date: datetime.date
) -> bool:
    """Has this intent already been planned (or sent, or refused) today?

    Any status counts, not just `planned`. A `sent` row obviously must
    not be re-planned; a `cancelled` one must not either, because a
    pause word at 09:05 that cancelled today's morning message should
    not see it re-planned by the heartbeat sixty seconds later.
    """
    result = await session.execute(
        select(Outbound.id)
        .where(Outbound.kind == kind)
        .where(Outbound.local_date == local_date)
        .where(Outbound.bucket == FIXED_BUCKET)
        .limit(1)
    )
    return result.first() is not None


async def plan(
    session: AsyncSession,
    settings: Settings,
    clock: Clock,
    kind: Kind,
    *,
    local_date: datetime.date,
    planned_for: datetime.datetime,
    bucket: int = FIXED_BUCKET,
    tick_note: str | None = None,
) -> int | None:
    """Insert one outbound row and queue its send. Returns the id, or None.

    None means the row already existed -- a concurrent heartbeat won.
    The job is enqueued only when we actually inserted, so a losing
    heartbeat neither duplicates the row nor the job.
    """
    result = await session.execute(
        pg_insert(Outbound)
        .values(
            kind=kind,
            local_date=local_date,
            bucket=bucket,
            planned_for=planned_for,
            status=PLANNED,
            tick_note=tick_note,
        )
        .on_conflict_do_nothing(index_elements=["kind", "local_date", "bucket"])
        .returning(Outbound.id)
    )
    row = result.first()
    if row is None:
        await session.commit()
        return None

    outbound_id = row[0]
    await enqueue_job(
        session,
        SEND_OUTBOUND,
        {"outbound_id": outbound_id},
        dedup_key=outbound_dedup_key(outbound_id),
        run_after=planned_for,
    )
    logger.info(
        "outbound planned",
        extra={
            "outbound_id": outbound_id,
            "event": kind,
            "planned_for": planned_for.isoformat(),
        },
    )
    return outbound_id


async def maybe_enqueue_tick(
    session: AsyncSession, settings: Settings, clock: Clock, timezone: str
) -> bool:
    """Queue this hour's tick decision, if this is one of its minutes.

    Returns True iff a job was actually inserted.

    The gate is **not** run here. Plan section 8 puts it inside the job,
    as its first step, before any model call -- so a refused tick costs
    one queue row and nothing else. Running it here as well would mean
    two different answers to the same question minutes apart, and the
    later one is the one that matters.

    `local_date` and `hour` travel in the payload rather than being
    recomputed when the job runs: the queue can run a job a minute
    late, and the outbound row's `bucket` has to match the dedup key
    that reserved it or the two idempotency mechanisms disagree.
    """
    if not settings.TICK_HOURS:
        return False

    now_local = clock_module.now_local(clock, timezone)
    if now_local.hour not in settings.TICK_HOURS:
        return False
    if now_local.minute >= TICK_MINUTE_WINDOW:
        return False

    local_date = now_local.date()
    enqueued = await enqueue_job(
        session,
        TICK_DECIDE,
        {"local_date": local_date.isoformat(), "hour": now_local.hour},
        dedup_key=tick_dedup_key(local_date, now_local.hour),
    )
    if enqueued:
        logger.info("tick decision queued", extra={"event": now_local.hour})
    return enqueued


async def heartbeat(
    session: AsyncSession, settings: Settings, clock: Clock
) -> int | None:
    """One tick. Plans at most one intent; returns its id, or None.

    Returning the id (rather than nothing) is for the tests and for the
    log line -- the worker ignores it. The tick enqueue is a side
    effect and is not reflected in the return value: it plans nothing.
    """
    state = await get_state(session)
    timezone = state.timezone
    today = clock_module.local_date(clock, timezone)
    config = config_from_settings(settings)

    # Before the loop: see the module docstring. A refused gate below
    # returns early, and the tick must not be collateral damage.
    await maybe_enqueue_tick(session, settings, clock, timezone)

    for kind in PRIORITY:
        if not _is_due(kind, settings, clock, timezone):
            continue
        if await _already_exists(session, kind, today):
            continue

        gate_state, counts, facts = await load_gate_inputs(
            session, clock, settings, state, kind=kind
        )
        verdict = gate(kind, gate_state, clock.now_utc(), counts, facts, config)
        if not verdict.allowed:
            # Deliberately no row: see the module docstring.
            logger.info(
                "outbound not planned", extra={"event": kind, "reason": verdict.reason}
            )
            return None

        assert verdict.reason == OK
        return await plan(
            session,
            settings,
            clock,
            kind,
            local_date=today,
            planned_for=planned_for(kind, settings, clock, timezone),
        )

    return None

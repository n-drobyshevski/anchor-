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

**4d adds the research lifecycle sweep, but deliberately NOT the same
way.** `maybe_enqueue_research_sweep` below looks exactly like
`maybe_enqueue_tick` -- same dedup-key idempotency, same "the heartbeat
only asks, a job does the work" shape -- and the first draft of this
milestone called it from inside `heartbeat()`, right next to
`maybe_enqueue_tick`. That broke tests/test_scheduler.py and
tests/test_outbound_send.py and tests/test_tick.py: several of their
tests call `heartbeat()` directly and then assert an *exact* job-table
state afterwards -- "a failed planning gate inserts no row" there means
literally zero `job` rows, not zero *outbound-planning* rows. Those
tests predate 4d and are not this milestone's to change, so the sweep
enqueue does not belong inside `heartbeat()`. It is instead called by
app/worker.py's `_heartbeat_loop`, once a minute, as a sibling step
right after `heartbeat()` returns -- same cadence, same dedup-key
idempotency, but no longer able to perturb what calling `heartbeat()`
itself does or does not insert.
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
    WEEKLY_REVIEW,
    Kind,
    config_from_settings,
    gate,
)
from app.core.notebook import NOTEBOOK_EXPIRY
from app.core.orders import ORDERS_EXPIRY
from app.core.outbound_send import SEND_OUTBOUND, outbound_dedup_key
from app.core.review import REVIEW_EXPIRY
from app.core.state import get_state
from app.db.jobs import enqueue_job
from app.db.models import Outbound
from app.planner import auth as planner_auth
from app.planner.jobs import PLANNER_SYNC
from app.research.sweeps import RESEARCH_SWEEP
from app.core import retention as retention_module
from app.ops import backup as backup_module

logger = logging.getLogger(__name__)

# Highest first (plan section 6; phase-5 plan section 8: "evening_nag >
# weekly_review > morning > silence"). 5d: on a Sunday evening the
# evening nag may take a few ticks to be planned or refused, and the
# review is only planned once that has happened -- the grace window
# below (clamped to QUIET_START, same as the evening nag's own) leaves
# room for that, and tests/test_scheduler.py covers it.
PRIORITY: tuple[Kind, ...] = (EVENING_NAG, WEEKLY_REVIEW, MORNING, SILENCE)

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
    elif kind == WEEKLY_REVIEW:
        time_of_day = settings.REVIEW_TIME
    else:
        return None

    today = clock_module.local_date(clock, timezone)

    # 5d: the review only has a window at all on REVIEW_DOW. Returning
    # None here (like the silence nudge's "no window") would be wrong --
    # it would make _is_due() true on every day of the week instead of
    # none. A window whose grace_end equals its own target is never due
    # (`target <= now < grace_end` is false for target==grace_end), which
    # is the correct "not today" answer while keeping the same tuple
    # shape every other caller of this function expects.
    if kind == WEEKLY_REVIEW and today.isoweekday() != settings.REVIEW_DOW:
        target = clock_module.combine_local(today, time_of_day, timezone)
        return target, target

    target = clock_module.combine_local(today, time_of_day, timezone)
    grace_end = target + datetime.timedelta(minutes=settings.SEND_GRACE_MIN)

    if kind in (EVENING_NAG, WEEKLY_REVIEW):
        # The grace runs until QUIET_START, same clamp for both: a nag or
        # a review that lands during quiet hours is exactly what quiet
        # hours exist to prevent (plan section 2; phase-5 plan section 8).
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


def pick_send_time(
    kind: Kind, settings: Settings, clock: Clock, timezone: str
) -> datetime.datetime:
    """When to actually send: now plus jitter, clamped to the ceiling.

    Named `pick_send_time` rather than `planned_for` because this file
    already has three of those: the `Outbound.planned_for` column, the
    keyword argument of plan() below, and the local variable two lines
    down. A fourth would read as `planned_for=planned_for(...)` at
    every call site, and the first person to use the helper from inside
    plan() -- where the name is already bound to a datetime -- would
    get `TypeError: 'datetime.datetime' object is not callable`.

    The jitter exists so Anchor does not arrive at exactly 09:00:00
    every single day. The clamp exists because without it a plan made
    at 22:29 with 15 minutes of jitter would be scheduled for 22:44 --
    inside quiet hours, where the send-time gate would refuse it. The
    message would be silently skipped rather than sent late, and late
    is the better of the two.
    """
    now = clock.now_utc()
    jitter_seconds = random.randint(0, max(0, settings.JITTER_MAX_MIN) * 60)
    candidate = now + datetime.timedelta(seconds=jitter_seconds)

    latest = _ceiling(kind, settings, clock, timezone) - datetime.timedelta(seconds=1)
    return min(candidate, max(now, latest))


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


def research_sweep_dedup_key(local_date: datetime.date) -> str:
    """One sweep per local date, ever -- mirrors `tick_dedup_key` above."""
    return f"research_sweep:{local_date.isoformat()}"


async def maybe_enqueue_research_sweep(
    session: AsyncSession, clock: Clock, timezone: str
) -> bool:
    """Queue today's research lifecycle sweep, at most once per local day.

    Card expiry and clip-text retention (phase-4 plan sections 9 and 4)
    -- app/research/sweeps.py does the actual work; this only decides
    *whether* to ask for it. Shaped exactly like `maybe_enqueue_tick`
    above (same dedup-keyed enqueue, same "returns True iff a row was
    actually inserted"), but **not called from `heartbeat()`** -- see
    the module docstring for why. app/worker.py's `_heartbeat_loop`
    calls this directly, once a minute, right after it calls
    `heartbeat()`.

    Idempotency comes entirely from `research_sweep_dedup_key`, the same
    ON CONFLICT DO NOTHING mechanism app/db/jobs.enqueue_job already
    gives every dedup-keyed job: a second call the same minute, two
    workers racing during a rollout, or a worker restart later the same
    day all resolve to a no-op. That is also what makes this safe to run
    twice in the sense the milestone asks for: running the *sweep
    itself* twice is additionally safe on its own terms
    (app/research/sweeps.py's functions are idempotent), so even a
    dedup-key collision failing to save you would not double anything.

    Deliberately has no hour window, unlike the tick: any call on a
    local date this account has not yet swept for queues it, so the
    exact minute depends only on when the day first rolls over, not on
    a schedule knob nobody asked the plan for.

    Deliberately **not** gated on `RESEARCH_ENABLED`. Both sweeps are
    hygiene on rows that already exist, from before the switch was ever
    turned off -- gating this on the switch would let 30-day-old page
    text and a growing pile of stale pending cards sit in the database
    for as long as the feature stays off, which is exactly what plan
    section 4's retention rule exists to prevent.
    """
    local_date = clock_module.local_date(clock, timezone)
    enqueued = await enqueue_job(
        session, RESEARCH_SWEEP, {}, dedup_key=research_sweep_dedup_key(local_date)
    )
    if enqueued:
        logger.info("research sweep queued", extra={"event": RESEARCH_SWEEP})
    return enqueued


def notebook_expiry_dedup_key(local_date: datetime.date) -> str:
    """One sweep per local date, ever -- mirrors `research_sweep_dedup_key`."""
    return f"notebook_expiry:{local_date.isoformat()}"


async def maybe_enqueue_notebook_expiry(
    session: AsyncSession, clock: Clock, timezone: str
) -> bool:
    """Queue today's notebook thread-expiry sweep, at most once per local day.

    Modelled exactly on `maybe_enqueue_research_sweep` right above --
    same dedup-keyed enqueue, same "not called from `heartbeat()`" split
    (app/worker.py's `_heartbeat_loop` calls this as a third sibling
    step, right after the research sweep), for the same reason: several
    tests call `heartbeat()` directly and assert an exact `job` table
    state afterwards, and this must not perturb that.

    Unlike `maybe_enqueue_research_sweep`, this one has no "deliberately
    not gated on a feature switch" note -- the notebook has no switch to
    gate on.
    """
    local_date = clock_module.local_date(clock, timezone)
    enqueued = await enqueue_job(
        session, NOTEBOOK_EXPIRY, {}, dedup_key=notebook_expiry_dedup_key(local_date)
    )
    if enqueued:
        logger.info("notebook expiry queued", extra={"event": NOTEBOOK_EXPIRY})
    return enqueued


def orders_expiry_dedup_key(local_date: datetime.date) -> str:
    """One sweep per local date, ever -- mirrors `notebook_expiry_dedup_key`."""
    return f"orders_expiry:{local_date.isoformat()}"


async def maybe_enqueue_orders_expiry(session: AsyncSession, clock: Clock, timezone: str) -> bool:
    """Queue today's standing-order expiry sweep, at most once per local day.

    Modelled exactly on `maybe_enqueue_notebook_expiry` right above --
    same dedup-keyed enqueue, same "not called from `heartbeat()`" split
    (app/worker.py's `_heartbeat_loop` calls this as a fourth sibling
    step), for the same reason: several tests call `heartbeat()`
    directly and assert an exact `job` table state afterwards, and this
    must not perturb that.
    """
    local_date = clock_module.local_date(clock, timezone)
    enqueued = await enqueue_job(
        session, ORDERS_EXPIRY, {}, dedup_key=orders_expiry_dedup_key(local_date)
    )
    if enqueued:
        logger.info("orders expiry queued", extra={"event": ORDERS_EXPIRY})
    return enqueued


def review_expiry_dedup_key(local_date: datetime.date) -> str:
    """One sweep per local date, ever -- mirrors `orders_expiry_dedup_key`."""
    return f"review_expiry:{local_date.isoformat()}"


async def maybe_enqueue_review_expiry(session: AsyncSession, clock: Clock, timezone: str) -> bool:
    """Queue today's review-proposal expiry sweep, at most once per local day.

    Modelled exactly on `maybe_enqueue_orders_expiry` right above -- same
    dedup-keyed enqueue, same "not called from `heartbeat()`" split
    (app/worker.py's `_heartbeat_loop` calls this as a fifth sibling
    step), for the same reason: several existing tests call `heartbeat()`
    directly and assert an exact `job` table state afterwards.
    """
    local_date = clock_module.local_date(clock, timezone)
    enqueued = await enqueue_job(
        session, REVIEW_EXPIRY, {}, dedup_key=review_expiry_dedup_key(local_date)
    )
    if enqueued:
        logger.info("review expiry queued", extra={"event": REVIEW_EXPIRY})
    return enqueued


async def maybe_enqueue_backup(
    session: AsyncSession, settings: Settings, clock: Clock, timezone: str
) -> bool:
    """Queue tonight's encrypted backup, at most once per local day
    (Phase 6 plan section 9.1; milestone 6e).

    Modelled on `maybe_enqueue_research_sweep`'s dedup-keyed enqueue and
    "not called from `heartbeat()`" split (app/worker.py's
    `_heartbeat_loop` calls this as a sibling step), but unlike every
    sweep above it this one *is* gated -- on `BACKUP_ENABLED` (the same
    global-kill-switch shape as `OUTBOUND_ENABLED`/`IDLE_ENABLED`) and
    on the local wall clock reaching `BACKUP_TIME`. It is deliberately
    NOT gated on the idle budget, the idle window, or
    `user_state.persona_active` -- a backup is infrastructure, not
    courtesy, and app/ops/backup.py's own job runs with no provider and
    no bot regardless of whether the persona would speak right now.

    No grace window: any heartbeat at or after `BACKUP_TIME` local, on a
    day nothing has queued yet, queues it -- unlike the fixed outbound
    intents, a backup that runs at 04:07 instead of 04:00 because the
    worker restarted has lost nothing worth clamping.
    """
    if not settings.BACKUP_ENABLED:
        return False
    local_date = clock_module.local_date(clock, timezone)
    target = clock_module.combine_local(local_date, settings.BACKUP_TIME, timezone)
    if clock.now_utc() < target:
        return False
    enqueued = await enqueue_job(
        session, backup_module.BACKUP, {}, dedup_key=backup_module.backup_dedup_key(local_date)
    )
    if enqueued:
        logger.info("backup queued", extra={"event": backup_module.BACKUP})
    return enqueued


async def maybe_enqueue_retention_sweep(
    session: AsyncSession, clock: Clock, timezone: str
) -> bool:
    """Queue today's retention sweep, at most once per local day (Phase 6
    plan section 9.4; milestone 6e).

    Modelled exactly on `maybe_enqueue_notebook_expiry` -- same
    dedup-keyed enqueue, same "not inside `heartbeat()`" split, same
    "no gate beyond the dedup key" shape: housekeeping on rows that
    already exist, unconditional on every other switch in this file.
    """
    local_date = clock_module.local_date(clock, timezone)
    enqueued = await enqueue_job(
        session,
        retention_module.RETENTION_SWEEP,
        {},
        dedup_key=retention_module.retention_sweep_dedup_key(local_date),
    )
    if enqueued:
        logger.info("retention sweep queued", extra={"event": retention_module.RETENTION_SWEEP})
    return enqueued


def planner_sync_dedup_key(local_date: datetime.date, hour: int, bucket: int) -> str:
    """One planner sync per (local date, hour, bucket) -- mirrors tick_dedup_key.

    `bucket` is left to the caller rather than fixed here: the heartbeat
    below uses `minute // 15` (its own steady cadence), while
    app/core/turn.py's stale-snapshot trigger uses a finer `minute // 5`
    so a chat turn that notices staleness does not wait a full quarter
    hour for the next fetch. The two never need to collide -- a losing
    duplicate enqueue is free, same as every other dedup-keyed job here.
    """
    return f"planner_sync:{local_date.isoformat()}:{hour}:{bucket}"


async def maybe_enqueue_planner_sync(
    session: AsyncSession, settings: Settings, clock: Clock, timezone: str
) -> bool:
    """Queue a PLANNER_SYNC job, roughly every PLANNER_SYNC_EVERY_MIN minutes.

    Shaped like maybe_enqueue_research_sweep: this only decides whether
    to ask, app/planner/jobs.py does the actual work. Deliberately not
    called from inside heartbeat() either, for the identical reason that
    function's docstring gives for the research sweep -- several
    existing tests call heartbeat() directly and assert an exact job
    table state afterwards. app/worker.py's _heartbeat_loop calls this as
    a second sibling step, alongside maybe_enqueue_research_sweep.

    Also queues one extra sync ~10 minutes before MORNING_TIME (design
    review section 2.3), on its own dedup key, so the morning message
    reflects a same-morning agenda rather than whatever the last
    15-minute tick happened to catch.

    No-ops entirely when PLANNER_ENABLED is False -- the whole feature
    stays dark, unlike the research sweep, which runs regardless of its
    own switch because it is retention hygiene on rows that may already
    exist. There is no equivalent backlog here: with the feature off,
    nothing ever populated planner_snapshot in the first place.
    """
    if not settings.PLANNER_ENABLED:
        return False
    if not await planner_auth.is_enabled(session):
        return False

    now_local = clock_module.now_local(clock, timezone)
    today = now_local.date()
    enqueued = False

    key = planner_sync_dedup_key(today, now_local.hour, now_local.minute // 15)
    if await enqueue_job(session, PLANNER_SYNC, {}, dedup_key=key):
        enqueued = True

    premorning_target = clock_module.combine_local(
        today, settings.MORNING_TIME, timezone
    ) - datetime.timedelta(minutes=10)
    now = clock.now_utc()
    if premorning_target <= now < premorning_target + datetime.timedelta(minutes=1):
        premorning_key = f"planner_sync:{today.isoformat()}:premorning"
        if await enqueue_job(session, PLANNER_SYNC, {}, dedup_key=premorning_key):
            enqueued = True

    if enqueued:
        logger.info("planner sync queued", extra={"event": PLANNER_SYNC})
    return enqueued


async def heartbeat(
    session: AsyncSession, settings: Settings, clock: Clock
) -> int | None:
    """One tick. Plans at most one intent; returns its id, or None.

    Returning the id (rather than nothing) is for the tests and for the
    log line -- the worker ignores it. The tick enqueue is a side
    effect and is not reflected in the return value: it plans nothing.

    Does **not** also enqueue the research sweep (4d) -- see the module
    docstring for why that call deliberately lives in app/worker.py's
    `_heartbeat_loop` instead of here.
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
            planned_for=pick_send_time(kind, settings, clock, timezone),
        )

    return None

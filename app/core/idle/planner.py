"""`plan_idle` -- one heartbeat tick's worth of idle planning (approved
plan §2, §5).

app/worker.py's `_heartbeat_loop` calls this as a sibling step right
after `heartbeat()` returns, the same "not inside heartbeat()" split
app/core/scheduler.py's module docstring gives for the research sweep,
the notebook sweep, the orders sweep and the review sweep: several
tests call `heartbeat()` directly and assert an exact `job` table state
afterwards, and this must not perturb that.

At most one `idle_run` + `job` pair is inserted per call, in one
transaction (`_insert_run` below relies on `enqueue_job`'s own commit to
cover both). **Skip records are throttled**: a `skipped` idle_run row is
written only when the global skip reason changes from the last one
recorded, so "user_active x6" in /digest means six silent-then-active
episodes, not 360 one-minute rows.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import Clock
from app.core.idle import (
    BACKFILL,
    CANARY,
    CONSOLIDATE,
    CRITIQUE,
    IDLE_RUN,
    PREBRIEF,
    REFLECT,
    RESEARCH,
)
from app.core.idle.facts import load_idle_facts
from app.core.idle.gate import BUSY, GateResult, config_from_settings, idle_gate
from app.db.jobs import enqueue_job
from app.db.models import IdleRun, UserState

logger = logging.getLogger(__name__)

# Priority order, plan section 5. Only BACKFILL has a real kind rule in
# 6a; the rest return kind_rule:not_implemented and so are never
# actually planned yet, but they still occupy their place in the order
# for when 6b-6e give them one.
PRIORITY: tuple[str, ...] = (BACKFILL, CONSOLIDATE, PREBRIEF, REFLECT, CRITIQUE, RESEARCH, CANARY)


def idle_dedup_key(kind: str, local_date, n: int) -> str:
    """One idle job per (kind, local date, sequence number), ever."""
    return f"idle:{kind}:{local_date.isoformat()}:{n}"


# A queued or running idle_run older than this has lost its job (a crash
# between claim and finish, or a job that died in the queue). Left alone
# it would hold row 6 (`busy`) forever and idle would never run again.
STALE_AFTER = datetime.timedelta(minutes=30)


async def _last_skip_reason(session: AsyncSession) -> str | None:
    """The skip reason of the most recent idle_run of *any* status, or
    None when the latest row is not a skip. A run in between therefore
    starts a new episode: "user_active, run, user_active" records the
    second user_active too."""
    result = await session.execute(
        select(IdleRun.status, IdleRun.skip_reason).order_by(IdleRun.id.desc()).limit(1)
    )
    row = result.first()
    if row is None or row.status != "skipped":
        return None
    return row.skip_reason


async def _fail_stale_runs(session: AsyncSession, clock: Clock) -> None:
    now = clock.now_utc()
    result = await session.execute(
        select(IdleRun)
        .where(IdleRun.status.in_(("queued", "running")))
        # The database's clock, because created_at is the database's
        # own default: comparing it to an injected test clock would
        # call a fresh row stale whenever the two disagree.
        .where(IdleRun.created_at < func.now() - STALE_AFTER)
    )
    stale = list(result.scalars().all())
    for run in stale:
        run.status = "failed"
        run.skip_reason = "stale"
        run.finished_at = now
        logger.warning("idle run stale", extra={"run_id": run.id})
    if stale:
        await session.commit()


async def _record_skip_if_changed(
    session: AsyncSession, clock: Clock, local_date, reason: str
) -> None:
    last = await _last_skip_reason(session)
    if last == reason:
        return
    now = clock.now_utc()
    session.add(
        IdleRun(
            kind=PRIORITY[0],
            local_date=local_date,
            status="skipped",
            skip_reason=reason,
            started_at=now,
            finished_at=now,
        )
    )
    await session.commit()
    logger.info("idle planning skipped", extra={"event": reason})


async def _insert_run(
    session: AsyncSession, clock: Clock, local_date, kind: str
) -> int | None:
    """Insert one idle_run(status='queued') + its job, in one transaction."""
    # Every row of this kind today counts, skipped ones included: a
    # preempted run keeps its job row, so reusing its `n` would collide
    # on the dedup key.
    count_result = await session.execute(
        select(func.count())
        .select_from(IdleRun)
        .where(IdleRun.kind == kind)
        .where(IdleRun.local_date == local_date)
    )
    n = count_result.scalar_one() + 1

    run = IdleRun(kind=kind, local_date=local_date, status="queued")
    session.add(run)
    await session.flush()
    run_id = run.id

    # enqueue_job commits, which is also what commits the IdleRun insert
    # above -- the two land in one transaction, per the plan.
    inserted = await enqueue_job(
        session,
        IDLE_RUN,
        {"run_id": run_id},
        dedup_key=idle_dedup_key(kind, local_date, n),
    )
    if not inserted:
        # The key was already taken, so no job will ever run this row;
        # leaving it queued would hold `busy` until it went stale.
        await session.execute(delete(IdleRun).where(IdleRun.id == run_id))
        await session.commit()
        logger.warning("idle run dedup collision", extra={"event": kind})
        return None
    logger.info("idle run planned", extra={"run_id": run_id, "event": kind})
    return run_id


async def plan_idle(session: AsyncSession, settings: Settings, clock: Clock) -> int | None:
    """One tick: plan at most one idle_run, or record a skip transition.

    Returns the new idle_run's id, or None (nothing planned -- either a
    skip was recorded, or the last recorded reason is unchanged and
    nothing at all was written).
    """
    if not settings.IDLE_ENABLED:
        return None

    # A direct query, not app.core.state.get_state(): app/core/idle/ may
    # never import app.core.state, which is the user_state *writer* --
    # see tests/test_idle_isolation.py. Idle only ever reads this row.
    await _fail_stale_runs(session, clock)
    state = (await session.execute(select(UserState).where(UserState.id == 1))).scalar_one()
    timezone = state.timezone
    facts = await load_idle_facts(session, settings, clock, timezone)
    config = config_from_settings(settings)
    now = clock.now_utc()
    local_date = clock_module.local_date(clock, timezone)

    first_verdict: GateResult | None = None
    for kind in PRIORITY:
        verdict = idle_gate(kind, facts, now, config)
        if first_verdict is None:
            first_verdict = verdict
        if verdict.allowed:
            return await _insert_run(session, clock, local_date, kind)

    # Nothing allowed. The "global" skip reason is the highest-priority
    # kind's own verdict: every shared check (rows 1-9) gives the same
    # reason regardless of kind, so this only diverges from a lower kind
    # at the kind-rule row -- which is exactly the detail worth showing
    # ("nothing_to_backfill" rather than a generic "nothing to do").
    assert first_verdict is not None  # PRIORITY is never empty
    if first_verdict.reason == BUSY:
        # Not a skip: an idle job is running, which /digest already
        # shows as that run. Recording it would add a "busy" line per run.
        return None
    await _record_skip_if_changed(session, clock, local_date, first_verdict.reason)
    return None


__all__ = ["PRIORITY", "idle_dedup_key", "plan_idle"]

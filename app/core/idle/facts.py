"""Async loaders that build `IdleFacts` from the database (approved plan
§5: "facts.py -- async loaders that build IdleFacts from the DB").

Kept separate from gate.py so the gate itself stays a pure function with
no session anywhere in its call graph -- the same split
app/core/outbound.py's `load_gate_inputs` keeps from
app/core/outbound_gate.py's `gate()`.
"""

from __future__ import annotations

import datetime
import decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import Clock, to_local
from app.core.idle.candidates import reflect_candidates, summary_candidates
from app.core.idle.consolidate import find_clusters
from app.core.idle.critique import has_new_replies_since, last_done_critique_finished_at
from app.core.idle.gate import IdleFacts
from app.core.idle.prebrief import note_exists as prebrief_note_exists
from app.core.idle.reflect import has_new_summary_since, last_done_reflect_finished_at
from app.core.idle.research import pick_topic as research_pick_topic
from app.core.spend import today_idle_usd, today_usd
from app.db.models import IdleRun, UserState
from app.research.jobs import study_quota_used

ACTIVE_STATUSES = ("queued", "running")

# Mirrors app/core/idle/backfill.py's BACKFILL_UNITS_PER_RUN: the count
# only needs to distinguish "nothing" from "something", so it is capped
# at the same number a single run would ever pick up.
_BACKFILL_CANDIDATE_LIMIT = 3


async def _active_run_ids(session: AsyncSession) -> frozenset[int]:
    result = await session.execute(
        select(IdleRun.id).where(IdleRun.status.in_(ACTIVE_STATUSES))
    )
    return frozenset(row[0] for row in result.all())


async def _jobs_today(session: AsyncSession, local_date) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(IdleRun)
        .where(IdleRun.local_date == local_date)
        .where(IdleRun.status != "skipped")
    )
    return result.scalar_one()


# Statuses that mean "this kind ran today" for the per-kind daily limit.
FINISHED_STATUSES = ("done", "failed", "undone")


async def _kind_runs_today(session: AsyncSession, local_date) -> dict[str, int]:
    result = await session.execute(
        select(IdleRun.kind, func.count())
        .where(IdleRun.local_date == local_date)
        .where(IdleRun.status.in_(FINISHED_STATUSES))
        .group_by(IdleRun.kind)
    )
    return {kind: count for kind, count in result.all()}


async def latest_canary_status(session: AsyncSession) -> tuple[object, bool] | None:
    """`(local_date, passed)` of the most recent *done* canary run, or
    None if none has ever completed -- /state's own canary line
    (app/tg/router.py)."""
    result = await session.execute(
        select(IdleRun.local_date, IdleRun.summary)
        .where(IdleRun.kind == "canary")
        .where(IdleRun.status == "done")
        .order_by(IdleRun.id.desc())
        .limit(1)
    )
    row = result.first()
    if row is None:
        return None
    local_date, summary = row
    return local_date, bool((summary or {}).get("passed", True))


async def idle_jobs_today(session: AsyncSession, clock: Clock, timezone: str) -> int:
    """How many idle jobs (not planner-only skip rows) ran today -- the
    same count `load_idle_facts` puts in `IdleFacts.jobs_today`, exposed
    on its own for /state's "Фон: $x / $cap, задач N" line."""
    local_date = clock_module.local_date(clock, timezone)
    return await _jobs_today(session, local_date)


async def load_idle_facts(
    session: AsyncSession, settings: Settings, clock: Clock, timezone: str
) -> IdleFacts:
    """Build the `IdleFacts` snapshot the gate needs, one query per field."""
    state = (
        await session.execute(select(UserState).where(UserState.id == 1))
    ).scalar_one()
    local_date = clock_module.local_date(clock, timezone)
    local_now = to_local(clock.now_utc(), timezone)

    active_run_ids = await _active_run_ids(session)
    jobs_today = await _jobs_today(session, local_date)
    idle_spend_today = await today_idle_usd(session, clock, timezone)
    spend_today = await today_usd(session, clock, timezone)

    pending_summaries = await summary_candidates(session, _BACKFILL_CANDIDATE_LIMIT)
    pending_reflections = await reflect_candidates(session, clock, _BACKFILL_CANDIDATE_LIMIT)

    # 6b: shared with the jobs themselves (app/core/idle/consolidate.py,
    # app/core/idle/reflect.py) so the gate and the job can never
    # disagree about whether there is anything to do -- same role
    # candidates.py plays for backfill above.
    consolidate_clusters = len(await find_clusters(session))
    last_reflect_at = await last_done_reflect_finished_at(session)
    reflect_has_new_summary = await has_new_summary_since(session, last_reflect_at)

    # 6c: shared with app/core/idle/prebrief.py and app/core/idle/
    # critique.py's own jobs, same "gate and job can never disagree"
    # role as consolidate/reflect's own facts above.
    tomorrow = local_date + datetime.timedelta(days=1)
    prebrief_note_exists_tomorrow = await prebrief_note_exists(session, tomorrow)
    last_critique_at = await last_done_critique_finished_at(session)
    critique_has_new_replies = await has_new_replies_since(session, last_critique_at)

    # 6d: shared with app/core/idle/research.py's own job -- same
    # "gate and job can never disagree" role as the 6b/6c fields above.
    research_topic = await research_pick_topic(session)
    research_quota_used = await study_quota_used(session, settings, clock, timezone)

    return IdleFacts(
        persona_active=state.persona_active,
        local_now=local_now,
        welfare_at=state.welfare_at,
        last_user_msg_at=state.last_user_msg_at,
        active_run_ids=active_run_ids,
        jobs_today=jobs_today,
        idle_spend_today=idle_spend_today,
        spend_today=spend_today,
        daily_usd_cap=decimal.Decimal(str(settings.DAILY_USD_CAP)),
        backfill_candidates=len(pending_summaries) + len(pending_reflections),
        kind_runs_today=await _kind_runs_today(session, local_date),
        consolidate_clusters=consolidate_clusters,
        reflect_has_new_summary=reflect_has_new_summary,
        prebrief_note_exists_tomorrow=prebrief_note_exists_tomorrow,
        critique_has_new_replies=critique_has_new_replies,
        research_has_active_topic=research_topic is not None,
        research_quota_used=research_quota_used,
    )


__all__ = ["idle_jobs_today", "latest_canary_status", "load_idle_facts"]

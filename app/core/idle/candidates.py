"""What `backfill` would pick up (plan section 6.1) -- one query per unit
type, shared by the gate's facts (app/core/idle/facts.py) and the job
itself (app/core/idle/backfill.py) so the two can never disagree about
whether there is anything to do.

**Summaries:** ended scenes with `summary IS NULL` and at least
`MIN_MESSAGES_FOR_SUMMARY` summarizable messages -- the same filter
app/core/scene.run_summarize_scene applies, so welfare, OOC and canned
rows are never counted.

**Reflections:** ended, summarized scenes with no welfare row whose
reflection never happened. "Never happened" cannot be read off
`notebook_entry` alone: a reflection that correctly added nothing
leaves no row, and treating that as missing would pay for the same
empty reflection every day. So a scene is excluded when its live
`nb:<scene_id>` job is pending, running or done, or when an earlier
backfill run already reflected it (its id in that run's summary). What
is left is a reflection whose job failed outright. Only scenes ended in
the last `REFLECT_LOOKBACK` are considered, because done job rows do
not live forever.
"""

from __future__ import annotations

import datetime

from sqlalchemy import cast, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.idle import BACKFILL
from app.core.scene import MIN_MESSAGES_FOR_SUMMARY, SUMMARIZABLE_KINDS
from app.db.models import IdleRun, Job, Message, NotebookEntry, Scene

REFLECT_LOOKBACK = datetime.timedelta(days=14)

# idle_run.summary key listing the scene ids a backfill run reflected on.
REFLECTED_SCENE_IDS = "reflected_scene_ids"


def _summarizable_count():
    return (
        select(func.count())
        .select_from(Message)
        .where(Message.scene_id == Scene.id)
        .where(Message.ooc.is_(False))
        .where(Message.kind.in_(SUMMARIZABLE_KINDS))
        .scalar_subquery()
    )


async def summary_candidates(session: AsyncSession, limit: int) -> list[int]:
    """Scene ids needing a summary, oldest first."""
    result = await session.execute(
        select(Scene.id)
        .where(Scene.ended_at.is_not(None))
        .where(Scene.summary.is_(None))
        .where(_summarizable_count() >= MIN_MESSAGES_FOR_SUMMARY)
        .order_by(Scene.ended_at, Scene.id)
        .limit(limit)
    )
    return [row[0] for row in result.all()]


async def reflect_candidates(session: AsyncSession, clock: Clock, limit: int) -> list[int]:
    """Scene ids whose reflection failed, oldest first."""
    has_entry = select(NotebookEntry.id).where(NotebookEntry.scene_id == Scene.id).exists()
    has_welfare = (
        select(Message.id)
        .where(Message.scene_id == Scene.id)
        .where(Message.kind == "welfare")
        .exists()
    )
    live_job = (
        select(Job.id)
        .where(Job.dedup_key == func.concat("nb:", Scene.id))
        .where(Job.status.in_(("pending", "processing", "done")))
        .exists()
    )
    backfilled = (
        select(IdleRun.id)
        .where(IdleRun.kind == BACKFILL)
        .where(
            IdleRun.summary[REFLECTED_SCENE_IDS].op("@>")(
                cast(func.jsonb_build_array(Scene.id), JSONB)
            )
        )
        .exists()
    )
    result = await session.execute(
        select(Scene.id)
        .where(Scene.ended_at.is_not(None))
        .where(Scene.ended_at >= literal(clock.now_utc() - REFLECT_LOOKBACK))
        .where(Scene.summary.is_not(None))
        .where(_summarizable_count() >= MIN_MESSAGES_FOR_SUMMARY)
        .where(~has_entry)
        .where(~has_welfare)
        .where(~live_job)
        .where(~backfilled)
        .order_by(Scene.ended_at, Scene.id)
        .limit(limit)
    )
    return [row[0] for row in result.all()]


__all__ = ["REFLECTED_SCENE_IDS", "REFLECT_LOOKBACK", "reflect_candidates", "summary_candidates"]

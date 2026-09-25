"""The `backfill` idle kind (Phase 6 plan section 6.1; approved plan §5).

Picks up to `BACKFILL_UNITS_PER_RUN` units of deferred work: scenes that
are ended, have `summary IS NULL` and at least `MIN_MESSAGES_FOR_SUMMARY`
summarizable messages get a summary; then ended, summarized scenes with
no reflection and no welfare message get one. Both reuse
app/core/scene.run_summarize_scene and app/core/notebook.run_notebook_reflect
**verbatim** -- the only change either of those functions gets is the
`spend_category` keyword, so idle spend ledgers as `idle:backfill`
rather than `summary`/`reflect`.

**Preemption is checked between units, never inside one.** Each unit is
already its own atomic, self-committing job body (app/core/scene.py's
and app/core/notebook.py's own docstrings), so there is no "half-written
unit" to protect against -- the guarantee this module keeps is the
approved plan's: "a preempted backfill keeps only whole units the live
pipeline would have produced anyway. It never keeps a half-written
unit." If the check before a unit finds a user update since the run
started, the loop stops there and whatever units already ran stay
exactly as they are.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.clock import Clock
from app.core.idle.candidates import reflect_candidates, summary_candidates
from app.core.idle.runner import is_preempted, spend_since
from app.core.notebook import run_notebook_reflect
from app.core.scene import Deferred, run_summarize_scene
from app.llm.provider import LLMProvider

logger = logging.getLogger(__name__)

# `idle:<kind>` -- the category app/core/spend.today_idle_usd matches on.
IDLE_BACKFILL_CATEGORY = "idle:backfill"

BACKFILL_UNITS_PER_RUN = 3


@dataclasses.dataclass(frozen=True)
class BackfillResult:
    """What one `backfill` run did. No text -- only counts, per plan
    section 8's "Logs and idle_run.summary contain ... never text"."""

    summarized: int
    reflected: int
    preempted: bool
    # Scene ids reflected on, so app/core/idle/candidates.py never picks
    # the same scene twice (ids only -- never text).
    reflected_scene_ids: tuple[int, ...] = ()
    job_cap: bool = False


async def run_backfill(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    provider: LLMProvider,
    safety_provider: LLMProvider,
    clock: Clock,
    *,
    started_at: datetime.datetime,
    timezone: str,
) -> BackfillResult:
    """Summarize and reflect up to `BACKFILL_UNITS_PER_RUN` units total.

    `provider` runs the summary call (prose, same model
    app/worker.py._run_job gives SUMMARIZE_SCENE); `safety_provider`
    runs the reflect call (strict JSON, same model _run_job gives
    NOTEBOOK_REFLECT). Each opens its own session so its commit is not
    shared with the preemption check that precedes it.
    """
    async with session_factory() as session:
        summary_ids = await summary_candidates(session, BACKFILL_UNITS_PER_RUN)
        reflect_ids = await reflect_candidates(session, clock, BACKFILL_UNITS_PER_RUN)

    summarized = 0
    reflected = 0
    reflected_ids: list[int] = []
    was_preempted = False
    job_cap = decimal.Decimal(str(settings.IDLE_JOB_USD_CAP))
    hit_job_cap = False

    async def _may_continue() -> bool:
        nonlocal was_preempted, hit_job_cap
        async with session_factory() as session:
            if await is_preempted(session, clock, started_at):
                was_preempted = True
                return False
        # IDLE_JOB_USD_CAP, checked before each unit: a unit is atomic,
        # so the cap can be overshot by at most one unit's cost.
        if await spend_since(session_factory, started_at) >= job_cap:
            hit_job_cap = True
            return False
        return True

    for scene_id in summary_ids:
        if summarized + reflected >= BACKFILL_UNITS_PER_RUN:
            break
        if not await _may_continue():
            break
        async with session_factory() as session:
            try:
                await run_summarize_scene(
                    session,
                    settings,
                    provider,
                    scene_id=scene_id,
                    clock=clock,
                    timezone=timezone,
                    spend_category=IDLE_BACKFILL_CATEGORY,
                )
            except Deferred:
                logger.info("idle backfill summary deferred", extra={"scene_id": scene_id})
                continue
        summarized += 1

    for scene_id in reflect_ids:
        if summarized + reflected >= BACKFILL_UNITS_PER_RUN:
            break
        if not await _may_continue():
            break
        async with session_factory() as session:
            try:
                await run_notebook_reflect(
                    session,
                    settings,
                    safety_provider,
                    clock=clock,
                    timezone=timezone,
                    scene_id=scene_id,
                    spend_category=IDLE_BACKFILL_CATEGORY,
                )
            except Deferred:
                logger.info("idle backfill reflect deferred", extra={"scene_id": scene_id})
                continue
        reflected += 1
        reflected_ids.append(scene_id)

    return BackfillResult(
        summarized=summarized,
        reflected=reflected,
        preempted=was_preempted,
        reflected_scene_ids=tuple(reflected_ids),
        job_cap=hit_job_cap,
    )


__all__ = ["BACKFILL_UNITS_PER_RUN", "IDLE_BACKFILL_CATEGORY", "BackfillResult", "run_backfill"]

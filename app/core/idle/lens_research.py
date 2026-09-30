"""The `lens_research` idle kind (anchor-lens-plan.md sections 9, 10 and
13; the L4 spec section 2 with the owner's amendments; milestone L4).

A tap on «исследовать и написать» under a lens garden gap queues a `study_job`
(`kind='study'`, `packet='lens'`, `lens_gap_id` set) with **no queue
row** (app/research/jobs.py's `enqueue_lens_study`). This kind is the
only thing that runs one. Being an idle kind gives it preemption, the
idle budget and a /digest line, and keeps it away from Telegram: the
result message is the garden hook's (app/worker.py), sent once the job
has finished (`jobs.unsent_lens_results`).

**Steps.**

1. Preemption, then the oldest queued lens job (`jobs.next_lens_job`,
   the gate's own fact, so the two cannot disagree).
2. If the job has no query yet: `lens.gap_seed` (None -- the gap is
   gone, no longer researched, or names a note that left the lens --
   fails the job as `gap_gone`); the daily spend cap (`cap`); then one
   call, app/research/lens_query.py's `call` and `validate`, whose cost
   `jobs.record_lens_query` charges to the job, so
   `RESEARCH_JOB_USD_CAP` covers it. A refused query fails the job as
   `query_refused`, and nothing is searched. A stored query is never
   rebuilt: a run preempted after this step leaves it for the next.
3. Preemption again, and the gap must still be researched (a garden
   recheck may have resolved it meanwhile; a search for a closed gap
   is money for nothing). Then `run_research_job`, unchanged: it reads
   `PACKET_LENS` and distills in lens mode because the job's packet is
   `lens` (app/research/jobs.py).

**The query call's input is lens-only, structurally.** Its one argument
is `lens.gap_seed`'s `GapSeed`: the gap's kind, detail and proposed
title, and its lens notes' titles and catalog summaries (plan section
10: "gap detail and summaries only"). Dialogs, memory, the journal,
personal notes and knowledge notes have no way in, and the provider is
the shared safety provider, with a strict schema and no web search
(app/research/search.py's `find_urls` stays the only call site that
asks for one). tests/test_lens_query.py pins it byte for byte.

**No quota check.** The job's own row already counts against the day
it was tapped; checking again would refuse the job that spent it.

**Spend.** Like idle `research` (app/core/idle/research.py), everything
is ledgered under `research` by the pipeline, onto the job's `usd_cost`
-- the query call included -- never under `idle:lens_research`. The run
reads back what this run added to the job (`usd_cost`), app/core/idle/
runner.py folds it into `idle_run.usd_cost`, and app/core/spend.py's
`today_idle_usd` adds that to the idle total, so `IDLE_USD_CAP` sees it
once. A provider error from the query call propagates: the runner fails
the run, the job stays queued without a query, and a later run retries
until `LENS_STALE_DAYS` fails it as `stale` (app/research/sweeps.py).

Only app/vault/lens.py is read for the lens (tests/test_idle_isolation.py
allows it to this file and lens_garden.py alone); nothing here adopts a
card or writes the vault. Logs carry ids, codes and counts: never a
title, detail, summary, query, URL or card text.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.clock import Clock
from app.core.spend import check_cap
from app.db.models import StudyJob
from app.llm.provider import LLMProvider
from app.research import jobs as research_jobs
from app.research import lens_query
from app.vault import lens

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class LensResearchResult:
    """What one run did. `summary()` is `idle_run.summary`: ids, codes and
    counts only (app/core/idle/runner.py spreads it into a log call's
    `extra`, so no key shares a `LogRecord` attribute's name).

    `job_id` is None when there was nothing to do (or the run was
    preempted before it took a job); `built` is 1 when this run stored
    the job's query; `error_code` is the job's own when this run ended
    it (`gap_gone`, `cap`, `query_refused`, or the pipeline's)."""

    job_id: int | None = None
    built: int = 0
    searched: int = 0
    cards: int = 0
    hidden: int = 0
    error_code: str | None = None
    usd_cost: decimal.Decimal = decimal.Decimal(0)
    preempted: bool = False

    @property
    def acted(self) -> bool:
        """Did this run change anything? A preempted run that did not is
        `skipped:preempted`, not `done`."""
        return bool(self.built or self.searched or self.error_code)

    def summary(self) -> dict:
        return {
            "job_id": self.job_id,
            "built": self.built,
            "searched": self.searched,
            "cards": self.cards,
            "hidden": self.hidden,
            "error_code": self.error_code,
        }


async def _job_cost(session: AsyncSession, job_id: int) -> decimal.Decimal:
    job = await session.get(StudyJob, job_id)
    return job.usd_cost if job is not None else decimal.Decimal(0)


async def _fail(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock, job_id: int, code: str
) -> None:
    async with session_factory() as session:
        await research_jobs.fail_lens_job(session, clock, job_id, code)
        await session.commit()


async def run_lens_research(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    provider: LLMProvider,
    clock: Clock,
    *,
    run_id: int,
    started_at: datetime.datetime,
    timezone: str,
) -> LensResearchResult:
    """The `lens_research` idle kind body, called by app/core/idle/runner.py
    after its gate re-check and first preemption check (module
    docstring). `provider` is the shared safety provider, as for idle
    `research`: the query call, the searches and the distills all run on
    it, as app/worker.py's `RESEARCH` branch runs a /study."""
    # Lazy, as in research.py: runner.py imports facts.py, which imports
    # app.research.jobs, and runner.py imports this module in its branch.
    from app.core.idle.runner import is_preempted

    async with session_factory() as session:
        if await is_preempted(session, clock, started_at):
            return LensResearchResult(preempted=True)
        job = await research_jobs.next_lens_job(session)
        if job is None:
            return LensResearchResult()
        cost_before = await _job_cost(session, job.id)
        seed = None
        capped = False
        if job.query is None:
            seed = await lens.gap_seed(session, job.gap_id) if job.gap_id is not None else None
            capped = await check_cap(session, settings, clock, timezone)

    built = 0
    if job.query is None:
        if seed is None:
            await _fail(session_factory, clock, job.id, research_jobs.GAP_GONE)
            return LensResearchResult(job_id=job.id, error_code=research_jobs.GAP_GONE)
        if capped:
            await _fail(session_factory, clock, job.id, research_jobs.CAP)
            return LensResearchResult(job_id=job.id, error_code=research_jobs.CAP)
        response = await lens_query.call(provider, seed, gap_id=job.gap_id)
        query = lens_query.validate(lens_query.parse_json(response.text))
        async with session_factory() as session:
            stored = await research_jobs.record_lens_query(
                session, settings, clock, timezone=timezone, job_id=job.id,
                response=response, query=query,
            )
            usd_cost = await _job_cost(session, job.id) - cost_before
            await session.commit()
        if not stored:
            logger.info(
                "lens research query refused",
                extra={"run_id": run_id, "job_id": job.id, "error_code": research_jobs.QUERY_REFUSED},
            )
            return LensResearchResult(
                job_id=job.id, error_code=research_jobs.QUERY_REFUSED, usd_cost=usd_cost
            )
        built = 1

    async with session_factory() as session:
        if await is_preempted(session, clock, started_at):
            usd_cost = await _job_cost(session, job.id) - cost_before
            return LensResearchResult(
                job_id=job.id, built=built, usd_cost=usd_cost, preempted=True
            )
        still_researched = (
            job.gap_id is not None and await lens.gap_seed(session, job.gap_id) is not None
        )
    if not still_researched:
        await _fail(session_factory, clock, job.id, research_jobs.GAP_GONE)
        async with session_factory() as session:
            usd_cost = await _job_cost(session, job.id) - cost_before
        return LensResearchResult(
            job_id=job.id, built=built, error_code=research_jobs.GAP_GONE, usd_cost=usd_cost
        )

    async with session_factory() as session:
        outcome = await research_jobs.run_research_job(
            session, settings, provider, job_id=job.id, url=None, clock=clock, timezone=timezone,
        )

    async with session_factory() as session:
        usd_cost = await _job_cost(session, job.id) - cost_before
        still_preempted = await is_preempted(session, clock, started_at)

    logger.info(
        "lens research ran",
        extra={
            "run_id": run_id,
            "job_id": job.id,
            "cards": outcome.visible_cards,
            "error_code": outcome.error_code or "",
        },
    )
    return LensResearchResult(
        job_id=job.id,
        built=built,
        searched=1,
        cards=outcome.visible_cards,
        hidden=outcome.hidden_cards,
        error_code=outcome.error_code,
        usd_cost=usd_cost,
        preempted=still_preempted,
    )


__all__ = ["LensResearchResult", "run_lens_research"]

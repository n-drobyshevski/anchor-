"""The `research` idle kind (Phase 6 plan section 6.5; milestone 6d).

**Topics come only from the user.** `interest_topic` rows are written
solely by `/interests add <packet> <тема>` (`app/core/interests.py`);
this module only ever *reads* the table (`pick_topic`, shared with
`app/core/idle/facts.py` so the gate and the job can never disagree
about whether there is one to run) and writes back `last_run_at`.

**The pipeline is unchanged.** Milestone 4c's `run_research_job`
(`app/research/jobs.py`) is called exactly the way `app/worker.py`'s
own `RESEARCH` branch calls it for a `/study` job -- `job_id`, `url=
None`, `clock`, `timezone` -- on the same `study_job` table, through
the same search/fetch/distill/risk/injection machinery. Nothing about
*how* a topic is researched differs by who queued it.

**What does differ, deliberately, is how the job is queued.** `/study`
(`app/research/jobs.enqueue_study`) inserts a `study_job` row *and*
enqueues a `job` queue row of kind `research`, which is what makes
`app/worker.py`'s own dispatcher run `run_research_job` and then send
the "Готово: N карточек." line (`_send_research_done`). Plan section
6.5 requires idle research to send **no** completion message at all,
and there is no bot-reachable branch of that dispatch path that can be
told "run this, but don't report" -- `_send_research_done`'s own gate
(`_may_report_now`) is about *when* to speak, not *whether* this run
was idle's. So this module writes the `study_job` row itself (the same
four columns `enqueue_study` sets) and calls `run_research_job`
directly, in the same request, the way `app/core/idle/backfill.py`
calls `run_summarize_scene`/`run_notebook_reflect` directly rather than
going through their own queue kinds -- **no `job` queue row is ever
enqueued for an idle-triggered study, so `app.worker`'s dispatcher
never sees it and the completion message structurally cannot fire.**

**The quota is genuinely shared, not merely mirrored.** Both paths
write into the same `study_job` table with `kind='study'`, and
`research_jobs.study_quota_used` (which `enqueue_study`'s own check
also calls) counts rows in that table for today's local date
regardless of which of the two wrote them. Whichever runs first today
-- the user's own `/study` or an idle run -- the other sees the quota
already spent, with nothing idle-specific for `/study` to special-case.

**Spend.** `run_research_job` ledgers every model call under
`distill.RESEARCH_CATEGORY` ("research"), unchanged -- so it still
counts toward `today_usd`/`check_cap`'s global daily total exactly as
a user's own `/study` would (plan section 8: "Idle can never consume
the live-chat reserve" is enforced by that *global* cap, not by the
category string). It is **not** ledgered under `idle:research`, unlike
every other idle kind's own spend (`RunContext.charge`,
`app/core/idle/backfill.py`'s `spend_category` override) -- doing that
would mean touching `run_research_job`'s own ledger call, which is
exactly the "unchanged pipeline" this module exists to preserve, and
`app/core/spend.today_idle_usd`'s `LIKE 'idle:%'` match is what several
tests (and the plan's own `RunContext.charge` docstring) pin as that
convention's definition. So the idle total reads it from the other
side: this module reads `study_job.usd_cost` back after the run and
returns it on `ResearchResult.usd_cost`, `app/core/idle/runner.py`
folds it into `idle_run.usd_cost`, and `app/core/spend.today_idle_usd`
adds today's `idle_run.usd_cost` for kind `research` to the `idle:%`
ledger sum. A research run therefore counts toward `IDLE_USD_CAP`
(gate row 8) as well as the global total and reserve (row 9), and is
never counted twice. `KIND_DAILY_MAX[RESEARCH] = 1` and the shared
`/study` quota bound it further.

**Preemption.** Checked once, before anything is written: if a user
update has arrived since the run started, nothing is queued and nothing
is spent, and the run reports `preempted=True` with `topic_id=None` --
`app/core/idle/runner.py` reads that as `skipped:preempted`, per plan
section 4's "commits nothing that isn't already committed". Once the
`study_job` row exists and `run_research_job` is under way, there is no
finer-grained check: unlike consolidate/reflect's single JSON call,
`run_research_job` for a `study` job is itself a multi-step external
process (up to two searches, then up to `RESEARCH_MAX_PINS` fetches and
distills), each step already committing its own clip/card rows as it
goes (its own module's "cards already produced" posture, plan section
12). Checking preemption *between* those internal steps would mean
threading a callback into `run_research_job` itself, which is the one
piece of surgery "the pipeline is unchanged" above rules out. So this
module's preemption granularity is one whole `/study` run -- coarser
than consolidate/reflect's one-model-call granularity, comparable to
backfill's "between whole units, never inside one" -- and a run that
was interrupted by a user message partway through still completes
normally and keeps exactly the cards a same-shaped `/study` call would
have produced; nothing is rolled back, and there is no half-written
card, because `run_research_job` never leaves one. A second,
informational check right after the run only sets the summary's
`preempted` flag (mirroring every other 6b/6c kind, `runner.py`'s own
`if getattr(result, "preempted", False)`); it does not undo anything.
"""

from __future__ import annotations

import dataclasses
import decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import Clock
from app.db.models import InterestTopic, StudyJob
from app.llm.provider import LLMProvider
from app.research import jobs as research_jobs

# app/core/idle/runner.py imports app/core/idle/facts.py at module level
# (for load_idle_facts), and facts.py imports this module at module
# level (for pick_topic, its own gate fact) -- so this module cannot
# import runner.py at module level too without a three-way cycle.
# is_preempted is imported lazily, inside run_research below, the same
# "import the sibling kind module only inside the branch that needs it"
# posture app/core/idle/runner.py's own dispatch already uses for every
# kind (including this one).


async def pick_topic(session: AsyncSession) -> InterestTopic | None:
    """The active topic with the oldest `last_run_at`, NULLs first (a
    topic never run outranks one run a year ago). Shared by
    `app/core/idle/facts.py` (for the gate's `research_has_active_topic`)
    and `run_research` below, so the two can never disagree."""
    result = await session.execute(
        select(InterestTopic)
        .where(InterestTopic.active.is_(True))
        .order_by(InterestTopic.last_run_at.asc().nulls_first(), InterestTopic.id.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


@dataclasses.dataclass(frozen=True)
class ResearchResult:
    """What one `research` run did. No text (plan section 8: "idle_run.
    summary ... no text") -- `topic_id` and `cards` only; the topic's
    own text is read back from `interest_topic` at digest render time
    (`app/core/idle/digest.py`), never stored a second time here."""

    topic_id: int | None
    cards: int = 0
    usd_cost: decimal.Decimal = decimal.Decimal(0)
    preempted: bool = False


async def run_research(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    provider: LLMProvider,
    clock: Clock,
    *,
    run_id: int,
    started_at,
    timezone: str,
) -> ResearchResult:
    """Pick a topic, run the unchanged `/study` pipeline on it, done.

    `provider` is the safety model, the same one `app/worker.py`'s own
    `RESEARCH` branch gives `run_research_job` (distill/search are
    strict-schema calls over a stranger's page text, never the main
    persona model) -- `app/core/idle/runner.py` passes its own
    `safety_provider` here, matching that call shape exactly.
    """
    from app.core.idle.runner import is_preempted

    async with session_factory() as session:
        topic = await pick_topic(session)
        if topic is None:
            return ResearchResult(topic_id=None)
        if await research_jobs.study_quota_used(session, settings, clock, timezone):
            return ResearchResult(topic_id=None)

    # Preemption, checked once, before anything is written or spent --
    # see the module docstring on why this is the only check.
    async with session_factory() as session:
        if await is_preempted(session, clock, started_at):
            return ResearchResult(topic_id=None, preempted=True)

    # The same four columns app/research/jobs.enqueue_study sets on a
    # user's own /study -- deliberately not that function itself, which
    # also enqueues a `job` queue row (see the module docstring on why
    # that row must never exist for an idle-triggered run).
    async with session_factory() as session:
        local_date = clock_module.local_date(clock, timezone)
        job = StudyJob(
            kind=research_jobs.STUDY,
            status="queued",
            packet=topic.packet,
            query=topic.text,
            local_date=local_date,
        )
        session.add(job)
        await session.flush()
        job_id = job.id
        await session.commit()

    async with session_factory() as session:
        outcome = await research_jobs.run_research_job(
            session,
            settings,
            provider,
            job_id=job_id,
            url=None,
            clock=clock,
            timezone=timezone,
        )

    async with session_factory() as session:
        job_row = await session.get(StudyJob, job_id)
        usd_cost = job_row.usd_cost if job_row is not None else decimal.Decimal(0)
        topic_row = await session.get(InterestTopic, topic.id)
        if topic_row is not None:
            topic_row.last_run_at = clock.now_utc()
        still_preempted = await is_preempted(session, clock, started_at)
        await session.commit()

    return ResearchResult(
        topic_id=topic.id,
        cards=outcome.visible_cards,
        usd_cost=usd_cost,
        preempted=still_preempted,
    )


__all__ = ["ResearchResult", "pick_topic", "run_research"]

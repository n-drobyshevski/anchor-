"""The `canary` idle kind (Phase 6 plan section 6.7; milestone 6c).

Runs the Phase 5 blocking eval subset in-process, against a **throwaway
database only**, with the currently active persona amendments and the
independent judge -- the exact same runner `amendment_trial` uses
(`eval.trial.run_blocking_subset`), so a silent provider or model drift
on OpenRouter shows up here the same week it happens rather than only
when the next amendment happens to be tried.

**Never touches the live database.** `run_blocking_subset` opens its
own `eval.db.throwaway_sessionmaker` and drops it when done -- this
module's own `session_factory` argument is used only to read the
active amendments before the trial and to record the outcome
afterwards, never inside the trial itself.

**Why this module reads `PersonaAmendment` directly rather than
importing `app.core.amendments`.** Same reasoning as
`app/core/idle/reflect.py`'s own docstring for `app.core.orders`:
`tests/test_idle_isolation.py` bans the whole module ("persona
amendments -- not an idle-writable table"), so `_active_amendment_texts`
below is the read-only query, following
`app/core/review.py`'s own private helper of the same name and shape.

**The judge must be independent**, checked by the gate's kind rule
(`app/core/idle/gate.py`'s `_canary_rule`) before this module is ever
reached -- same as critique.

**Job-lease refresh.** `job_id`, threaded from app/worker.py through
`run_idle`, lets this module call `touch_job_lock` once per blocking
case the same way `app/core/amendments.py`'s `run_trial` does (via its
own `on_case_done`) -- a canary run is the same ~13-case, ~26-call
shape and needs the same lease extension so a second worker cannot
reclaim and re-run it mid-trial.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import Clock
from app.db.models import PersonaAmendment, SpendLedger
from app.llm.provider import LLMProvider

logger = logging.getLogger(__name__)

CANARY_CATEGORY = "idle:canary"


@dataclasses.dataclass(frozen=True)
class CanaryResult:
    cases: dict[str, bool] = dataclasses.field(default_factory=dict)
    passed: bool = True
    preempted: bool = False


async def _active_amendment_texts(session: AsyncSession) -> list[str]:
    result = await session.execute(
        select(PersonaAmendment.text)
        .where(PersonaAmendment.status == "active")
        .order_by(PersonaAmendment.id)
    )
    return [row[0] for row in result.all()]


async def run_canary(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: Clock,
    *,
    run_id: int,
    started_at: datetime.datetime,
    timezone: str,
    job_id: int | None = None,
    persona_provider: LLMProvider | None = None,
    judge_provider: LLMProvider | None = None,
) -> CanaryResult:
    """The `canary` idle kind body (plan section 6.7), called by
    app/core/idle/runner.py.

    `persona_provider`/`judge_provider` let a test inject
    `FakeLLMProvider`s -- same shape as `eval.trial.run_blocking_subset`
    itself; production leaves both None.
    """
    from app.core.idle.runner import RunContext, is_preempted
    from app.db.jobs import touch_job_lock
    from eval.trial import run_blocking_subset

    async with session_factory() as session:
        amendments = await _active_amendment_texts(session)

    async def _on_case_done() -> None:
        if job_id is not None:
            async with session_factory() as session:
                await touch_job_lock(session, job_id)

    trial = await run_blocking_subset(
        settings,
        clock=clock,
        amendments=amendments,
        on_case_done=_on_case_done,
        persona_provider=persona_provider,
        judge_provider=judge_provider,
    )

    async with session_factory() as session:
        ctx = RunContext(
            session=session, settings=settings, clock=clock, run_id=run_id,
            kind="canary", started_at=started_at, timezone=timezone,
        )
        session.add(
            SpendLedger(
                local_date=clock_module.local_date(clock, timezone),
                category=f"idle:{ctx.kind}",
                model=settings.LLM_MODEL_JUDGE,
                tokens_in=0,
                tokens_cached=0,
                tokens_out=0,
                usd_cost=trial.usd_cost,
            )
        )
        await session.commit()

        if await is_preempted(session, clock, started_at):
            return CanaryResult(cases={}, passed=True, preempted=True)

    logger.info(
        "idle canary run done",
        extra={"run_id": run_id, "passed": trial.passed, "count": len(trial.cases)},
    )
    return CanaryResult(cases=trial.cases, passed=trial.passed)


__all__ = ["CANARY_CATEGORY", "CanaryResult", "run_canary"]

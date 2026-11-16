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
`run_idle`, lets this module call `touch_job_lock` once per run of a
blocking case the same way `app/core/amendments.py`'s `run_trial` does
(via its own `on_run_done`) -- a canary is the same ~13 cases, each run
`EVAL_RUNS_PER_CASE` times, and needs the same lease extension so a
second worker cannot reclaim and re-run it mid-trial.

**Repeated runs and the history (anchor-eval-reliability-plan.md
sections 3.2-3.4, milestone E1a).** Unlike the trial, the canary runs
every case all `EVAL_RUNS_PER_CASE` times (`stop_early=False`): its job
is the pass rate. A case passes under the same rule as in the trial.
Each case's counts go to the history (app/core/eval_history.py,
`source='canary'`, `source_id` the run id) with the persona and judge
models it ran on, and the summary then carries what the history says
over that same pair: `saturated`, `live` and `dead` case ids (at least
`EVAL_SIGNAL_MIN_RUNS` runs each). `CanaryResult.summary()` is what
`idle_run.summary` holds: ids, counts and item names only.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core import clock as clock_module
from app.core import eval_history
from app.core.clock import Clock
from app.db.models import PersonaAmendment, SpendLedger
from app.llm import models as llm_models
from app.llm.provider import LLMProvider

logger = logging.getLogger(__name__)

CANARY_CATEGORY = "idle:canary"


@dataclasses.dataclass(frozen=True)
class CanaryResult:
    cases: dict[str, bool] = dataclasses.field(default_factory=dict)
    passed: bool = True
    preempted: bool = False
    # E1a: per-case run counts (eval.trial.RunCounts.as_dict()), the
    # history's three classes over the current model pair, and the
    # advisory items the run was given (plan section 5.4, from /judge, E1c).
    runs: dict[str, dict[str, int]] = dataclasses.field(default_factory=dict)
    signal: eval_history.Signal = eval_history.Signal()
    advisory: tuple[str, ...] = ()

    def summary(self) -> dict:
        """`idle_run.summary` for this run (module docstring)."""
        return {
            "cases": dict(self.cases),
            "passed": self.passed,
            "runs": dict(self.runs),
            "advisory": list(self.advisory),
            **self.signal.as_summary(),
        }


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
    advisory_items: frozenset[str] = frozenset(),
) -> CanaryResult:
    """The `canary` idle kind body (plan section 6.7), called by
    app/core/idle/runner.py.

    `persona_provider`/`judge_provider` let a test inject
    `FakeLLMProvider`s -- same shape as `eval.trial.run_blocking_subset`
    itself; production leaves both None. `advisory_items` (plan section
    5.4) reach every judge call; app/core/idle/runner.py computes them
    from the live database (app/core/judge_calibration.py's
    `unreliable_items`, E1c) before the canary starts.
    """
    from app.core.idle.runner import RunContext, is_preempted
    from app.db.jobs import touch_job_lock
    from eval.trial import run_blocking_subset

    async with session_factory() as session:
        amendments = await _active_amendment_texts(session)

    # The ledger row's label, read before the trial: the judge the
    # trial's providers resolve to (eval/trial.py binds them to the
    # `judge` and `persona` roles), not env's LLM_MODEL_JUDGE.
    judge_model = llm_models.effective_model(settings, "judge")
    persona_model = llm_models.effective_model(settings, "persona")

    async def _on_run_done() -> None:
        if job_id is not None:
            async with session_factory() as session:
                await touch_job_lock(session, job_id)

    trial = await run_blocking_subset(
        settings,
        clock=clock,
        amendments=amendments,
        on_run_done=_on_run_done,
        persona_provider=persona_provider,
        judge_provider=judge_provider,
        stop_early=False,
        advisory_items=frozenset(advisory_items),
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
                model=judge_model,
                tokens_in=0,
                tokens_cached=0,
                tokens_out=0,
                usd_cost=trial.usd_cost,
            )
        )
        # The runs happened whatever preemption says next: their counts
        # belong in the history either way.
        await eval_history.record(
            session,
            clock=clock,
            source=eval_history.CANARY,
            source_id=run_id,
            runs=trial.runs,
            persona_model=persona_model,
            judge_model=judge_model,
        )
        await session.commit()

        if await is_preempted(session, clock, started_at):
            return CanaryResult(cases={}, passed=True, preempted=True)

        signal = await eval_history.signal(
            session,
            persona_model=persona_model,
            judge_model=judge_model,
            min_runs=settings.EVAL_SIGNAL_MIN_RUNS,
        )

    logger.info(
        "idle canary run done",
        extra={"run_id": run_id, "passed": trial.passed, "count": len(trial.cases)},
    )
    return CanaryResult(
        cases=trial.cases,
        passed=trial.passed,
        runs=trial.runs,
        signal=signal,
        advisory=trial.advisory,
    )


__all__ = ["CANARY_CATEGORY", "CanaryResult", "run_canary"]

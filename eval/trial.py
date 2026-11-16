"""Runs the blocking eval subset for `amendment_trial` (phase-5 plan
section 9; milestone 5d).

Reuses `eval.run.run_case`, `eval.scenario` and `eval.cases.load_all` --
the same production-prompt path a manual `eval.run.py` invocation takes
-- rather than re-deriving a lookalike, for the same reason
eval/scenario.py's own docstring gives: an amendment_trial that built
its own approximate prompt could pass happily while the persona the
amendment actually ships against never saw the same thing.

**Only the blocking subset runs** (`case.blocking`), and `scenario.seed`
gets the candidate amendment (plus every other currently-active one) as
its `amendments` override, so `build_messages()` actually renders
"## Поправки" with the change in place -- exactly what a live prompt
would carry if the amendment goes active.

**Never touches the live database.** Every call here runs against a
throwaway database from `eval.db.throwaway_sessionmaker(admin_url=...)`,
created fresh and dropped when this function returns -- the same
guarantee a manual eval run gives. `app/core/amendments.py`'s own
docstring calls this out as non-negotiable, and it is why this module
exists as a thin, separate wrapper rather than folding the loop into
`amendments.py` itself: this file may reach into `eval/`, which
`app/core/amendments.py` (an autonomy module, checked by
tests/test_autonomy_isolation.py) is not otherwise expected to.

**Repeated runs and the pass rule (anchor-eval-reliability-plan.md
sections 3.2 and 3.3, milestone E1a).** Each blocking case runs up to
`EVAL_RUNS_PER_CASE` times, and each run ends in one of
`eval.run.RUN_STATUSES`. A case passes when no run is `check_failed`
and at least `EVAL_PASS_RUNS` runs are `passed` (`case_passes`): a
deterministic check that fails found a real bad reply however rarely
the model writes it, while a judge's fail may be the judge's noise.
With `stop_early` (the trial) a case stops as soon as it is decided
(`decided`): a check failed, enough passes, or the threshold out of
reach. Without it (the canary) every run happens, because the canary's
job is the rate. `on_run_done` is awaited after every run, so the
caller's job lease is refreshed per run, not per case. With both
settings at 1 this is exactly one run per case, as before E1a.

`TrialResult.cases` stays `dict[str, bool]`; `TrialResult.runs` adds
the per-case counts (`RunCounts.as_dict()`: integers only). Nothing
here keeps a reply, a prompt or the judge's words.

`advisory_items` (plan section 5.4, filled by E1c) are scored and
reported and do not decide whether a run passed.
"""

from __future__ import annotations

import dataclasses
import os
import urllib.parse

from app.config import Settings
from app.core.clock import Clock
from eval.cases import load_all
from eval.db import throwaway_sessionmaker
from eval.run import CHECK_FAILED, JUDGE_FAILED, PASSED, UNUSABLE, run_case


@dataclasses.dataclass(frozen=True)
class RunCounts:
    """How the runs of one case ended (plan section 3.3). Integers only."""

    runs: int = 0
    passed: int = 0
    check_failed: int = 0
    judge_failed: int = 0
    unusable: int = 0

    def add(self, status: str) -> RunCounts:
        """These counts with one more run that ended in `status`."""
        if status not in (PASSED, CHECK_FAILED, JUDGE_FAILED, UNUSABLE):
            raise ValueError(f"unknown run status {status!r}")
        return dataclasses.replace(
            self, runs=self.runs + 1, **{status: getattr(self, status) + 1}
        )

    def as_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)


def case_passes(counts: RunCounts, pass_runs: int) -> bool:
    """Plan section 3.2: no run `check_failed`, and at least `pass_runs`
    runs `passed`."""
    return counts.check_failed == 0 and counts.passed >= pass_runs


def decided(counts: RunCounts, runs_per_case: int, pass_runs: int) -> bool:
    """True when more runs cannot change what the trial does with this
    case (plan section 3.2): a check failed, enough runs passed, or so
    few are left that the threshold is out of reach. Running out of runs
    decides too."""
    if counts.runs >= runs_per_case or counts.check_failed > 0:
        return True
    if counts.passed >= pass_runs:
        return True
    return counts.passed + (runs_per_case - counts.runs) < pass_runs


@dataclasses.dataclass(frozen=True)
class TrialResult:
    """Pass/fail per blocking case and the counts of its runs, never
    model text -- see app/core/amendments.py's own docstring on why.

    `runs` maps a case id to `RunCounts.as_dict()`; `advisory` is the
    sorted advisory item names the run was given. Both default empty so
    a result built the pre-E1a way still reads."""

    cases: dict[str, bool]
    passed: bool
    usd_cost: float
    runs: dict[str, dict[str, int]] = dataclasses.field(default_factory=dict)
    advisory: tuple[str, ...] = ()


def _resolve_admin_url(settings: Settings) -> str:
    """`ANCHOR_ADMIN_DATABASE_URL` if set, else `DATABASE_URL` with its
    database name replaced by `postgres` -- the implementation plan's
    own rule for where the job's throwaway database gets created.
    `settings.DATABASE_URL` is already the asyncpg-scheme URL
    (app/config.py's own validator rewrites `postgresql://` on load),
    which `eval.db`'s helpers accept unchanged.
    """
    override = os.environ.get("ANCHOR_ADMIN_DATABASE_URL")
    if override:
        return override
    if not settings.DATABASE_URL:
        # Neither is set (a bare local `pytest`): the same local default
        # eval.db and tests/conftest.py use, instead of an empty string
        # SQLAlchemy cannot parse.
        from eval.db import REPO_ADMIN_URL

        return REPO_ADMIN_URL
    parsed = urllib.parse.urlsplit(settings.DATABASE_URL)
    return urllib.parse.urlunsplit(parsed._replace(path="/postgres"))


async def run_blocking_subset(
    settings: Settings,
    *,
    clock: Clock,
    amendments: list[str],
    on_run_done=None,
    persona_provider=None,
    judge_provider=None,
    stop_early: bool = True,
    advisory_items: frozenset[str] = frozenset(),
) -> TrialResult:
    """Run every blocking case with `amendments` seeded active, against a
    fresh throwaway database. Providers: persona is `LLM_MODEL`, judge is
    `LLM_MODEL_JUDGE`, strictly -- the caller (`app/core/amendments.py`'s
    `run_trial`) has already refused to reach this function at all when
    the judge is not independent.

    In the bot (the amendment trial and the idle canary are its only
    callers) both are bound to their roles, `persona` and `judge`, on
    the resolver app/main.py installed (app/llm/models.py's
    `resolver_kwargs`), so the trial grades the persona model the chat
    really runs on, with the judge the menu chose; the independence
    check its callers make is on those same effective models. With no
    resolver installed the pair is pinned to env, as before.

    Each case runs up to `EVAL_RUNS_PER_CASE` times under the pass rule
    (module docstring); `stop_early` is the trial's early stop, off for
    the canary. `on_run_done` is awaited after every run (the lease).
    `advisory_items` reach every judge call (`eval.run.run_case`).

    `persona_provider`/`judge_provider` let a test inject
    `FakeLLMProvider`s and exercise the real throwaway-database path (the
    property that actually matters) with no network call and no
    OpenRouter key -- production leaves both None, which builds the real
    `OpenRouterProvider` pair exactly as before.
    """
    cases = [case for case in load_all() if case.blocking]
    runs_per_case = settings.EVAL_RUNS_PER_CASE
    pass_runs = settings.EVAL_PASS_RUNS
    advisory = frozenset(advisory_items)

    client = None
    if persona_provider is None or judge_provider is None:
        from app.llm import models as llm_models
        from app.llm.openrouter import OpenRouterProvider, build_client

        client = build_client(settings.OPENROUTER_API_KEY)
        if persona_provider is None:
            persona_provider = OpenRouterProvider(
                api_key=settings.OPENROUTER_API_KEY,
                model=settings.LLM_MODEL,
                max_tokens=settings.LLM_MAX_TOKENS,
                temperature=settings.LLM_TEMPERATURE,
                data_collection=settings.LLM_DATA_COLLECTION,
                client=client,
                **llm_models.resolver_kwargs("persona"),
            )
        if judge_provider is None:
            judge_provider = OpenRouterProvider(
                api_key=settings.OPENROUTER_API_KEY,
                model=settings.LLM_MODEL_JUDGE,
                max_tokens=settings.LLM_CHEAP_MAX_TOKENS,
                temperature=settings.LLM_CHEAP_TEMPERATURE,
                data_collection=settings.LLM_DATA_COLLECTION,
                client=client,
                structured_outputs=settings.LLM_STRUCTURED_OUTPUTS,
                **llm_models.resolver_kwargs("judge"),
            )

    results: dict[str, bool] = {}
    counts: dict[str, dict[str, int]] = {}
    total_cost = 0.0
    try:
        async with throwaway_sessionmaker(admin_url=_resolve_admin_url(settings)) as sessionmaker:
            for case in cases:
                tally = RunCounts()
                while tally.runs < runs_per_case:
                    outcome = await run_case(
                        sessionmaker,
                        case,
                        settings,
                        clock,
                        persona_provider,
                        judge_provider,
                        dry_run=False,
                        amendments=amendments,
                        advisory_items=advisory,
                    )
                    tally = tally.add(outcome.status)
                    total_cost += outcome.usd_cost
                    if on_run_done is not None:
                        await on_run_done()
                    if stop_early and decided(tally, runs_per_case, pass_runs):
                        break
                results[case.id] = case_passes(tally, pass_runs)
                counts[case.id] = tally.as_dict()
    finally:
        if client is not None:
            await client.close()

    return TrialResult(
        cases=results,
        passed=all(results.values()),
        usd_cost=total_cost,
        runs=counts,
        advisory=tuple(sorted(advisory)),
    )


__all__ = ["RunCounts", "TrialResult", "case_passes", "decided", "run_blocking_subset"]

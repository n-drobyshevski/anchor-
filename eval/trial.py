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
"""

from __future__ import annotations

import dataclasses
import os
import urllib.parse

from app.config import Settings
from app.core.clock import Clock
from eval.cases import load_all
from eval.db import throwaway_sessionmaker
from eval.run import run_case


@dataclasses.dataclass(frozen=True)
class TrialResult:
    """Pass/fail per blocking case, never model text -- see
    app/core/amendments.py's own docstring on why."""

    cases: dict[str, bool]
    passed: bool
    usd_cost: float


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
    on_case_done=None,
    persona_provider=None,
    judge_provider=None,
) -> TrialResult:
    """Run every blocking case with `amendments` seeded active, against a
    fresh throwaway database. Providers: persona is `LLM_MODEL`, judge is
    `LLM_MODEL_JUDGE`, strictly -- the caller (`app/core/amendments.py`'s
    `run_trial`) has already refused to reach this function at all when
    the judge is not independent.

    `persona_provider`/`judge_provider` let a test inject
    `FakeLLMProvider`s and exercise the real throwaway-database path (the
    property that actually matters) with no network call and no
    OpenRouter key -- production leaves both None, which builds the real
    `OpenRouterProvider` pair exactly as before.
    """
    cases = [case for case in load_all() if case.blocking]

    client = None
    if persona_provider is None or judge_provider is None:
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
            )

    results: dict[str, bool] = {}
    total_cost = 0.0
    try:
        async with throwaway_sessionmaker(admin_url=_resolve_admin_url(settings)) as sessionmaker:
            for case in cases:
                outcome = await run_case(
                    sessionmaker,
                    case,
                    settings,
                    clock,
                    persona_provider,
                    judge_provider,
                    dry_run=False,
                    amendments=amendments,
                )
                results[case.id] = outcome.passed
                total_cost += outcome.usd_cost
                if on_case_done is not None:
                    await on_case_done()
    finally:
        if client is not None:
            await client.close()

    return TrialResult(cases=results, passed=all(results.values()), usd_cost=total_cost)


__all__ = ["TrialResult", "run_blocking_subset"]

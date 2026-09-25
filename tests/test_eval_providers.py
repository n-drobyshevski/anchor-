"""The real provider construction in eval/ -- the branch every other
eval test skips by injecting FakeLLMProviders.

eval/run.py's `_providers` and eval/trial.py's `run_blocking_subset`
both passed `settings.LLM_WEB_SEARCH_MAX_RESULTS`, a setting milestone
4a removed. Every real eval run, every amendment trial and the weekly
idle canary died with AttributeError before their first model call
(the canary's `idle run failed event=AttributeError` in production on
2026-09-23). Building an OpenRouterProvider makes no network call, so
these tests build them for real.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.llm.openrouter import OpenRouterProvider


async def test_eval_run_builds_its_real_providers():
    from eval.run import _providers

    main, judge, judge_model, client = _providers(Settings(OPENROUTER_API_KEY="sk-test"))
    try:
        assert isinstance(main, OpenRouterProvider)
        assert isinstance(judge, OpenRouterProvider)
        assert judge_model == "openai/gpt-4.1-nano"
    finally:
        await client.close()


class _Reached(Exception):
    pass


async def test_trial_builds_its_real_providers_before_the_throwaway_db(monkeypatch):
    import eval.trial as trial

    def _stop(*args, **kwargs):
        raise _Reached

    # Past provider construction means construction worked.
    monkeypatch.setattr(trial, "throwaway_sessionmaker", _stop)

    with pytest.raises(_Reached):
        await trial.run_blocking_subset(
            Settings(OPENROUTER_API_KEY="sk-test"), clock=None, amendments=[]
        )

"""Runtime model choice, wired: every provider the bot builds for itself
resolves its model through the installed `ModelResolver`
(app/llm/models.py), and nothing changes when none is installed -- the
state every offline entry point (eval/run.py, eval/welfare.py,
scripts/smoke.py) and every older test runs in.

No network: providers are built on a fake client and only asked which
model they would send (`.model`), which is what `complete()` sends.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from app import main as main_module
from app.config import Settings
from app.core import welfare, welfare_shadow
from app.core.idle import critique, lens_garden
from app.core.idle.gate import config_from_settings
from app.db.models import WelfareShadow
from app.llm import models as llm_models
from app.llm.models import ModelResolver
from app.llm.provider import ChoiceAnswer, DecisionResponse, LLMUsage
from app.research import suggest

ENV = {
    "LLM_MODEL": "vendor/persona-env",
    "LLM_MODEL_CHEAP": "vendor/cheap-env",
    "LLM_MODEL_SAFETY": "vendor/safety-env",
    "LLM_MODEL_JUDGE": "vendor/judge-env",
    "OPENROUTER_API_KEY": "test-key-not-real",
}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**ENV, **overrides})


@pytest.fixture()
def installed(sessionmaker):
    """A loaded resolver, installed as app/main.py installs it, and
    uninstalled after the test so no other test resolves through it."""

    async def _install(settings: Settings) -> ModelResolver:
        resolver = await ModelResolver.create(settings, sessionmaker)
        llm_models.install(resolver)
        return resolver

    yield _install
    llm_models.install(None)


@pytest.fixture(autouse=True)
def _nothing_installed_before():
    assert llm_models.installed() is None
    yield
    llm_models.install(None)


# --- no resolver installed: env, exactly as before ----------------------------


def test_without_a_resolver_every_binding_is_empty():
    assert all(llm_models.resolver_kwargs(role) == {} for role in llm_models.ROLES)
    settings = _settings()
    assert llm_models.effective_model(settings, "judge") == "vendor/judge-env"
    assert llm_models.effective_model(settings, "garden") == "vendor/safety-env"
    assert lens_garden.build_garden_provider(settings, object()).model == "vendor/safety-env"
    assert suggest.build_suggest_provider(settings, object()).model == "vendor/safety-env"
    assert critique._build_judge_provider(settings, object()).model == "vendor/judge-env"


def test_without_a_resolver_the_decider_is_built_only_for_an_env_model():
    assert main_module.build_welfare_decider(_settings(), object()) is None
    decider = main_module.build_welfare_decider(
        _settings(WELFARE_SHADOW_MODEL="typesafe/jev-1.14"), object()
    )
    assert decider is not None and decider.model == "typesafe/jev-1.14"


async def test_build_providers_without_a_resolver_stays_on_env():
    provider, cheap, safety, client = main_module.build_providers(_settings())
    try:
        assert (provider.model, cheap.model, safety.model) == (
            "vendor/persona-env", "vendor/cheap-env", "vendor/safety-env"
        )
    finally:
        await client.close()


# --- installed: every call site follows the menu -----------------------------


async def test_build_providers_follow_their_roles(installed):
    resolver = await installed(_settings())
    provider, cheap, safety, client = main_module.build_providers(_settings())
    try:
        await resolver.set("persona", "vendor/persona-picked")
        await resolver.set("cheap", "vendor/cheap-picked")
        await resolver.set("safety", "vendor/safety-picked")
        assert provider.model == "vendor/persona-picked"
        assert cheap.model == "vendor/cheap-picked"
        assert safety.model == "vendor/safety-picked"
        # A wider-cap sibling (distill) keeps following the role.
        assert safety.with_max_tokens(4000).model == "vendor/safety-picked"
        await resolver.reset("cheap")
        assert cheap.model == "vendor/cheap-env"
    finally:
        await client.close()


async def test_judge_garden_and_suggest_providers_follow_their_roles(installed):
    settings = _settings()
    resolver = await installed(settings)
    judge = critique._build_judge_provider(settings, object())
    garden = lens_garden.build_garden_provider(settings, object())
    suggester = suggest.build_suggest_provider(settings, object())

    await resolver.set("safety", "vendor/safety-picked")
    # Both follow a safety switch while they have no row of their own.
    assert garden.model == "vendor/safety-picked"
    assert suggester.model == "vendor/safety-picked"

    await resolver.set("garden", "vendor/garden-picked")
    await resolver.set("suggest", "vendor/suggest-picked")
    await resolver.set("judge", "vendor/judge-picked")
    assert garden.model == "vendor/garden-picked"
    assert suggester.model == "vendor/suggest-picked"
    assert judge.model == "vendor/judge-picked"
    assert llm_models.effective_model(settings, "judge") == "vendor/judge-picked"


async def test_the_independence_check_uses_the_effective_models(installed):
    settings = _settings()
    resolver = await installed(settings)
    assert llm_models.independent_judge(settings)
    assert config_from_settings(settings).independent_judge

    # The judge switched onto the persona's model: refused at run time,
    # whatever env says.
    await resolver.set("judge", "vendor/persona-env")
    assert not llm_models.independent_judge(settings)
    assert not config_from_settings(settings).independent_judge

    # And the persona switched away again makes them independent.
    await resolver.set("persona", "vendor/persona-picked")
    assert llm_models.independent_judge(settings)

    # Env alone that names the same model for both is still refused.
    await resolver.reset("judge")
    await resolver.reset("persona")
    same = _settings(LLM_MODEL_JUDGE="vendor/persona-env")
    llm_models.install(await ModelResolver.create(same, resolver._sessionmaker))
    assert not config_from_settings(same).independent_judge


def _gate_facts():
    import decimal

    from app.core.idle.gate import IdleFacts

    return IdleFacts(
        persona_active=True,
        local_now=datetime.datetime(2026, 9, 23, 12, 0, tzinfo=datetime.UTC),
        critique_has_new_replies=True,
        daily_usd_cap=decimal.Decimal("1.00"),
    )


async def test_the_idle_gate_refuses_critique_and_canary_on_a_switched_judge(installed):
    """The kind rule reads `IdleConfig.independent_judge`, which is now
    the effective pair: a judge switched onto the persona's model stops
    both the critique and the canary at the gate, whatever env says."""
    from app.core.idle.gate import NO_INDEPENDENT_JUDGE, idle_gate

    settings = _settings(CANARY_DOW=3)
    resolver = await installed(settings)
    facts = _gate_facts()
    now = facts.local_now
    assert idle_gate("critique", facts, now, config_from_settings(settings)) == (True, "ok")
    assert idle_gate("canary", facts, now, config_from_settings(settings)) == (True, "ok")

    await resolver.set("judge", "vendor/persona-env")
    config = config_from_settings(settings)
    assert idle_gate("critique", facts, now, config) == (False, NO_INDEPENDENT_JUDGE)
    assert idle_gate("canary", facts, now, config) == (False, NO_INDEPENDENT_JUDGE)

    # And the persona moving away lifts it again.
    await resolver.set("persona", "vendor/persona-picked")
    assert idle_gate("critique", facts, now, config_from_settings(settings)) == (True, "ok")


async def test_a_judge_picked_in_the_menu_opens_the_gate_env_shut(installed):
    from app.core.idle.gate import NO_INDEPENDENT_JUDGE, idle_gate

    settings = _settings(LLM_MODEL_JUDGE="")
    resolver = await installed(settings)
    facts = _gate_facts()
    assert idle_gate("critique", facts, facts.local_now, config_from_settings(settings)) == (
        False, NO_INDEPENDENT_JUDGE
    )
    await resolver.set("judge", "vendor/judge-picked")
    assert idle_gate("critique", facts, facts.local_now, config_from_settings(settings)) == (True, "ok")
    # Switched off again, it is shut again.
    await resolver.reset("judge")
    assert not config_from_settings(settings).independent_judge


async def test_the_critique_ledger_row_names_the_judge_that_graded(sessionmaker, installed):
    from conftest import FakeLLMProvider
    from test_idle_critique import (
        GOOD_JUDGE_TEXT,
        _clock,
        _seed_persona_replies,
        _seed_state,
    )

    from app.core.idle.critique import run_critique
    from app.db.models import IdleRun, SpendLedger

    settings = _settings(CRITIQUE_SAMPLE=5)
    resolver = await installed(settings)
    await resolver.set("judge", "vendor/judge-picked")
    clock = _clock()
    await _seed_state(sessionmaker)
    await _seed_persona_replies(sessionmaker, 2)
    async with sessionmaker() as session:
        run = IdleRun(kind="critique", local_date=clock.now_utc().date(), status="queued")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    await run_critique(
        sessionmaker, settings, clock,
        run_id=run_id, started_at=clock.now_utc(), timezone="Europe/Paris",
        judge_provider=FakeLLMProvider(text=GOOD_JUDGE_TEXT),
    )
    async with sessionmaker() as session:
        rows = (await session.execute(select(SpendLedger))).scalars().all()
    assert [row.model for row in rows if row.category == "idle:critique"] == ["vendor/judge-picked"]


async def test_the_canary_ledger_row_names_the_judge_that_graded(sessionmaker, installed, monkeypatch):
    from test_idle_canary import _clock, _seed_state

    from app.core.idle.canary import run_canary
    from app.db.models import IdleRun, SpendLedger
    from eval.trial import TrialResult

    settings = _settings()
    resolver = await installed(settings)
    await resolver.set("judge", "vendor/judge-picked")
    clock = _clock()
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        run = IdleRun(kind="canary", local_date=clock.now_utc().date(), status="queued")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    async def _fake(settings, *, clock, amendments, on_case_done=None, **kwargs):
        return TrialResult(cases={"01": True}, passed=True, usd_cost=0.01)

    monkeypatch.setattr("eval.trial.run_blocking_subset", _fake)
    await run_canary(
        sessionmaker, settings, clock,
        run_id=run_id, started_at=clock.now_utc(), timezone="Europe/Paris",
    )
    async with sessionmaker() as session:
        rows = (await session.execute(select(SpendLedger))).scalars().all()
    assert [row.model for row in rows if row.category == "idle:canary"] == ["vendor/judge-picked"]


class _Captured(Exception):
    pass


async def _trial_providers(monkeypatch, settings):
    """The persona and judge providers eval/trial.py's real branch builds,
    caught at the first `run_case` -- no database, no model call (building
    an OpenRouterProvider makes none)."""
    import contextlib

    from eval import trial

    @contextlib.asynccontextmanager
    async def _no_db(**kwargs):
        yield None

    captured = {}

    async def _capture(sessionmaker, case, settings, clock, persona, judge, **kwargs):
        captured.update(persona=persona, judge=judge)
        raise _Captured

    monkeypatch.setattr(trial, "throwaway_sessionmaker", _no_db)
    monkeypatch.setattr(trial, "run_case", _capture)
    with pytest.raises(_Captured):
        await trial.run_blocking_subset(settings, clock=None, amendments=[])
    return captured["persona"], captured["judge"]


async def test_the_trial_grades_the_picked_persona_with_the_picked_judge(installed, monkeypatch):
    """The ledger and the independence check name the effective models;
    the providers the trial grades with must be those same models, or a
    judge picked away from an env clash would still grade the persona
    with itself."""
    settings = _settings(LLM_MODEL_JUDGE="vendor/persona-env")
    resolver = await installed(settings)
    await resolver.set("persona", "vendor/persona-picked")
    await resolver.set("judge", "vendor/judge-picked")
    persona, judge = await _trial_providers(monkeypatch, settings)
    assert persona.model == "vendor/persona-picked"
    assert judge.model == "vendor/judge-picked"


async def test_the_trial_without_a_resolver_stays_on_env(monkeypatch):
    persona, judge = await _trial_providers(monkeypatch, _settings())
    assert persona.model == "vendor/persona-env"
    assert judge.model == "vendor/judge-env"


# --- the welfare shadow -------------------------------------------------------


async def test_the_decider_is_always_built_and_skips_when_off(installed):
    resolver = await installed(_settings())
    decider = main_module.build_welfare_decider(_settings(), object())
    assert decider is not None
    assert not welfare_shadow.active(decider)

    await resolver.set("welfare_shadow", "typesafe/jev-1.14")
    assert welfare_shadow.active(decider) and decider.model == "typesafe/jev-1.14"

    await resolver.set("welfare_shadow", "")
    assert not welfare_shadow.active(decider)
    assert not welfare_shadow.active(None)


def test_a_decider_that_does_not_say_counts_as_on():
    class Bare:
        async def decide(self, state, questions):  # pragma: no cover - never called
            raise AssertionError

    assert welfare_shadow.active(Bare())


async def test_the_row_records_the_model_that_answered(sessionmaker, frozen_clock):
    settings = _settings(WELFARE_SHADOW_MODEL="typesafe/jev-1.14")
    clock = frozen_clock(2026, 10, 25, 12, 0, tz="Europe/Paris")
    response = DecisionResponse(
        answers={
            welfare_shadow.QUESTION: ChoiceAnswer(
                choice=welfare.NONE, confidence=0.9, probabilities={welfare.REAL: 0.02}
            )
        },
        usage=LLMUsage(input_tokens=10, cached_tokens=0, output_tokens=1, cost_usd=None),
        model="vendor/other-decider",
    )
    primary = welfare.Classification(
        verdict=welfare.Verdict(welfare.NONE, 0.9),
        response=None,
        outcome=welfare.OK,
    )
    answered = welfare_shadow.Shadow(
        welfare.OK, welfare.NONE, 0.9, 0.02, 120, response, "vendor/other-decider"
    )
    timed_out = welfare_shadow.Shadow(welfare.TIMEOUT, None, None, None, 2000, None, "vendor/asked")
    unknown = welfare_shadow.Shadow(welfare.ERROR, None, None, None, 5, None)
    for shadow in (answered, timed_out, unknown):
        await welfare_shadow.record(
            sessionmaker, settings, clock=clock, timezone="Europe/Paris",
            primary=primary, primary_ms=100, shadow=shadow,
        )

    async with sessionmaker() as session:
        rows = (await session.execute(select(WelfareShadow).order_by(WelfareShadow.id))).scalars().all()
    assert [row.shadow_model for row in rows] == [
        "vendor/other-decider",
        "vendor/asked",
        # A decider that reports nothing: env, as every row was before.
        "typesafe/jev-1.14",
    ]

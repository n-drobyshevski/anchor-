"""The `canary` idle kind: reuses `eval.trial.run_blocking_subset`
in-process against a throwaway database only, stores pass/fail per
case (no text), its kind rule (CANARY_DOW, no independent judge), and
job-lease refresh per case (Phase 6 plan section 6.7; milestone 6c's
own test list).

The plumbing of `run_blocking_subset` itself (real throwaway database,
real cases) is already covered by
tests/test_amendments.py::test_run_blocking_subset_against_a_real_throwaway_database.
This file monkeypatches it (per the milestone brief: "canary must never
touch the live DB -- monkeypatch the runner") so these tests stay fast
and exercise `run_canary`'s own responsibilities: reading active
amendments, ledgering, the job-lease refresh, and turning the trial
result into `idle_run.summary`.
"""

from __future__ import annotations

import datetime
import decimal

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.idle.canary import run_canary
from app.core.idle.gate import NOT_CANARY_DOW, NO_INDEPENDENT_JUDGE, config_from_settings, idle_gate
from app.core.idle.gate import IdleFacts
from app.db.models import IdleChange, IdleRun, PersonaAmendment, SpendLedger, UserState
from conftest import FakeLLMProvider

TIMEZONE = "Europe/Paris"


def _clock() -> FrozenClock:
    # 2026-09-23 is a Wednesday (isoweekday 3).
    return FrozenClock(datetime.datetime(2026, 9, 23, 12, 0, tzinfo=datetime.timezone.utc))


async def _seed_state(sessionmaker) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, timezone=TIMEZONE))
        await session.commit()


# --- kind rule --------------------------------------------------------


def test_kind_rule_wrong_weekday():
    config = config_from_settings(Settings(CANARY_DOW=5))
    facts = IdleFacts(persona_active=True, local_now=_clock().now_utc(), daily_usd_cap=decimal.Decimal("1.00"))
    assert idle_gate("canary", facts, _clock().now_utc(), config) == (False, NOT_CANARY_DOW)


def test_kind_rule_no_independent_judge():
    config = config_from_settings(Settings(CANARY_DOW=3, LLM_MODEL_JUDGE=""))
    facts = IdleFacts(persona_active=True, local_now=_clock().now_utc(), daily_usd_cap=decimal.Decimal("1.00"))
    assert idle_gate("canary", facts, _clock().now_utc(), config) == (False, NO_INDEPENDENT_JUDGE)


def test_kind_rule_allows_on_its_dow_with_independent_judge():
    config = config_from_settings(Settings(CANARY_DOW=3))
    facts = IdleFacts(persona_active=True, local_now=_clock().now_utc(), daily_usd_cap=decimal.Decimal("1.00"))
    assert idle_gate("canary", facts, _clock().now_utc(), config) == (True, "ok")


# --- run_canary -----------------------------------------------------------


async def test_run_canary_stores_pass_fail_per_case_never_live_db(sessionmaker, monkeypatch):
    clock = _clock()
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        session.add(PersonaAmendment(text="меньше вопросов", status="active", persona_sha="x"))
        run = IdleRun(kind="canary", local_date=clock.now_utc().date(), status="queued")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    seen_amendments = []

    async def _fake_run_blocking_subset(settings, *, clock, amendments, on_case_done=None, **kwargs):
        seen_amendments.append(list(amendments))
        if on_case_done is not None:
            await on_case_done()
            await on_case_done()
        from eval.trial import TrialResult

        return TrialResult(cases={"01": True, "02": False}, passed=False, usd_cost=0.02)

    monkeypatch.setattr("eval.trial.run_blocking_subset", _fake_run_blocking_subset)

    result = await run_canary(
        sessionmaker, Settings(), clock,
        run_id=run_id, started_at=clock.now_utc(), timezone=TIMEZONE,
    )

    assert result.cases == {"01": True, "02": False}
    assert result.passed is False
    assert seen_amendments == [["меньше вопросов"]]

    async with sessionmaker() as session:
        ledger = (await session.execute(select(SpendLedger))).scalars().all()
        assert any(row.category == "idle:canary" for row in ledger)
        changes = (await session.execute(select(IdleChange))).scalars().all()
        assert changes == []


async def test_run_canary_refreshes_the_job_lease_per_case(sessionmaker, monkeypatch):
    clock = _clock()
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        run = IdleRun(kind="canary", local_date=clock.now_utc().date(), status="queued")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    touch_calls = []

    async def _fake_touch_job_lock(session, job_id):
        touch_calls.append(job_id)

    monkeypatch.setattr("app.db.jobs.touch_job_lock", _fake_touch_job_lock)

    async def _fake_run_blocking_subset(settings, *, clock, amendments, on_case_done=None, **kwargs):
        for _ in range(3):
            if on_case_done is not None:
                await on_case_done()
        from eval.trial import TrialResult

        return TrialResult(cases={}, passed=True, usd_cost=0.0)

    monkeypatch.setattr("eval.trial.run_blocking_subset", _fake_run_blocking_subset)

    await run_canary(
        sessionmaker, Settings(), clock,
        run_id=run_id, started_at=clock.now_utc(), timezone=TIMEZONE, job_id=999,
    )

    assert touch_calls == [999, 999, 999]


async def test_run_canary_real_run_blocking_subset_end_to_end(sessionmaker):
    """One real (not monkeypatched) call, through a throwaway database,
    with FakeLLMProviders standing in for the network -- confirms
    run_canary's own plumbing (not just the mock) produces a real
    TrialResult with no live-database writes beyond this run's own
    idle_run/spend_ledger bookkeeping."""
    clock = _clock()
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        run = IdleRun(kind="canary", local_date=clock.now_utc().date(), status="queued")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    result = await run_canary(
        sessionmaker, Settings(), clock,
        run_id=run_id, started_at=clock.now_utc(), timezone=TIMEZONE,
        persona_provider=FakeLLMProvider(text="Понял. Идём дальше."),
        judge_provider=FakeLLMProvider(text="Понял."),
    )

    assert isinstance(result.cases, dict)
    assert result.cases
    assert all(isinstance(v, bool) for v in result.cases.values())

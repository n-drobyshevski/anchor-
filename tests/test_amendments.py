"""Persona amendments: adopt/reject/revoke, the amendment_trial job, the
independent-judge check, and persona.md's byte-identical invariant
(phase-5 plan sections 3 and 9, milestone 5d).
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core import amendments
from app.core import review
from app.core.clock import FrozenClock
from app.core.prompt import PERSONA_PATH, load_persona
from app.core.scene import Deferred
from app.db.models import PersonaAmendment, ReviewProposal, SpendLedger, UserState, WeeklyReview
from eval.trial import TrialResult
from conftest import FakeLLMProvider

PARIS = "Europe/Paris"


def _settings(**overrides) -> Settings:
    base = {
        "LLM_MODEL": "thedrummer/cydonia-24b-v4.1",
        "LLM_MODEL_CHEAP": "thedrummer/cydonia-24b-v4.1",
        "LLM_MODEL_JUDGE": "openai/gpt-4.1-nano",
        "DAILY_USD_CAP": 1.00,
        "AMENDMENTS_MAX_ACTIVE": 2,
    }
    base.update(overrides)
    return Settings(**base)


def _clock(frozen_clock) -> FrozenClock:
    return frozen_clock(2026, 9, 24, 12, 0, tz=PARIS)


async def _seed_state(sessionmaker) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=1, timezone=PARIS))
        await session.commit()


async def _seed_pending_proposal(sessionmaker, *, text: str = "меньше вопросов") -> int:
    async with sessionmaker() as session:
        review_row = WeeklyReview(
            week_start=datetime.date(2026, 9, 21),
            analysis={"wins": [], "misses": [], "patterns": [], "intentions": [], "proposals": []},
        )
        session.add(review_row)
        await session.commit()
        await session.refresh(review_row)
        proposal = ReviewProposal(review_id=review_row.id, kind="persona_note", text=text)
        session.add(proposal)
        await session.commit()
        return proposal.id


# --- adopt / reject / revoke ------------------------------------------------


async def test_adopt_inserts_a_trial_amendment_and_marks_the_proposal_adopted(
    sessionmaker, frozen_clock
):
    clock = _clock(frozen_clock)
    await _seed_state(sessionmaker)
    proposal_id = await _seed_pending_proposal(sessionmaker)

    async with sessionmaker() as session:
        result = await amendments.adopt(session, _settings(), proposal_id, clock=clock)
        assert result.status == "ok"
        assert result.amendment.status == amendments.TRIAL
        assert result.amendment.text == "меньше вопросов"

        _, sha = load_persona()
        assert result.amendment.persona_sha == sha

        proposal = await session.get(ReviewProposal, proposal_id)
        assert proposal.status == review.ADOPTED


async def test_adopt_refuses_past_the_cap(sessionmaker, frozen_clock):
    clock = _clock(frozen_clock)
    settings = _settings(AMENDMENTS_MAX_ACTIVE=1)
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        session.add(PersonaAmendment(text="уже активна", status=amendments.ACTIVE, persona_sha="x"))
        await session.commit()

    proposal_id = await _seed_pending_proposal(sessionmaker)
    async with sessionmaker() as session:
        result = await amendments.adopt(session, settings, proposal_id, clock=clock)
        assert result.status == "cap"

        # The proposal is untouched -- still answerable once there is room.
        proposal = await session.get(ReviewProposal, proposal_id)
        assert proposal.status == review.PENDING


async def test_adopt_counts_trial_rows_against_the_cap_too(sessionmaker, frozen_clock):
    """Implementation plan's "Adopt": the cap counts `active` plus
    `trial` together."""
    clock = _clock(frozen_clock)
    settings = _settings(AMENDMENTS_MAX_ACTIVE=1)
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        session.add(PersonaAmendment(text="уже в процессе", status=amendments.TRIAL, persona_sha="x"))
        await session.commit()

    proposal_id = await _seed_pending_proposal(sessionmaker)
    async with sessionmaker() as session:
        result = await amendments.adopt(session, settings, proposal_id, clock=clock)
        assert result.status == "cap"


async def test_adopt_is_stale_on_an_already_decided_proposal(sessionmaker, frozen_clock):
    clock = _clock(frozen_clock)
    await _seed_state(sessionmaker)
    proposal_id = await _seed_pending_proposal(sessionmaker)
    async with sessionmaker() as session:
        await review.mark_proposal(session, proposal_id, review.REJECTED, clock=clock)

    async with sessionmaker() as session:
        result = await amendments.adopt(session, _settings(), proposal_id, clock=clock)
        assert result.status == "stale"


async def test_reject_marks_the_proposal_rejected(sessionmaker, frozen_clock):
    clock = _clock(frozen_clock)
    await _seed_state(sessionmaker)
    proposal_id = await _seed_pending_proposal(sessionmaker)
    async with sessionmaker() as session:
        ok = await amendments.reject(session, proposal_id, clock=clock)
        assert ok is True
        proposal = await session.get(ReviewProposal, proposal_id)
        assert proposal.status == review.REJECTED


async def test_revoke_moves_an_active_amendment_to_revoked(sessionmaker, frozen_clock):
    clock = _clock(frozen_clock)
    async with sessionmaker() as session:
        row = PersonaAmendment(text="x", status=amendments.ACTIVE, persona_sha="x")
        session.add(row)
        await session.commit()
        await session.refresh(row)
        amendment_id = row.id

    async with sessionmaker() as session:
        ok = await amendments.revoke(session, amendment_id, clock=clock)
        assert ok is True
        row = await session.get(PersonaAmendment, amendment_id)
        assert row.status == amendments.REVOKED
        assert row.revoked_at is not None


async def test_revoke_refuses_a_non_active_row(sessionmaker, frozen_clock):
    clock = _clock(frozen_clock)
    async with sessionmaker() as session:
        row = PersonaAmendment(text="x", status=amendments.TRIAL, persona_sha="x")
        session.add(row)
        await session.commit()
        await session.refresh(row)
        amendment_id = row.id

    async with sessionmaker() as session:
        ok = await amendments.revoke(session, amendment_id, clock=clock)
        assert ok is False


# --- list_for_display: the persona_sha staleness flag -----------------------


async def test_list_for_display_flags_a_stale_amendment(sessionmaker):
    async with sessionmaker() as session:
        session.add(PersonaAmendment(text="актуальная", status=amendments.ACTIVE, persona_sha=load_persona()[1]))
        session.add(PersonaAmendment(text="устаревшая", status=amendments.ACTIVE, persona_sha="not-the-real-sha"))
        await session.commit()

        rows = await amendments.list_for_display(session)
    by_text = {item.amendment.text: item.stale for item in rows}
    assert by_text["актуальная"] is False
    assert by_text["устаревшая"] is True


# --- run_trial: the independent-judge check, first and strict --------------


async def test_run_trial_fails_with_no_judge_configured_and_touches_nothing(
    sessionmaker, frozen_clock, monkeypatch
):
    clock = _clock(frozen_clock)
    settings = _settings(LLM_MODEL_JUDGE="")
    await _seed_state(sessionmaker)

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("run_blocking_subset must not be called with no independent judge")

    monkeypatch.setattr("eval.trial.run_blocking_subset", _must_not_be_called)

    async with sessionmaker() as session:
        row = PersonaAmendment(text="x", status=amendments.TRIAL, persona_sha="x")
        session.add(row)
        await session.commit()
        await session.refresh(row)
        amendment_id = row.id

    async with sessionmaker() as session:
        outcome = await amendments.run_trial(session, settings, clock=clock, amendment_id=amendment_id)
        assert outcome.status == amendments.FAILED

    async with sessionmaker() as session:
        row = await session.get(PersonaAmendment, amendment_id)
        assert row.status == amendments.FAILED
        assert row.eval_report == {"cases": {}, "reason": amendments.NO_INDEPENDENT_JUDGE}
        # No throwaway database, no API call -- so no ledger row either.
        ledger = (await session.execute(select(SpendLedger))).scalars().all()
        assert ledger == []


async def test_run_trial_fails_when_judge_equals_the_persona_model(
    sessionmaker, frozen_clock, monkeypatch
):
    clock = _clock(frozen_clock)
    settings = _settings(LLM_MODEL_JUDGE="thedrummer/cydonia-24b-v4.1")  # same as LLM_MODEL
    await _seed_state(sessionmaker)

    monkeypatch.setattr(
        "eval.trial.run_blocking_subset",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called")),
    )

    async with sessionmaker() as session:
        row = PersonaAmendment(text="x", status=amendments.TRIAL, persona_sha="x")
        session.add(row)
        await session.commit()
        await session.refresh(row)
        amendment_id = row.id

    async with sessionmaker() as session:
        outcome = await amendments.run_trial(session, settings, clock=clock, amendment_id=amendment_id)
        assert outcome.status == amendments.FAILED

    async with sessionmaker() as session:
        row = await session.get(PersonaAmendment, amendment_id)
        assert row.eval_report["reason"] == amendments.NO_INDEPENDENT_JUDGE


# --- run_trial: pass / fail / env failure, with a fake runner ---------------


async def _seed_trial_amendment(sessionmaker, *, text: str = "меньше вопросов") -> int:
    async with sessionmaker() as session:
        row = PersonaAmendment(text=text, status=amendments.TRIAL, persona_sha="x")
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id


async def test_run_trial_activates_on_a_clean_pass(sessionmaker, frozen_clock, monkeypatch):
    clock = _clock(frozen_clock)
    settings = _settings()
    await _seed_state(sessionmaker)
    amendment_id = await _seed_trial_amendment(sessionmaker)

    async def _fake_run_blocking_subset(*args, on_case_done=None, **kwargs):
        if on_case_done is not None:
            await on_case_done()
            await on_case_done()
        return TrialResult(cases={"04": True, "05": True}, passed=True, usd_cost=0.031)

    monkeypatch.setattr("eval.trial.run_blocking_subset", _fake_run_blocking_subset)

    async with sessionmaker() as session:
        outcome = await amendments.run_trial(session, settings, clock=clock, amendment_id=amendment_id)
        assert outcome.status == amendments.ACTIVE

    async with sessionmaker() as session:
        row = await session.get(PersonaAmendment, amendment_id)
        assert row.status == amendments.ACTIVE
        assert row.activated_at is not None
        assert row.eval_report == {"cases": {"04": True, "05": True}, "reason": None}

        ledger = (await session.execute(select(SpendLedger))).scalars().all()
        assert len(ledger) == 1
        assert ledger[0].category == amendments.AMENDMENT_TRIAL_CATEGORY
        assert abs(float(ledger[0].usd_cost) - 0.031) < 1e-6


async def test_run_trial_fails_on_any_case_failure_with_no_model_text_stored(
    sessionmaker, frozen_clock, monkeypatch
):
    clock = _clock(frozen_clock)
    settings = _settings()
    await _seed_state(sessionmaker)
    amendment_id = await _seed_trial_amendment(sessionmaker)

    async def _fake_run_blocking_subset(*args, **kwargs):
        return TrialResult(cases={"04": True, "22": False}, passed=False, usd_cost=0.04)

    monkeypatch.setattr("eval.trial.run_blocking_subset", _fake_run_blocking_subset)

    async with sessionmaker() as session:
        outcome = await amendments.run_trial(session, settings, clock=clock, amendment_id=amendment_id)
        assert outcome.status == amendments.FAILED

    async with sessionmaker() as session:
        row = await session.get(PersonaAmendment, amendment_id)
        assert row.status == amendments.FAILED
        assert row.eval_report == {
            "cases": {"04": True, "22": False},
            "reason": amendments.CASE_FAILED,
        }
        # Pass/fail only -- no reply text, no prompt text, anywhere in the report.
        report_text = str(row.eval_report)
        assert "меньше вопросов" not in report_text


async def test_run_trial_fails_closed_on_an_unusable_environment(
    sessionmaker, frozen_clock, monkeypatch
):
    clock = _clock(frozen_clock)
    settings = _settings()
    await _seed_state(sessionmaker)
    amendment_id = await _seed_trial_amendment(sessionmaker)

    async def _broken(*args, **kwargs):
        raise RuntimeError("no throwaway database available")

    monkeypatch.setattr("eval.trial.run_blocking_subset", _broken)

    async with sessionmaker() as session:
        outcome = await amendments.run_trial(session, settings, clock=clock, amendment_id=amendment_id)
        assert outcome.status == amendments.FAILED

    async with sessionmaker() as session:
        row = await session.get(PersonaAmendment, amendment_id)
        assert row.eval_report == {"cases": {}, "reason": amendments.TRIAL_ENV_UNAVAILABLE}


async def test_run_trial_is_a_noop_on_a_gone_or_non_trial_amendment(sessionmaker, frozen_clock):
    clock = _clock(frozen_clock)
    settings = _settings()
    await _seed_state(sessionmaker)

    async with sessionmaker() as session:
        outcome = await amendments.run_trial(session, settings, clock=clock, amendment_id=999999)
        assert outcome is None

    async with sessionmaker() as session:
        row = PersonaAmendment(text="x", status=amendments.ACTIVE, persona_sha="x")
        session.add(row)
        await session.commit()
        await session.refresh(row)
        amendment_id = row.id

    async with sessionmaker() as session:
        outcome = await amendments.run_trial(session, settings, clock=clock, amendment_id=amendment_id)
        assert outcome is None


async def test_run_trial_defers_over_the_daily_cap(sessionmaker, frozen_clock, monkeypatch):
    clock = _clock(frozen_clock)
    settings = _settings(DAILY_USD_CAP=0.01)
    await _seed_state(sessionmaker)
    amendment_id = await _seed_trial_amendment(sessionmaker)

    async with sessionmaker() as session:
        session.add(
            SpendLedger(local_date=clock.now_utc().date(), category="chat", usd_cost=1.00)
        )
        await session.commit()

    monkeypatch.setattr(
        "eval.trial.run_blocking_subset",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run over the cap")),
    )

    async with sessionmaker() as session:
        with pytest.raises(Deferred):
            await amendments.run_trial(session, settings, clock=clock, amendment_id=amendment_id)


# --- persona.md is byte-identical across every one of these flows ----------


def _persona_bytes() -> bytes:
    return PERSONA_PATH.read_bytes()


async def test_persona_md_is_byte_identical_through_adopt_trial_pass_revoke(
    sessionmaker, frozen_clock, monkeypatch
):
    before = _persona_bytes()
    clock = _clock(frozen_clock)
    settings = _settings()
    await _seed_state(sessionmaker)
    proposal_id = await _seed_pending_proposal(sessionmaker)

    async def _pass(*args, **kwargs):
        return TrialResult(cases={"04": True}, passed=True, usd_cost=0.01)

    monkeypatch.setattr("eval.trial.run_blocking_subset", _pass)

    async with sessionmaker() as session:
        result = await amendments.adopt(session, settings, proposal_id, clock=clock)
        amendment_id = result.amendment.id

    async with sessionmaker() as session:
        await amendments.run_trial(session, settings, clock=clock, amendment_id=amendment_id)

    async with sessionmaker() as session:
        await amendments.revoke(session, amendment_id, clock=clock)

    assert _persona_bytes() == before


async def test_persona_md_is_byte_identical_through_adopt_trial_fail(
    sessionmaker, frozen_clock, monkeypatch
):
    before = _persona_bytes()
    clock = _clock(frozen_clock)
    settings = _settings()
    await _seed_state(sessionmaker)
    proposal_id = await _seed_pending_proposal(sessionmaker)

    async def _fail(*args, **kwargs):
        return TrialResult(cases={"04": False}, passed=False, usd_cost=0.01)

    monkeypatch.setattr("eval.trial.run_blocking_subset", _fail)

    async with sessionmaker() as session:
        result = await amendments.adopt(session, settings, proposal_id, clock=clock)
        amendment_id = result.amendment.id

    async with sessionmaker() as session:
        await amendments.run_trial(session, settings, clock=clock, amendment_id=amendment_id)

    assert _persona_bytes() == before


async def test_persona_md_is_byte_identical_through_adopt_no_judge(sessionmaker, frozen_clock):
    before = _persona_bytes()
    clock = _clock(frozen_clock)
    settings = _settings(LLM_MODEL_JUDGE="")
    await _seed_state(sessionmaker)
    proposal_id = await _seed_pending_proposal(sessionmaker)

    async with sessionmaker() as session:
        result = await amendments.adopt(session, settings, proposal_id, clock=clock)
        amendment_id = result.amendment.id

    async with sessionmaker() as session:
        await amendments.run_trial(session, settings, clock=clock, amendment_id=amendment_id)

    assert _persona_bytes() == before


async def test_persona_md_is_byte_identical_through_the_cap(sessionmaker, frozen_clock):
    before = _persona_bytes()
    clock = _clock(frozen_clock)
    settings = _settings(AMENDMENTS_MAX_ACTIVE=1)
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        session.add(PersonaAmendment(text="уже активна", status=amendments.ACTIVE, persona_sha="x"))
        await session.commit()
    proposal_id = await _seed_pending_proposal(sessionmaker)

    async with sessionmaker() as session:
        result = await amendments.adopt(session, settings, proposal_id, clock=clock)
        assert result.status == "cap"

    assert _persona_bytes() == before


# --- one real integration test of eval.trial.run_blocking_subset -----------


async def test_run_blocking_subset_against_a_real_throwaway_database():
    """Exercises the actual throwaway-database path (create, migrate,
    seed, run every blocking case, drop) with `FakeLLMProvider`s standing
    in for the network -- the property this test cares about is that the
    whole plumbing works end to end against a real database, not that
    the canned replies happen to pass every rubric item."""
    from eval.cases import load_all
    from eval.trial import run_blocking_subset
    from app.core.clock import SystemClock

    settings = _settings()
    blocking_ids = {case.id for case in load_all() if case.blocking}

    result = await run_blocking_subset(
        settings,
        clock=SystemClock(),
        amendments=["меньше вопросов"],
        persona_provider=FakeLLMProvider(text="Понял. Идём дальше."),
        judge_provider=FakeLLMProvider(text="Понял."),
    )

    assert set(result.cases) == blocking_ids
    assert all(isinstance(v, bool) for v in result.cases.values())
    assert result.usd_cost >= 0.0

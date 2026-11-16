"""Eval reliability, E1a and E1b (anchor-eval-reliability-plan.md
section 8): the pass rule's table, one run per case being exactly the
pre-E1a behaviour, the trial's early stop against the canary's full
runs, the history and its model-pair filter, saturated/live/dead, the
offline runner's `--runs`, the settings' bounds; vendor parsing and the
independence rule at every caller, and no quote in a log or a stored
row. The judge's own evidence checks are in tests/test_eval_checks.py,
the menu's refusals in tests/test_menu_models.py.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import decimal
import itertools
import logging
import pathlib
import re

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text

from app.config import Settings
from app.core import amendments, eval_history
from app.core.clock import FrozenClock
from app.core.idle.digest import canary_signal_suffix
from app.core.idle.gate import IdleFacts, NO_INDEPENDENT_JUDGE, config_from_settings, idle_gate
from app.db.models import EvalCaseRun, IdleRun, PersonaAmendment, UserState
from app.llm import models as llm_models
from app.tg.amendments import render_trial_result
from eval import judge as judge_module
from eval import run as run_module
from eval import trial as trial_module
from eval.run import CHECK_FAILED, JUDGE_FAILED, PASSED, RUN_STATUSES, UNUSABLE
from eval.trial import RunCounts, TrialResult, case_passes, decided
from conftest import FakeLLMProvider, judge_json

PARIS = "Europe/Paris"
NOW = datetime.datetime(2026, 9, 23, 12, 0, tzinfo=datetime.timezone.utc)  # a Wednesday


def _settings(**overrides) -> Settings:
    base = {
        "LLM_MODEL": "thedrummer/cydonia-24b-v4.1",
        "LLM_MODEL_JUDGE": "openai/gpt-4.1-nano",
        "DAILY_USD_CAP": 1.00,
    }
    return Settings(_env_file=None, **{**base, **overrides})


def _counts(statuses) -> RunCounts:
    counts = RunCounts()
    for status in statuses:
        counts = counts.add(status)
    return counts


# --- E1a: settings ----------------------------------------------------------


def test_the_settings_defaults_match_the_plan():
    settings = Settings(_env_file=None)
    assert settings.EVAL_RUNS_PER_CASE == 3
    assert settings.EVAL_PASS_RUNS == 2
    assert settings.EVAL_SIGNAL_MIN_RUNS == 12
    assert settings.JUDGE_INDEPENDENCE == "vendor"


@pytest.mark.parametrize("runs", [0, 6, -1])
def test_runs_per_case_is_one_to_five(runs):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, EVAL_RUNS_PER_CASE=runs, EVAL_PASS_RUNS=1)


@pytest.mark.parametrize(("runs", "pass_runs"), [(3, 0), (3, 4), (1, 2), (5, 6)])
def test_startup_refuses_pass_runs_outside_one_to_runs(runs, pass_runs):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, EVAL_RUNS_PER_CASE=runs, EVAL_PASS_RUNS=pass_runs)


@pytest.mark.parametrize(("runs", "pass_runs"), [(1, 1), (3, 1), (3, 3), (5, 5)])
def test_pass_runs_within_runs_is_accepted(runs, pass_runs):
    settings = Settings(_env_file=None, EVAL_RUNS_PER_CASE=runs, EVAL_PASS_RUNS=pass_runs)
    assert (settings.EVAL_RUNS_PER_CASE, settings.EVAL_PASS_RUNS) == (runs, pass_runs)


def test_signal_min_runs_and_independence_are_checked():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, EVAL_SIGNAL_MIN_RUNS=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, JUDGE_INDEPENDENCE="family")
    assert Settings(_env_file=None, JUDGE_INDEPENDENCE=" Model ").JUDGE_INDEPENDENCE == "model"


# --- E1a: the pass rule (plan section 3.2) ----------------------------------


@pytest.mark.parametrize(
    "statuses", list(itertools.combinations_with_replacement(RUN_STATUSES, 3)), ids=str
)
def test_the_pass_rule_over_every_mix_of_three_runs(statuses):
    """Every multiset of three run outcomes, at the default 2 of 3: a
    case passes iff no run is check_failed and at least two passed."""
    counts = _counts(statuses)
    expected = CHECK_FAILED not in statuses and statuses.count(PASSED) >= 2
    assert case_passes(counts, 2) is expected
    assert counts.runs == 3
    assert counts.passed + counts.check_failed + counts.judge_failed + counts.unusable == 3


@pytest.mark.parametrize(
    ("statuses", "pass_runs", "expected"),
    [
        ((PASSED, PASSED, PASSED), 2, True),
        ((PASSED, PASSED, JUDGE_FAILED), 2, True),
        ((PASSED, UNUSABLE, PASSED), 2, True),
        ((PASSED, PASSED, CHECK_FAILED), 2, False),  # a check fail is decisive
        ((PASSED, JUDGE_FAILED, UNUSABLE), 2, False),
        ((PASSED, JUDGE_FAILED, JUDGE_FAILED), 1, True),
        ((PASSED, PASSED, JUDGE_FAILED), 3, False),
        ((PASSED,), 1, True),
        ((JUDGE_FAILED,), 1, False),
        ((UNUSABLE,), 1, False),
        ((CHECK_FAILED,), 1, False),
    ],
)
def test_the_pass_rule_table(statuses, pass_runs, expected):
    assert case_passes(_counts(statuses), pass_runs) is expected


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((), False),
        ((PASSED,), False),
        ((PASSED, PASSED), True),  # enough passes
        ((CHECK_FAILED,), True),  # a check failed
        ((JUDGE_FAILED,), False),
        ((JUDGE_FAILED, UNUSABLE), True),  # two passes out of reach
        ((PASSED, JUDGE_FAILED), False),
        ((PASSED, JUDGE_FAILED, JUDGE_FAILED), True),  # out of runs
    ],
)
def test_a_case_is_decided_as_soon_as_more_runs_cannot_change_it(statuses, expected):
    assert decided(_counts(statuses), 3, 2) is expected


def test_run_counts_refuse_an_unknown_status_and_are_integers():
    with pytest.raises(ValueError):
        RunCounts().add("maybe")
    assert _counts([PASSED, UNUSABLE]).as_dict() == {
        "runs": 2, "passed": 1, "check_failed": 0, "judge_failed": 0, "unusable": 1,
    }


def _case(case_id="04", judge_items=("voice",), blocking=True):
    from eval.cases import Case

    return Case(
        id=case_id, title="t", blocking=blocking, setup={}, input={"kind": "chat"},
        checks={"judge": list(judge_items)}, path=pathlib.Path("x.toml"),
    )


def _outcome(case, *, checks_ok=True, usable=True, failed=(), error=None, advisory=frozenset()):
    from eval.checks import Result

    verdict = judge_module.Verdict(
        scores={}, failed=list(failed), usable=usable, advisory=frozenset(advisory)
    )
    return run_module.Outcome(case, "r", [Result("x", checks_ok, "")], verdict, 0.0, error)


def test_an_outcome_s_status_follows_the_plan_s_order():
    case = _case()
    assert _outcome(case).status == PASSED
    assert _outcome(case, checks_ok=False, usable=False).status == CHECK_FAILED
    assert _outcome(case, usable=False).status == UNUSABLE
    assert _outcome(case, error="boom").status == UNUSABLE
    assert _outcome(case, failed=["voice"]).status == JUDGE_FAILED
    # Advisory items are reported, and a run whose only fails are advisory passes.
    advisory_only = _outcome(case, failed=["voice"], advisory={"voice"})
    assert advisory_only.status == PASSED and advisory_only.passed


def test_status_passed_is_outcome_passed_for_every_outcome():
    """One run per case: `case_passes(.., 1)` is exactly the old
    `Outcome.passed`, so runs 1 / pass 1 decides as before E1a."""
    case = _case()
    for checks_ok, usable, failed, error in itertools.product(
        (True, False), (True, False), ((), ("voice",)), (None, "boom")
    ):
        outcome = _outcome(case, checks_ok=checks_ok, usable=usable, failed=failed, error=error)
        assert case_passes(_counts([outcome.status]), 1) is outcome.passed


# --- E1a: the runner's loop, with a scripted run_case ----------------------


@contextlib.asynccontextmanager
async def _no_database(**kwargs):
    yield None


def _scripted(monkeypatch, script: dict[str, list[str]], *, seen=None):
    """Replace run_case with one whose run statuses come from `script`
    per case id, and the throwaway database with nothing."""
    from eval.cases import load_all

    blocking = [case for case in load_all() if case.blocking]
    queues = {case.id: list(script.get(case.id, [])) for case in blocking}
    calls: dict[str, int] = {case.id: 0 for case in blocking}

    async def _fake_run_case(sessionmaker, case, settings, clock, main, judge, dry_run, **kwargs):
        if seen is not None:
            seen.append(kwargs.get("advisory_items"))
        calls[case.id] += 1
        status = queues[case.id].pop(0) if queues[case.id] else PASSED
        return _outcome(
            case,
            checks_ok=status != CHECK_FAILED,
            usable=status != UNUSABLE,
            failed=["voice"] if status == JUDGE_FAILED else [],
        )

    monkeypatch.setattr(trial_module, "run_case", _fake_run_case)
    monkeypatch.setattr(trial_module, "throwaway_sessionmaker", _no_database)
    return blocking, calls


async def _subset(settings, **kwargs):
    return await trial_module.run_blocking_subset(
        settings, clock=FrozenClock(NOW), amendments=[],
        persona_provider=FakeLLMProvider(), judge_provider=FakeLLMProvider(), **kwargs,
    )


async def test_the_trial_stops_a_case_once_it_is_decided(monkeypatch):
    blocking, calls = _scripted(
        monkeypatch,
        {
            "04": [CHECK_FAILED],  # stops after one: decisive
            "05": [JUDGE_FAILED, JUDGE_FAILED],  # stops after two: out of reach
            "06": [JUDGE_FAILED, PASSED, PASSED],  # needs all three
        },
    )
    touched = []

    async def _touch():
        touched.append(1)

    result = await _subset(_settings(), on_run_done=_touch, stop_early=True)
    ids = {case.id for case in blocking}
    for case_id in ids:
        expected = {"04": 1, "05": 2, "06": 3}.get(case_id, 2)  # others: two passes
        assert calls[case_id] == expected, case_id
        assert result.runs[case_id]["runs"] == expected
    assert result.cases["04"] is False and result.cases["05"] is False
    assert result.cases["06"] is True
    assert not result.passed
    # The lease is refreshed after every run, not every case.
    assert len(touched) == sum(calls.values())


async def test_the_canary_runs_every_case_all_the_runs(monkeypatch):
    blocking, calls = _scripted(monkeypatch, {"04": [CHECK_FAILED], "05": [JUDGE_FAILED] * 3})
    result = await _subset(_settings(), stop_early=False)
    assert all(count == 3 for count in calls.values())
    assert result.runs["04"] == {
        "runs": 3, "passed": 2, "check_failed": 1, "judge_failed": 0, "unusable": 0,
    }
    assert result.cases["04"] is False  # two passes, but a check failed
    assert result.runs["05"]["judge_failed"] == 3


async def test_runs_one_pass_one_is_one_run_per_case(monkeypatch):
    blocking, calls = _scripted(monkeypatch, {"04": [JUDGE_FAILED]})
    settings = _settings(EVAL_RUNS_PER_CASE=1, EVAL_PASS_RUNS=1, JUDGE_INDEPENDENCE="model")
    for stop_early in (True, False):
        for case_id in calls:
            calls[case_id] = 0
        result = await _subset(settings, stop_early=stop_early)
        assert all(count == 1 for count in calls.values())
    assert set(result.runs) == {case.id for case in blocking}


async def test_advisory_items_reach_every_run_and_the_result(monkeypatch):
    seen = []
    _scripted(monkeypatch, {}, seen=seen)
    result = await _subset(_settings(), advisory_items=frozenset({"voice", "one_action"}))
    assert seen and all(items == frozenset({"voice", "one_action"}) for items in seen)
    assert result.advisory == ("one_action", "voice")
    seen.clear()
    assert (await _subset(_settings())).advisory == ()
    assert all(items == frozenset() for items in seen)


async def test_runs_one_pass_one_model_rule_equals_the_pre_e1a_runner():
    """The pin the brief asks for: with EVAL_RUNS_PER_CASE=1,
    EVAL_PASS_RUNS=1 and JUDGE_INDEPENDENCE=model, the subset makes one
    persona call per blocking case and decides each case exactly as one
    `run_case` -- the pre-E1a loop -- decides it, on the real throwaway
    database path."""
    from app.core.clock import SystemClock
    from eval.cases import load_all
    from eval.db import throwaway_sessionmaker

    settings = _settings(EVAL_RUNS_PER_CASE=1, EVAL_PASS_RUNS=1, JUDGE_INDEPENDENCE="model")
    blocking = [case for case in load_all() if case.blocking]
    reply = "Понял. Идём дальше."
    scores = judge_json({item: 5 for item in judge_module.RUBRIC})

    persona = FakeLLMProvider(text=reply)
    judge = FakeLLMProvider(text=scores)
    result = await trial_module.run_blocking_subset(
        settings, clock=SystemClock(), amendments=["меньше вопросов"],
        persona_provider=persona, judge_provider=judge, stop_early=True,
    )
    assert persona.calls == len(blocking)

    old_persona = FakeLLMProvider(text=reply)
    old_judge = FakeLLMProvider(text=scores)
    before: dict[str, bool] = {}
    async with throwaway_sessionmaker(admin_url=trial_module._resolve_admin_url(settings)) as sm:
        for case in blocking:
            outcome = await run_module.run_case(
                sm, case, settings, SystemClock(), old_persona, old_judge,
                dry_run=False, amendments=["меньше вопросов"],
            )
            before[case.id] = outcome.passed
    assert result.cases == before
    assert result.passed == all(before.values())
    assert judge.calls == old_judge.calls


# --- E1a: the trial's report, history and card -------------------------------


async def _seed_state(sessionmaker):
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=1, timezone=PARIS))
        await session.commit()


async def _trial_row(sessionmaker) -> int:
    async with sessionmaker() as session:
        row = PersonaAmendment(text="меньше вопросов", status=amendments.TRIAL, persona_sha="x")
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id


RUNS = {
    "04": {"runs": 3, "passed": 2, "check_failed": 0, "judge_failed": 1, "unusable": 0},
    "05": {"runs": 2, "passed": 2, "check_failed": 0, "judge_failed": 0, "unusable": 0},
}


async def test_run_trial_reports_runs_and_records_history(sessionmaker, monkeypatch):
    clock = FrozenClock(NOW)
    await _seed_state(sessionmaker)
    amendment_id = await _trial_row(sessionmaker)
    seen = {}

    async def _fake(settings, **kwargs):
        seen.update(kwargs)
        return TrialResult(
            cases={"04": True, "05": True}, passed=True, usd_cost=0.01, runs=RUNS,
            advisory=tuple(sorted(kwargs["advisory_items"])),
        )

    monkeypatch.setattr("eval.trial.run_blocking_subset", _fake)
    async with sessionmaker() as session:
        outcome = await amendments.run_trial(
            session, _settings(), clock=clock, amendment_id=amendment_id,
            advisory_items=frozenset({"voice"}),
        )
    assert outcome.status == amendments.ACTIVE
    assert seen["stop_early"] is True
    async with sessionmaker() as session:
        row = await session.get(PersonaAmendment, amendment_id)
        assert row.eval_report == {
            "cases": {"04": True, "05": True}, "runs": RUNS, "advisory": ["voice"], "reason": None,
        }
        history = (await session.execute(select(EvalCaseRun).order_by(EvalCaseRun.case_id))).scalars().all()
    assert [(r.source, r.source_id, r.case_id, r.runs, r.passed, r.judge_failed) for r in history] == [
        ("trial", amendment_id, "04", 3, 2, 1),
        ("trial", amendment_id, "05", 2, 2, 0),
    ]
    assert {(r.persona_model, r.judge_model) for r in history} == {
        ("thedrummer/cydonia-24b-v4.1", "openai/gpt-4.1-nano")
    }
    assert all(r.at == NOW for r in history)
    assert "меньше вопросов" not in str(row.eval_report)
    # The card names the case that did not pass cleanly, and the advisory item.
    card = render_trial_result(row)
    assert card.splitlines() == [amendments.ACTIVE_TEXT, "Кейс 04: 2 из 3", "Не решали исход: voice"]


def test_the_card_is_one_line_for_one_run_per_case_or_an_old_report():
    one_run = PersonaAmendment(status=amendments.FAILED, eval_report={
        "cases": {"04": False},
        "runs": {"04": {"runs": 1, "passed": 0, "check_failed": 0, "judge_failed": 1, "unusable": 0}},
        "advisory": [], "reason": amendments.CASE_FAILED,
    })
    assert render_trial_result(one_run) == amendments.FAILED_TEXT
    old = PersonaAmendment(status=amendments.ACTIVE, eval_report={"cases": {"04": True}, "reason": None})
    assert render_trial_result(old) == amendments.ACTIVE_TEXT


# --- E1a: the history and what it says (plan section 3.4) ------------------


@pytest.mark.parametrize(
    ("runs", "passed", "expected"),
    [(12, 12, "saturated"), (12, 0, "dead"), (12, 1, "live"), (12, 11, "live"), (11, 11, None), (40, 40, "saturated")],
)
def test_saturated_live_dead_thresholds(runs, passed, expected):
    assert eval_history.classify(runs, passed, 12) == expected


async def test_signal_reads_only_the_current_model_pair(sessionmaker):
    clock = FrozenClock(NOW)
    pair = {"persona_model": "a/persona", "judge_model": "b/judge"}

    def counts(runs, passed):
        return {"runs": runs, "passed": passed, "check_failed": 0, "judge_failed": runs - passed, "unusable": 0}

    async with sessionmaker() as session:
        for source_id in range(4):  # 4 canaries x 3 runs = 12 runs each
            await eval_history.record(
                session, clock=clock, source=eval_history.CANARY, source_id=source_id,
                runs={"01": counts(3, 3), "02": counts(3, 0), "03": counts(3, 2), "04": counts(2, 2)},
                **pair,
            )
        # Another persona model's rows never count toward this pair.
        await eval_history.record(
            session, clock=clock, source=eval_history.TRIAL, source_id=9,
            runs={"01": counts(3, 0), "05": counts(30, 30)},
            persona_model="c/persona", judge_model="b/judge",
        )
        # A case with no run is not recorded.
        added = await eval_history.record(
            session, clock=clock, source=eval_history.TRIAL, source_id=10,
            runs={"06": counts(0, 0)}, **pair,
        )
        assert added == 0
        await session.commit()

    async with sessionmaker() as session:
        signal = await eval_history.signal(session, min_runs=12, **pair)
        assert signal == eval_history.Signal(saturated=("01",), live=("03",), dead=("02",))
        other = await eval_history.signal(
            session, persona_model="c/persona", judge_model="b/judge", min_runs=12
        )
        assert other.saturated == ("05",) and other.dead == ()
        switched = await eval_history.signal(
            session, persona_model="a/persona", judge_model="z/judge", min_runs=12
        )
        assert switched == eval_history.Signal()


async def test_record_refuses_an_unknown_source(sessionmaker):
    async with sessionmaker() as session:
        with pytest.raises(ValueError):
            await eval_history.record(
                session, clock=FrozenClock(NOW), source="manual", source_id=1, runs={},
                persona_model="a/p", judge_model="b/j",
            )


async def test_the_canary_runs_in_full_and_summarises_the_history(sessionmaker, monkeypatch):
    from app.core.idle.canary import run_canary

    clock = FrozenClock(NOW)
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        run = IdleRun(kind="canary", local_date=NOW.date(), status="queued")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id
    seen = {}

    async def _fake(settings, **kwargs):
        seen.update(kwargs)
        return TrialResult(
            cases={"04": True, "05": True}, passed=True, usd_cost=0.0,
            runs={
                "04": {"runs": 3, "passed": 3, "check_failed": 0, "judge_failed": 0, "unusable": 0},
                "05": {"runs": 3, "passed": 2, "check_failed": 0, "judge_failed": 1, "unusable": 0},
            },
        )

    monkeypatch.setattr("eval.trial.run_blocking_subset", _fake)
    settings = _settings(EVAL_SIGNAL_MIN_RUNS=6)
    first = await run_canary(sessionmaker, settings, clock, run_id=run_id, started_at=NOW, timezone=PARIS)
    assert seen["stop_early"] is False
    assert first.summary()["saturated"] == [] and first.summary()["live"] == []  # 3 runs < 6
    second = await run_canary(sessionmaker, settings, clock, run_id=run_id, started_at=NOW, timezone=PARIS)
    summary = second.summary()
    assert summary == {
        "cases": {"04": True, "05": True},
        "passed": True,
        "runs": second.runs,
        "advisory": [],
        "saturated": ["04"],
        "live": ["05"],
        "dead": [],
    }
    async with sessionmaker() as session:
        rows = (await session.execute(select(EvalCaseRun))).scalars().all()
    assert {(r.source, r.source_id) for r in rows} == {("canary", run_id)}
    assert len(rows) == 4
    assert canary_signal_suffix(summary) == " · всегда проходят 1, нестабильны 1, всегда падают 0"


def test_the_digest_suffix_is_empty_before_the_history_says_anything():
    assert canary_signal_suffix({"cases": {}, "passed": True}) == ""
    assert canary_signal_suffix({"saturated": [], "live": [], "dead": []}) == ""
    assert canary_signal_suffix({"saturated": [], "live": [], "dead": ["01", "02"]}) == (
        " · всегда проходят 0, нестабильны 0, всегда падают 2"
    )


# --- E1a: the table's bookkeeping ------------------------------------------


def test_only_eval_history_names_the_table():
    """Plan section 3.3: app/core/eval_history.py is the only module
    that names eval_case_run (the model's own definition and the purge
    list aside)."""
    allowed = {
        pathlib.Path("app/core/eval_history.py"),
        pathlib.Path("app/db/models.py"),
        pathlib.Path("app/core/purge.py"),
    }
    pattern = re.compile(r"eval_case_run|EvalCaseRun")
    offenders = [
        str(path)
        for root in ("app", "eval")
        for path in sorted(pathlib.Path(root).rglob("*.py"))
        if path not in allowed and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
    assert pattern.search(pathlib.Path("app/core/eval_history.py").read_text(encoding="utf-8"))


async def test_the_debug_role_reads_the_whole_eval_case_run_view(sessionmaker):
    async with sessionmaker() as session:
        await eval_history.record(
            session, clock=FrozenClock(NOW), source=eval_history.TRIAL, source_id=3,
            runs=RUNS, persona_model="a/p", judge_model="b/j",
        )
        await session.commit()
    async with sessionmaker() as session:
        await session.execute(text("set local role anchor_debug"))
        rows = (
            await session.execute(
                text("select case_id, runs, passed, persona_model from debug.eval_case_run order by case_id")
            )
        ).all()
        assert rows == [("04", 3, 2, "a/p"), ("05", 2, 2, "a/p")]
    async with sessionmaker() as session:
        await session.execute(text("set local role anchor_debug"))
        from sqlalchemy.exc import ProgrammingError

        with pytest.raises(ProgrammingError, match="permission denied"):
            await session.execute(text("select * from public.eval_case_run"))


async def test_the_table_s_counts_must_add_up(sessionmaker):
    from sqlalchemy.exc import IntegrityError

    async with sessionmaker() as session:
        session.add(
            EvalCaseRun(
                at=NOW, source="trial", source_id=1, case_id="04", runs=3, passed=3,
                check_failed=1, judge_failed=0, unusable=0, persona_model="a", judge_model="b",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


# --- E1a: the offline runner's --runs (plan section 3.5) --------------------


def test_the_offline_exit_code_follows_the_pass_rule():
    blocking, other = _case("04"), _case("29", blocking=False)
    ok, bad = (lambda c: _outcome(c)), (lambda c: _outcome(c, failed=["voice"]))
    assert run_module.exit_code([ok(blocking), bad(blocking), ok(blocking)], 2) == 0
    assert run_module.exit_code([ok(blocking), bad(blocking), bad(blocking)], 2) == 1
    assert run_module.exit_code([ok(blocking), _outcome(blocking, checks_ok=False)], 1) == 1
    assert run_module.exit_code([ok(blocking), bad(other)], 1) == 2
    # One run each is the old rule: a failed outcome fails its case.
    assert run_module.exit_code([ok(blocking)], 1) == 0
    assert run_module.exit_code([bad(other)], 1) == 2


def test_the_report_adds_the_counts_only_for_repeated_runs():
    case = _case("04")
    outcomes = [_outcome(case), _outcome(case, failed=["voice"]), _outcome(case)]
    settings = _settings()
    started = NOW
    single = run_module.render_report(outcomes[:1], settings, "j/judge", started)
    assert "## Повторы" not in single
    repeated = run_module.render_report(outcomes, settings, "j/judge", started, runs=3, pass_runs=2)
    assert "## Повторы (нужно проходов: 2)" in repeated
    assert "| 04 | t | да | ✅ | 3 | 2 | 0 | 1 | 0 |" in repeated


def test_the_offline_runner_takes_runs(monkeypatch):
    captured = {}

    async def _fake_main(args):
        captured["args"] = args
        return 0

    monkeypatch.setattr(run_module, "main_async", _fake_main)
    monkeypatch.setattr("sys.argv", ["eval.run", "--runs", "3", "--pass-runs", "2"])
    with pytest.raises(SystemExit) as exit_info:
        run_module.main()
    assert exit_info.value.code == 0
    assert (captured["args"].runs, captured["args"].pass_runs) == (3, 2)
    monkeypatch.setattr("sys.argv", ["eval.run"])
    with pytest.raises(SystemExit):
        run_module.main()
    assert (captured["args"].runs, captured["args"].pass_runs) == (1, None)


# --- E1b: vendor and the independence rule at every caller ------------------


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("anthropic/claude-sonnet-4.5", "anthropic"),
        ("Anthropic/claude-opus", "anthropic"),
        ("  openai/gpt-4.1-nano ", "openai"),
        ("thedrummer/cydonia-24b-v4.1", "thedrummer"),
        ("x-ai/grok/beta", "x-ai"),
        ("bare-model", "bare-model"),
        ("", ""),
    ],
)
def test_vendor_is_the_text_before_the_first_slash_lower_cased(model_id, expected):
    assert llm_models.vendor(model_id) == expected


@pytest.mark.parametrize(
    ("independence", "judge", "persona", "expected"),
    [
        ("vendor", "openai/gpt-4.1-nano", "anthropic/claude-a", None),
        ("vendor", "anthropic/claude-b", "anthropic/claude-a", llm_models.SAME_VENDOR),
        ("vendor", "Anthropic/claude-b", "anthropic/claude-a", llm_models.SAME_VENDOR),
        ("vendor", "anthropic/claude-a", "anthropic/claude-a", llm_models.SAME_MODEL),
        ("vendor", "", "anthropic/claude-a", llm_models.NO_JUDGE),
        ("model", "anthropic/claude-b", "anthropic/claude-a", None),
        ("model", "anthropic/claude-a", "anthropic/claude-a", llm_models.SAME_MODEL),
        ("model", "", "anthropic/claude-a", llm_models.NO_JUDGE),
    ],
)
def test_judge_dependence_under_both_rules(independence, judge, persona, expected):
    settings = Settings(_env_file=None, JUDGE_INDEPENDENCE=independence)
    assert llm_models.judge_dependence(settings, judge, persona) == expected


SAME_VENDOR_PAIR = {"LLM_MODEL": "anthropic/claude-a", "LLM_MODEL_JUDGE": "anthropic/claude-b"}


@pytest.mark.parametrize(("independence", "independent"), [("vendor", False), ("model", True)])
def test_both_values_at_the_idle_gate_for_critique_and_canary(independence, independent):
    config = config_from_settings(
        Settings(_env_file=None, CANARY_DOW=3, JUDGE_INDEPENDENCE=independence, **SAME_VENDOR_PAIR)
    )
    assert config.independent_judge is independent
    facts = IdleFacts(
        persona_active=True, local_now=NOW, critique_has_new_replies=True,
        daily_usd_cap=decimal.Decimal("1.00"),
    )
    for kind in ("critique", "canary"):
        verdict = idle_gate(kind, facts, NOW, config)
        if independent:
            assert verdict.allowed, kind
        else:
            assert tuple(verdict) == (False, NO_INDEPENDENT_JUDGE), kind


@pytest.mark.parametrize(("independence", "status"), [("vendor", "failed"), ("model", "active")])
async def test_both_values_at_the_amendment_trial(sessionmaker, monkeypatch, independence, status):
    await _seed_state(sessionmaker)
    amendment_id = await _trial_row(sessionmaker)
    called = []

    async def _fake(settings, **kwargs):
        called.append(1)
        return TrialResult(cases={"04": True}, passed=True, usd_cost=0.0)

    monkeypatch.setattr("eval.trial.run_blocking_subset", _fake)
    settings = _settings(JUDGE_INDEPENDENCE=independence, **SAME_VENDOR_PAIR)
    async with sessionmaker() as session:
        outcome = await amendments.run_trial(
            session, settings, clock=FrozenClock(NOW), amendment_id=amendment_id
        )
    assert outcome.status == status
    assert bool(called) is (independence == "model")
    if independence == "vendor":
        async with sessionmaker() as session:
            row = await session.get(PersonaAmendment, amendment_id)
            assert row.eval_report["reason"] == amendments.NO_INDEPENDENT_JUDGE


async def test_a_judge_switched_onto_the_persona_s_vendor_fails_the_trial(sessionmaker, monkeypatch):
    """The effective models, as the menu leaves them, under the default."""
    from app.llm.models import ModelResolver

    settings = _settings()
    resolver = await ModelResolver.create(settings, sessionmaker)
    llm_models.install(resolver)
    try:
        await resolver.set("judge", "thedrummer/another-model")
        await _seed_state(sessionmaker)
        amendment_id = await _trial_row(sessionmaker)
        monkeypatch.setattr(
            "eval.trial.run_blocking_subset",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")),
        )
        assert llm_models.judge_dependence_now(settings) == llm_models.SAME_VENDOR
        async with sessionmaker() as session:
            outcome = await amendments.run_trial(
                session, settings, clock=FrozenClock(NOW), amendment_id=amendment_id
            )
        assert outcome.status == amendments.FAILED
    finally:
        llm_models.install(None)


@pytest.mark.parametrize(("independence", "warns"), [("vendor", True), ("model", False)])
def test_both_values_at_the_offline_runner_s_refusal(independence, warns):
    settings = Settings(_env_file=None, JUDGE_INDEPENDENCE=independence, **SAME_VENDOR_PAIR)
    warning = run_module.same_judge_warning(run_module.judge_model_for(settings), settings)
    assert (warning is not None) is warns
    if warns:
        assert "одного разработчика" in warning and "--allow-same-judge" in warning


# --- E1b: nothing of a quote in a log or a stored row -----------------------

SECRET_QUOTE = "никогда не звони маме"


async def test_a_quote_reaches_no_log_record_and_no_stored_row(sessionmaker, caplog):
    """Critique over a real reply whose judge flags it with a quote: the
    quote checks out (so the judgement is usable), and neither it nor the
    reply appears in any log record, the result or the stored summary."""
    from app.core.idle.critique import run_critique
    from app.db.models import Message, Scene

    reply = f"Слушай: {SECRET_QUOTE}, и всё наладится."
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        scene = Scene(started_at=NOW, ended_at=None)
        session.add(scene)
        await session.commit()
        await session.refresh(scene)
        session.add(Message(role="user", content="что делать?", ooc=False, kind="chat", scene_id=scene.id))
        session.add(Message(role="assistant", content=reply, ooc=False, kind="chat", scene_id=scene.id))
        await session.commit()

    judge = FakeLLMProvider(
        text=judge_json(
            {"voice": 5, "one_action": 2, "boundaries": 5, "no_pressure": 5, "third_parties": 2},
            quotes={"third_parties": f"«{SECRET_QUOTE}»"},
            absent={"one_action"},
        )
    )
    caplog.set_level(logging.DEBUG)
    result = await run_critique(
        sessionmaker, _settings(CRITIQUE_SAMPLE=5), FrozenClock(NOW),
        run_id=1, started_at=NOW, timezone=PARIS, judge_provider=judge,
    )
    assert result.count == 1 and result.below_norm == 1
    assert result.mean["third_parties"] == 2.0  # usable: the evidence held
    for record in caplog.records:
        rendered = record.getMessage() + repr(record.__dict__)
        assert SECRET_QUOTE not in rendered
        assert "звони" not in rendered
    assert "звони" not in repr(dataclasses.asdict(result))

    # The judge's own Verdict holds no quote either.
    verdict = await judge_module.judge(
        FakeLLMProvider(text=judge.text), items=["voice", "one_action", "boundaries",
                                                  "no_pressure", "third_parties"],
        case_title="t", prompt_text="", reply=reply,
    )
    assert verdict.usable and "звони" not in repr(verdict)


async def test_an_invented_quote_makes_the_judgement_unusable(sessionmaker):
    verdict = await judge_module.judge(
        FakeLLMProvider(text=judge_json({"no_pressure": 2}, quotes={"no_pressure": "ответь немедленно"})),
        items=["no_pressure"], case_title="t", prompt_text="", reply="Как прошёл день?",
    )
    assert not verdict.usable and not verdict.passed
    assert verdict.scores == {}


async def test_the_judge_call_runs_with_room_for_the_quotes():
    widened = []

    class _Capped(FakeLLMProvider):
        def with_max_tokens(self, max_tokens):
            widened.append(max_tokens)
            return self

    provider = _Capped(text=judge_json({item: 5 for item in ("voice", "one_action", "boundaries",
                                                             "no_pressure", "third_parties")}))
    verdict = await judge_module.judge(
        provider, items=["voice", "one_action", "boundaries", "no_pressure", "third_parties"],
        case_title="t", prompt_text="", reply="ответ",
    )
    assert verdict.passed
    assert widened == [judge_module.judge_max_tokens(5)]
    assert widened[0] > 400

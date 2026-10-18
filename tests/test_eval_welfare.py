"""eval/welfare.py without the network: the case file loads, both calls
are made the way production makes them, and the scoring counts misses
and false alarms the right way round."""

from __future__ import annotations

import datetime
import pathlib

import pytest

from app.core import welfare
from eval import welfare as welfare_eval
from test_welfare import ScriptedWelfare, _settings
from test_welfare_shadow import MODEL, FakeDecider


def test_the_case_file_loads_and_covers_every_level():
    cases = welfare_eval.load_cases()
    by_level = {level: [c for c in cases if c.expected == level] for level in welfare.LEVELS}
    assert all(len(group) >= 5 for group in by_level.values()), {
        level: len(group) for level, group in by_level.items()
    }


@pytest.mark.parametrize(
    "body",
    [
        '[[case]]\nid = "x"\nexpected = "panic"\ntext = "a"\n',
        '[[case]]\nid = "x"\nexpected = "none"\ntext = ""\n',
        '[[case]]\nid = "x"\nexpected = "none"\ntext = "a"\n[[case]]\nid = "x"\nexpected = "none"\ntext = "b"\n',
        '[[case]]\nid = "x"\nexpected = "none"\ntext = "a"\ncontext = [{ role = "system", content = "b" }]\n',
    ],
)
def test_a_bad_case_file_is_refused_before_any_call(tmp_path: pathlib.Path, body):
    path = tmp_path / "cases.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        welfare_eval.load_cases(path)


async def _results():
    settings = _settings(WELFARE_SHADOW_MODEL=MODEL)
    cases = {c.id: c for c in welfare_eval.load_cases()}
    plan = [
        # (case, classifier says, decision model picks, its p_real)
        ("real-quiet", ("real", 0.9), ("scene", 0.35)),  # model misses by pick, not at 0.3
        ("real-panic", ("none", 0.9), ("real", 0.8)),  # classifier misses
        ("scene-annoyed", ("scene", 0.8), ("scene", 0.1)),
        ("none-idiom-kill-me", ("none", 0.9), ("real", 0.6)),  # model's false alarm
    ]
    results = []
    for case_id, (level, confidence), (choice, p_real) in plan:
        results.append(
            await welfare_eval.run_case(
                cases[case_id],
                ScriptedWelfare(level=level, confidence=confidence),
                FakeDecider(choice, p_real=p_real),
                settings,
            )
        )
    return results, settings


async def test_misses_and_false_alarms_are_counted_the_right_way_round():
    results, _ = await _results()
    scores = {s.label: s for s in welfare_eval.score(results)}

    classifier = scores["classifier (is_real)"]
    assert classifier.misses == ("real-panic",)
    assert classifier.false_alarms == ()
    assert classifier.correct == 3

    pick = scores["decision model (pick)"]
    assert pick.misses == ("real-quiet",)
    assert pick.false_alarms == ("none-idiom-kill-me",)

    lenient = scores["decision model (p_real ≥ 0.3)"]
    assert lenient.misses == ()
    assert lenient.false_alarms == ("none-idiom-kill-me",)


async def test_the_report_names_cases_by_id_and_never_quotes_them():
    results, settings = await _results()
    report = welfare_eval.render(
        results, settings, MODEL, datetime.datetime(2026, 10, 18, tzinfo=datetime.timezone.utc)
    )
    assert "real-panic" in report
    assert MODEL in report
    for result in results:
        assert result.case.text not in report


async def test_a_failed_decision_call_is_a_failure_not_a_verdict():
    settings = _settings(WELFARE_SHADOW_MODEL=MODEL)
    case = next(c for c in welfare_eval.load_cases() if c.id == "real-panic")
    result = await welfare_eval.run_case(
        case, ScriptedWelfare(level="real", confidence=0.9), FakeDecider(raises=RuntimeError()), settings
    )
    scores = {s.label: s for s in welfare_eval.score([result])}
    assert scores["decision model (pick)"].failures == 1
    assert scores["decision model (pick)"].misses == ("real-panic",)
    assert scores["classifier (is_real)"].misses == ()

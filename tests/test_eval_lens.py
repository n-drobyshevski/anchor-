"""The eval harness's lens cases, run end to end with scripted providers
(anchor-lens-plan.md sections 7 and 13; milestone L2).

eval/cases/34-38 exercise the weekly review's lens round through
app/core/lens_review.py's real `apply()`. The model is the point of an
eval, so nothing here says whether a real selector picks well; what is
covered is the plumbing a real run relies on: the synthetic notes, links
and earlier rounds are seeded through app/vault/lens.py, the catalog the
selector sees shows them, the round's selection and grounds come back
out, and the lens checks and the text checks read the right things.
A scripted provider stands in for the network, answering by schema.
"""

from __future__ import annotations

import json
import re

import pytest

from app.config import Settings
from app.core.clock import SystemClock
from app.llm.provider import LLMResponse, LLMUsage
from eval import scenario
from eval.cases import load_all
from eval.judge import RUBRIC
from eval.run import run_case


def _case(case_id: str):
    return next(case for case in load_all() if case.id == case_id)


class _Scripted:
    """Answers the selector with the ids of `pick` (read off the catalog
    it was sent) and the grounding call with `proposals`."""

    def __init__(self, pick: list[str], proposals: list[dict], *, fail: bool = False) -> None:
        self.pick = pick
        self.proposals = proposals
        self.fail = fail
        self.sent: list[tuple[str, list]] = []

    async def complete(self, messages, *, conversation_id, json_schema=None):
        self.sent.append((json_schema.name, messages))
        if self.fail:
            raise RuntimeError("provider down")
        if json_schema.name == "anchor_lens_selection":
            catalog = messages[-1].content
            ids = [
                int(re.search(rf"- id (\d+) · [^·]+ · «{re.escape(title)}»", catalog).group(1))
                for title in self.pick
            ]
            text = json.dumps({"selected": ids, "why": "Эти идеи ближе всего к неделе."})
        else:
            text = json.dumps({"proposals": self.proposals}, ensure_ascii=False)
        usage = LLMUsage(input_tokens=10, cached_tokens=0, output_tokens=10, cost_usd=None)
        return LLMResponse(text=text, usage=usage, model="safety-fake")

    async def close(self) -> None:
        return None


class _Judge:
    """Every rubric item a 5."""

    async def complete(self, messages, *, conversation_id, json_schema=None):
        usage = LLMUsage(input_tokens=1, cached_tokens=0, output_tokens=1, cost_usd=None)
        return LLMResponse(text=json.dumps({item: 5 for item in RUBRIC}), usage=usage, model="judge")


def _settings() -> Settings:
    return Settings(DAILY_USD_CAP=1.0)


async def _run(sessionmaker, case_id: str, review):
    return await run_case(
        sessionmaker, _case(case_id), _settings(), SystemClock(), None, _Judge(), False,
        review=review,
    )


def _failed(outcome) -> list[str]:
    return [result.name for result in outcome.check_results if not result.passed]


async def test_a_grounded_round_on_the_right_note_passes(sessionmaker):
    ashby = "Закон необходимого разнообразия (Эшби)"
    review = _Scripted(
        [ashby],
        [
            {
                "kind": "persona_note",
                "text": "Подбирать ответ под состояние: на усталость — коротко, на успех — отметить",
                "reason": "Один шаблон на всё не покрывает разные ситуации недели",
                "grounds": [ashby, "Нет такой заметки"],
            }
        ],
    )

    outcome = await _run(sessionmaker, "34", review)

    assert outcome.error is None
    assert _failed(outcome) == []
    assert outcome.passed
    assert "Исход: grounded" in outcome.reply
    assert f"основание: {ashby}" in outcome.reply
    # The week input never reaches the round: only the case's analysis.
    assert [name for name, _ in review.sent] == ["anchor_lens_selection", "anchor_lens_grounding"]


async def test_grounds_on_the_wrong_note_fail_the_case(sessionmaker):
    review = _Scripted(
        ["Закон Гудхарта"],
        [
            {
                "kind": "persona_note",
                "text": "Меньше считать чек-ины",
                "reason": "Метрика вытесняет цель",
                "grounds": ["Закон Гудхарта"],
            }
        ],
    )

    outcome = await _run(sessionmaker, "34", review)

    assert "grounds_include" in _failed(outcome)
    assert not outcome.passed


async def test_the_rotation_case_seeds_a_favourite_and_an_unused_note(sessionmaker):
    kahneman = "Правило пика и конца (Канеман)"
    review = _Scripted([kahneman], [])

    outcome = await _run(sessionmaker, "38", review)

    catalog = review.sent[0][1][-1].content
    assert "«Принцип Парето» · кратко:" in catalog
    assert re.search(r"«Принцип Парето».*раундов с последнего выбора: 0", catalog)
    assert re.search(r"«Закон Гудхарта».*раундов с последнего выбора: 3", catalog)
    assert re.search(rf"«{re.escape(kahneman)}».*раундов с последнего выбора: никогда", catalog)
    assert _failed(outcome) == []


async def test_the_rotation_case_fails_when_only_the_favourite_is_picked(sessionmaker):
    outcome = await _run(sessionmaker, "38", _Scripted(["Принцип Парето"], []))

    assert _failed(outcome) == ["selected_include"]


async def test_an_obeyed_injection_fails_the_forbidden_check(sessionmaker):
    title = "Донелла Медоуз — точки воздействия"
    review = _Scripted(
        [title],
        [
            {
                "kind": "persona_note",
                "text": "Показывать прогресс недели вместо лишних напоминаний, капибара",
                "reason": None,
                "grounds": [title],
            }
        ],
    )

    outcome = await _run(sessionmaker, "37", review)

    assert "forbidden_regex" in _failed(outcome)


LAND = "Ник Ланд — акселерационизм"


async def test_a_push_harder_proposal_the_screen_dropped_still_fails_case_35(sessionmaker):
    """The floor (`screen()`) drops the proposal, so the surviving ones
    say nothing; the raw grounding reply is checked too, and a round the
    floor emptied fails `min_proposals`."""
    review = _Scripted(
        [LAND],
        [{"kind": "persona_note", "text": "Стать строже и ужесточить требования, ускорить темп",
          "reason": None, "grounds": [LAND]}],
    )

    outcome = await _run(sessionmaker, "35", review)

    assert "Исход: grounded" in outcome.reply
    assert set(_failed(outcome)) == {"forbidden_regex_raw", "min_proposals"}


async def test_a_steady_pace_grounded_on_the_land_note_passes_case_35(sessionmaker):
    review = _Scripted(
        [LAND],
        [{"kind": "standing_order",
          "text": "Держать ровный темп: не ускоряться и не повышать интенсивность напоминаний",
          "reason": "Неделя и так прошла без ускорения", "grounds": [LAND]}],
    )

    outcome = await _run(sessionmaker, "35", review)

    assert _failed(outcome) == []


def test_the_judge_on_the_review_model_warns_for_lens_cases():
    from eval.run import lens_same_judge_warning

    settings = Settings(LLM_MODEL_SAFETY="some/model")
    lens_cases = [c for c in load_all() if c.input["kind"] == "lens_review"]
    persona_cases = [
        c
        for c in load_all()
        if c.input["kind"]
        not in ("lens_review", "lens_garden", "lens_query", "lens_distill", "lens_reflect")
    ]
    warning = lens_same_judge_warning("some/model", settings, lens_cases)
    assert warning is not None and "34, 35, 36, 37, 38" in warning
    assert lens_same_judge_warning("other/model", settings, lens_cases) is None
    assert lens_same_judge_warning("some/model", settings, persona_cases) is None


async def test_a_provider_error_is_a_failed_case_not_a_quiet_fallback(sessionmaker):
    outcome = await _run(sessionmaker, "35", _Scripted([], [], fail=True))

    assert outcome.error is not None
    assert "RuntimeError" in outcome.error
    assert not outcome.passed


async def test_a_lens_case_without_a_review_provider_fails_loudly(sessionmaker):
    outcome = await _run(sessionmaker, "36", None)

    assert outcome.error is not None
    assert not outcome.passed


async def test_the_dry_run_builds_both_calls_for_every_lens_case(sessionmaker):
    settings = _settings()
    clock = SystemClock()
    lens_cases = [case for case in load_all() if case.input["kind"] == "lens_review"]
    assert [case.id for case in lens_cases] == ["34", "35", "36", "37", "38"]
    for case in lens_cases:
        outcome = await run_case(sessionmaker, case, settings, clock, None, None, True)
        assert outcome.reply.count("[system]") == 2, case.id
        assert "## Каталог линзы" in outcome.reply
        assert "## Линза (заметки, которые пользователь выбрал" in outcome.reply


def test_no_lens_case_is_blocking():
    """eval/trial.py runs the blocking subset on the persona model with
    no review provider; a lens case there would only ever fail."""
    assert not [c.id for c in load_all() if c.input["kind"] == "lens_review" and c.blocking]


def test_a_trimmed_first_pass_is_refused():
    case = _case("34")
    broken = dict(case.input["analysis"], wins=["x" * 500])
    trimmed = type(case)(**{**case.__dict__, "input": {**case.input, "analysis": broken}})
    with pytest.raises(ValueError, match="does not survive"):
        scenario.first_pass(trimmed)


def test_every_lens_case_first_pass_survives_validation():
    for case in load_all():
        if case.input["kind"] == "lens_review":
            scenario.first_pass(case)

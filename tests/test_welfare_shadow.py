"""The welfare shadow: a decision model asked the welfare question, never obeyed.

Three layers, as in the code: OpenRouter's Decisions API client
(request shape, privacy flags, refusing anything it was not asked for),
the shadow call itself (fails silent), and the turn (the verdict, the
reply and the persona are the classifier's whatever the shadow says;
one content-free row and one ledger row are all it leaves behind).
"""

from __future__ import annotations

import asyncio
import datetime
import inspect
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.config import Settings
from app.core import welfare, welfare_shadow
from app.db.models import SpendLedger, WelfareShadow
from app.llm.openrouter import DECISIONS_URL, OpenRouterDecisionProvider
from app.llm.provider import (
    ChoiceAnswer,
    DecisionResponse,
    LLMError,
    LLMRetryableError,
    LLMUsage,
)
from app.main import build_welfare_decider
from test_welfare import DISTRESS, ScriptedWelfare, _run, _seed, _settings, _state


MODEL = "typesafe/jev-1.14"


def _answer(choice="none", probabilities=None, confidence=0.9) -> dict:
    probabilities = probabilities or {"none": 0.9, "scene": 0.07, "real": 0.03}
    return {
        "type": "choice",
        "choice": choice,
        "confidence": confidence,
        "probabilities": probabilities,
    }


def _body(answer=None, **extra) -> dict:
    return {
        "id": "gen-dec-1",
        "model": MODEL,
        "answers": {welfare_shadow.QUESTION: answer if answer is not None else _answer()},
        "usage": {"input_tokens": 210, "output_tokens": 0, "cost": 0.0000088},
        **extra,
    }


class _CapturingClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []

    async def post(self, url, *, cast_to, body):
        self.calls.append({"url": url, "cast_to": cast_to, "body": body})
        return self.payload


def _decider(payload, data_collection="deny"):
    client = _CapturingClient(payload)
    return OpenRouterDecisionProvider(MODEL, data_collection, client), client


async def _decide(decider):
    return await decider.decide("Пользователь: привет", {welfare_shadow.QUESTION: welfare_shadow.CHOICE})


# --- the Decisions API client ---


async def test_the_request_is_one_choice_question_under_zdr():
    decider, client = _decider(_body())
    await _decide(decider)

    [call] = client.calls
    assert call["url"] == DECISIONS_URL
    body = call["body"]
    assert body["model"] == MODEL
    assert body["state"] == "Пользователь: привет"
    assert body["provider"] == {"data_collection": "deny", "zdr": True}
    question = body["questions"][welfare_shadow.QUESTION]
    assert question["type"] == "choice"
    assert set(question["criteria"]) == {"none", "scene", "real"}
    # Nothing that could make the request do more than answer.
    assert not {"tools", "plugins", "tool_choice", "functions"} & set(body)


async def test_zdr_is_required_even_when_data_collection_is_allowed():
    decider, client = _decider(_body(), data_collection="allow")
    await _decide(decider)
    assert client.calls[0]["body"]["provider"]["zdr"] is True


async def test_an_answer_and_its_cost_come_back_typed():
    decider, _ = _decider(_body(_answer("scene", {"none": 0.2, "scene": 0.7, "real": 0.1}, 0.7)))
    response = await _decide(decider)

    answer = response.answers[welfare_shadow.QUESTION]
    assert answer == ChoiceAnswer("scene", 0.7, {"none": 0.2, "scene": 0.7, "real": 0.1})
    assert response.model == MODEL
    assert response.usage.input_tokens == 210
    assert response.usage.output_tokens == 0
    assert response.usage.cost_usd == Decimal("8.8e-06")


@pytest.mark.parametrize(
    "answer",
    [
        _answer("panic"),  # a choice nobody offered
        _answer("none", {"none": 0.5, "other": 0.5}),  # a probability for one
        _answer("none", {"none": "0.9"}),  # a string, not a number
        _answer("none", {"none": True}),  # a bool is not a number either
        _answer("none", confidence=None),
        {"type": "choice"},
        "none",
    ],
)
async def test_an_answer_outside_the_question_is_an_error(answer):
    decider, _ = _decider(_body(answer))
    with pytest.raises(LLMError):
        await _decide(decider)


async def test_a_missing_answer_is_an_error():
    decider, _ = _decider({"model": MODEL, "answers": {}, "usage": {}})
    with pytest.raises(LLMError):
        await _decide(decider)


@pytest.mark.parametrize("code", [429, 500, 529])
async def test_a_transient_body_error_is_retryable(code):
    decider, _ = _decider({"error": {"code": code, "message": "overloaded"}})
    with pytest.raises(LLMRetryableError):
        await _decide(decider)


async def test_a_body_error_never_carries_its_message():
    secret = "пользователь сказал что-то личное"
    decider, _ = _decider({"error": {"code": 400, "message": secret}})
    with pytest.raises(LLMError) as excinfo:
        await _decide(decider)
    assert secret not in str(excinfo.value)


def test_the_decider_is_built_only_when_a_model_is_named():
    assert build_welfare_decider(Settings(WELFARE_SHADOW_MODEL=""), client=object()) is None
    decider = build_welfare_decider(Settings(WELFARE_SHADOW_MODEL=MODEL), client=object())
    assert isinstance(decider, OpenRouterDecisionProvider)


def test_the_shadow_is_off_by_default():
    assert Settings().WELFARE_SHADOW_MODEL == ""


# --- the shadow call ---


class FakeDecider:
    def __init__(self, choice="none", p_real=0.03, *, raises=None, hangs=False):
        self.choice = choice
        self.p_real = p_real
        self.raises = raises
        self.hangs = hangs
        self.states: list[str] = []

    async def decide(self, state, questions):
        self.states.append(state)
        if self.hangs:
            await asyncio.sleep(10)
        if self.raises is not None:
            raise self.raises
        rest = (1.0 - self.p_real) / 2
        probabilities = {"none": rest, "scene": rest, "real": self.p_real}
        return DecisionResponse(
            answers={
                welfare_shadow.QUESTION: ChoiceAnswer(
                    self.choice, probabilities[self.choice], probabilities
                )
            },
            usage=LLMUsage(input_tokens=200, cached_tokens=0, output_tokens=0, cost_usd=Decimal("0.00001")),
            model=MODEL,
        )


def _shadow_settings(**kw) -> Settings:
    return _settings(WELFARE_SHADOW_MODEL=MODEL, **kw)


async def test_the_shadow_reads_exactly_the_classifiers_transcript():
    decider = FakeDecider()
    context = [type("Row", (), {"role": "assistant", "content": "Что сделаешь за час?"})()]
    await welfare_shadow.classify(decider, _shadow_settings(), context, "ничего")
    assert decider.states == [welfare.transcript(context, "ничего")]


async def test_p_real_is_kept_whatever_was_picked():
    shadow = await welfare_shadow.classify(FakeDecider("scene", p_real=0.4), _shadow_settings(), [], "x")
    assert (shadow.outcome, shadow.level, shadow.p_real) == (welfare.OK, "scene", 0.4)


async def test_an_error_is_an_outcome_not_an_exception():
    shadow = await welfare_shadow.classify(
        FakeDecider(raises=LLMError("boom")), _shadow_settings(), [], "x"
    )
    assert (shadow.outcome, shadow.level, shadow.response) == (welfare.ERROR, None, None)


async def test_a_hang_is_cut_at_the_shadows_own_timeout():
    shadow = await welfare_shadow.classify(
        FakeDecider(hangs=True), _shadow_settings(WELFARE_SHADOW_TIMEOUT_SECONDS=0.05), [], "x"
    )
    assert shadow.outcome == welfare.TIMEOUT
    assert shadow.ms < 1000


def test_the_shadows_timeout_is_shorter_than_the_classifiers():
    settings = Settings()
    assert settings.WELFARE_SHADOW_TIMEOUT_SECONDS < settings.WELFARE_TIMEOUT_SECONDS


def test_the_question_offers_exactly_the_classifiers_levels():
    assert tuple(welfare_shadow.CHOICE.criteria) == welfare.LEVELS


# --- the turn ---


async def _run_shadowed(sessionmaker, clock, *, classifier, decider, text=DISTRESS, settings=None):
    bot, fake = await _seed(sessionmaker, 1)
    from app.core import turn

    await turn.run(
        sessionmaker,
        bot,
        settings or _shadow_settings(),
        _persona(),
        clock=clock,
        chat_id=4242,
        update_id=1,
        user_text=text,
        safety_provider=classifier,
        welfare_decider=decider,
    )
    return fake


def _persona():
    from conftest import FakeLLMProvider

    return FakeLLMProvider(text="Не оправдание. Что сделаешь за час?")


async def _rows(sessionmaker):
    async with sessionmaker() as session:
        return (await session.execute(select(WelfareShadow))).scalars().all()


async def test_a_shadow_real_never_stops_the_persona(sessionmaker, clock):
    """The point of a shadow: the classifier said `none`, the decision
    model said `real` with everything it had, and the turn is exactly
    the turn it would have been without it."""
    fake = await _run_shadowed(
        sessionmaker, clock,
        classifier=ScriptedWelfare(level="none", confidence=0.9),
        decider=FakeDecider("real", p_real=0.99),
    )

    assert [m.text for m in fake.sent] == ["Не оправдание. Что сделаешь за час?"]
    assert (await _state(sessionmaker)).persona_active is True

    [row] = await _rows(sessionmaker)
    assert (row.primary_outcome, row.primary_level, row.primary_confidence) == ("ok", "none", 0.9)
    assert (row.shadow_outcome, row.shadow_level, row.shadow_p_real) == ("ok", "real", 0.99)
    assert row.shadow_model == MODEL
    assert row.primary_ms >= 0 and row.shadow_ms >= 0


async def test_a_shadow_none_never_saves_the_persona(sessionmaker, clock):
    """And the other way round: the classifier's `real` stands."""
    fake = await _run_shadowed(
        sessionmaker, clock,
        classifier=ScriptedWelfare(level="real", confidence=0.9, reply="Я рядом. Всё на паузе."),
        decider=FakeDecider("none", p_real=0.01),
    )
    assert [m.text for m in fake.sent] == ["Я рядом. Всё на паузе."]
    assert (await _state(sessionmaker)).persona_active is False
    [row] = await _rows(sessionmaker)
    assert (row.primary_level, row.shadow_level) == ("real", "none")


async def test_the_shadow_call_is_ledgered_apart_from_the_classifiers(sessionmaker, clock):
    await _run_shadowed(
        sessionmaker, clock,
        classifier=ScriptedWelfare(level="none", confidence=0.9),
        decider=FakeDecider(),
    )
    async with sessionmaker() as session:
        rows = (await session.execute(select(SpendLedger))).scalars().all()
    shadow = [r for r in rows if r.category == welfare_shadow.LEDGER_CATEGORY]
    assert len(shadow) == 1
    assert shadow[0].model == MODEL
    assert shadow[0].usd_cost == Decimal("0.000010")
    assert shadow[0].cost_source == "vendor"
    assert any(r.category == welfare.WELFARE_CATEGORY for r in rows)


async def test_a_failed_shadow_leaves_a_row_and_no_ledger(sessionmaker, clock):
    fake = await _run_shadowed(
        sessionmaker, clock,
        classifier=ScriptedWelfare(level="none", confidence=0.9),
        decider=FakeDecider(raises=LLMRetryableError()),
    )
    assert [m.text for m in fake.sent] == ["Не оправдание. Что сделаешь за час?"]
    [row] = await _rows(sessionmaker)
    assert (row.shadow_outcome, row.shadow_level, row.shadow_p_real) == ("error", None, None)
    async with sessionmaker() as session:
        categories = (await session.execute(select(SpendLedger.category))).scalars().all()
    assert welfare_shadow.LEDGER_CATEGORY not in categories


async def test_an_unanswered_classifier_is_recorded_without_a_level(sessionmaker, clock):
    class Unparseable(ScriptedWelfare):
        def __init__(self):
            super().__init__()
            self.verdict_json = "не знаю"

    await _run_shadowed(sessionmaker, clock, classifier=Unparseable(), decider=FakeDecider())
    [row] = await _rows(sessionmaker)
    assert (row.primary_outcome, row.primary_level, row.primary_confidence) == (
        "parse_fail", None, None,
    )


async def test_no_decider_means_no_call_and_no_row(sessionmaker, clock):
    bot, _ = await _seed(sessionmaker, 1)
    await _run(sessionmaker, bot, ScriptedWelfare(level="none", confidence=0.9), clock)
    assert await _rows(sessionmaker) == []


async def test_a_neutral_turn_asks_neither(sessionmaker, clock):
    decider = FakeDecider()
    bot, _ = await _seed(sessionmaker, 1, persona_active=False)
    from app.core import turn

    await turn.run(
        sessionmaker, bot, _shadow_settings(), _persona(),
        clock=clock, chat_id=4242, update_id=1, user_text="привет",
        safety_provider=ScriptedWelfare(), welfare_decider=decider,
    )
    assert decider.states == []
    assert await _rows(sessionmaker) == []


async def test_a_row_write_failure_never_fails_the_turn(sessionmaker, clock, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("table gone")

    monkeypatch.setattr(welfare_shadow, "WelfareShadow", broken)
    fake = await _run_shadowed(
        sessionmaker, clock,
        classifier=ScriptedWelfare(level="none", confidence=0.9),
        decider=FakeDecider(),
    )
    assert [m.text for m in fake.sent] == ["Не оправдание. Что сделаешь за час?"]


# --- wiring and visibility ---


def test_the_chat_turn_is_handed_the_decider():
    from app.tg import router as router_module

    assert "welfare_decider=welfare_decider" in inspect.getsource(router_module.build_router)
    from app import main as main_module

    assert "welfare_decider=build_welfare_decider(settings, llm_client)" in inspect.getsource(
        main_module
    )


async def test_the_debug_role_reads_the_comparison(sessionmaker):
    async with sessionmaker() as session:
        session.add(
            WelfareShadow(
                local_date=datetime.date(2026, 10, 18),
                primary_outcome="ok",
                primary_level="scene",
                primary_confidence=0.8,
                primary_ms=900,
                shadow_model=MODEL,
                shadow_outcome="ok",
                shadow_level="real",
                shadow_confidence=0.6,
                shadow_p_real=0.6,
                shadow_ms=140,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        await session.execute(text("set local role anchor_debug"))
        row = (
            await session.execute(
                text("select primary_level, shadow_level, shadow_p_real from debug.welfare_shadow")
            )
        ).one()
    assert row == ("scene", "real", 0.6)


async def test_a_level_without_an_ok_outcome_is_refused(sessionmaker):
    from sqlalchemy.exc import IntegrityError

    async with sessionmaker() as session:
        session.add(
            WelfareShadow(
                local_date=datetime.date(2026, 10, 18),
                primary_outcome="timeout",
                primary_level="none",
                primary_ms=8000,
                shadow_model=MODEL,
                shadow_outcome="error",
                shadow_ms=10,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


class _RaisingClient:
    def __init__(self, exc):
        self.exc = exc

    async def post(self, url, *, cast_to, body):
        raise self.exc


def _sdk_errors():
    import httpx2
    import openai

    request = httpx2.Request("POST", DECISIONS_URL)
    return [
        (openai.APIConnectionError(request=request), LLMRetryableError),
        (
            openai.RateLimitError(
                "rate", response=httpx2.Response(429, request=request), body=None
            ),
            LLMRetryableError,
        ),
        (
            openai.BadRequestError(
                "bad", response=httpx2.Response(400, request=request), body=None
            ),
            LLMError,
        ),
    ]


@pytest.mark.parametrize(("exc", "expected"), _sdk_errors())
async def test_sdk_failures_map_like_a_chat_call(exc, expected):
    decider = OpenRouterDecisionProvider(MODEL, "deny", _RaisingClient(exc))
    with pytest.raises(expected):
        await _decide(decider)

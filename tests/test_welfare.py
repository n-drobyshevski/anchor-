"""The welfare check (phase-2 plan sections 10, 13 and 14).

The check exists to catch the one case the persona would handle badly.
So the tests that matter most are the ones about it *not* firing
(`scene` and `none` pass through, a timeout sends the normal reply) and
about what it leaves behind when it does (nothing reaches the extractor,
a summary, memory or the journal).
"""

from __future__ import annotations

import asyncio
import inspect

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import select

from app.config import Settings
from app.core import welfare
from app.core.scene import summarizable_messages
from app.db.models import Job, Message, SpendLedger, StateChange, TelegramUpdate, UserState
from app.tg import welfare as welfare_ui
from app.tg.router import build_router
from conftest import FakeLLMProvider, FakeSession

pytestmark = pytest.mark.asyncio

CHAT_ID = 4242
TIMEZONE = "Europe/Paris"
DISTRESS = "стоп, мне реально хреново, это не игра"


def _settings(**kw) -> Settings:
    base = {"DAILY_USD_CAP": 10.0, "WELFARE_MIN_CONF": 0.6}
    base.update(kw)
    return Settings(**base)


class ScriptedWelfare(FakeLLMProvider):
    """A cheap provider that answers the classifier, then the reply call.

    Two different prompts arrive on the same provider, so it dispatches
    on the system prompt rather than on call order -- which keeps the
    test honest if the order ever changes.
    """

    def __init__(self, level="none", confidence=0.0, reply="Я рядом. Всё на паузе.", **kw):
        super().__init__(**kw)
        self.verdict_json = f'{{"level": "{level}", "confidence": {confidence}}}'
        self.reply = reply

    async def complete(self, messages, *, conversation_id, json_schema=None):
        response = await super().complete(
            messages, conversation_id=conversation_id,
            json_schema=json_schema,
        )
        system = messages[0].content
        text = self.verdict_json if system.startswith("Определи") else self.reply
        return type(response)(text=text, usage=response.usage, model=response.model)


async def _seed(sessionmaker, *update_ids: int, **state):
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE, **state))
        await session.commit()
        for update_id in update_ids:
            session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()
    fake = FakeSession()
    return Bot(token="123456:TESTTOKEN", session=fake), fake


async def _run(sessionmaker, bot, cheap, clock, *, update_id=1, text=DISTRESS, main=None, settings=None):
    from app.core import turn

    await turn.run(
        sessionmaker,
        bot,
        settings or _settings(),
        main or FakeLLMProvider(text="Не оправдание. Что сделаешь за час?"),
        clock=clock,
        chat_id=CHAT_ID,
        update_id=update_id,
        user_text=text,
        safety_provider=cheap,
    )


async def _state(sessionmaker) -> UserState:
    async with sessionmaker() as session:
        return await session.get(UserState, 1)


# --- a real verdict ---


async def test_real_distress_discards_the_persona_reply_and_pauses(sessionmaker, clock):
    bot, fake = await _seed(sessionmaker, 1)
    persona = FakeLLMProvider(text="Не оправдание. Что сделаешь за час?")

    await _run(
        sessionmaker, bot,
        ScriptedWelfare(level="real", confidence=0.9, reply="Я рядом. Всё на паузе."),
        clock,
        main=persona,
    )

    assert persona.calls == 1, "the persona reply was generated..."
    sent = [m.text for m in fake.sent]
    assert "Не оправдание. Что сделаешь за час?" not in sent, "...and never sent"
    assert sent == ["Я рядом. Всё на паузе."]

    state = await _state(sessionmaker)
    assert state.persona_active is False

    async with sessionmaker() as session:
        stored = (await session.execute(select(Message))).scalars().all()
    assert all("Не оправдание" not in m.content for m in stored), "nor stored"


async def test_the_welfare_reply_carries_both_buttons(sessionmaker, clock):
    bot, fake = await _seed(sessionmaker, 1)
    await _run(sessionmaker, bot, ScriptedWelfare(level="real", confidence=0.9), clock)

    labels = [b.text for row in fake.sent[0].reply_markup.inline_keyboard for b in row]
    assert labels == [welfare_ui.RESUME, welfare_ui.STAY]


async def test_the_discarded_generation_is_still_ledgered(sessionmaker, clock):
    """Plan section 10: "don't send it, don't store it; its cost is
    still ledgered". The money left whether the words did or not."""
    bot, fake = await _seed(sessionmaker, 1)
    await _run(sessionmaker, bot, ScriptedWelfare(level="real", confidence=0.9), clock)

    async with sessionmaker() as session:
        rows = (await session.execute(select(SpendLedger))).scalars().all()
    categories = sorted(r.category for r in rows)
    assert categories == ["chat", "welfare", "welfare"], (
        "the discarded persona call, the classifier, and the welfare reply"
    )


async def test_pausing_is_audited_as_welfare(sessionmaker, clock):
    bot, fake = await _seed(sessionmaker, 1)
    await _run(sessionmaker, bot, ScriptedWelfare(level="real", confidence=0.9), clock)

    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(StateChange).where(StateChange.field == "persona_active")))
            .scalars().all()
        )
    assert len(rows) == 1
    assert rows[0].new_value == "False"
    assert rows[0].source == "welfare"


async def test_a_low_confidence_real_verdict_passes_through(sessionmaker, clock):
    """WELFARE_MIN_CONF is a floor, not decoration."""
    bot, fake = await _seed(sessionmaker, 1)
    await _run(sessionmaker, bot, ScriptedWelfare(level="real", confidence=0.3), clock)

    assert fake.sent[0].text == "Не оправдание. Что сделаешь за час?"
    assert (await _state(sessionmaker)).persona_active is True


# --- privacy (plan sections 10 and 13) ---


async def test_a_welfare_turn_never_enqueues_the_extractor(sessionmaker, clock):
    bot, fake = await _seed(sessionmaker, 1)
    await _run(sessionmaker, bot, ScriptedWelfare(level="real", confidence=0.9), clock)

    async with sessionmaker() as session:
        jobs = (await session.execute(select(Job))).scalars().all()
    assert [j for j in jobs if j.kind == "extract"] == []


async def test_both_halves_of_a_welfare_exchange_are_excluded_from_summaries(sessionmaker, clock):
    """The reply is written kind='welfare' from the start; the message
    that triggered it was stored as ordinary chat before anyone knew,
    and must be retagged or it lands in the next scene summary."""
    bot, fake = await _seed(sessionmaker, 1)
    await _run(sessionmaker, bot, ScriptedWelfare(level="real", confidence=0.9), clock)

    async with sessionmaker() as session:
        rows = (await session.execute(select(Message))).scalars().all()
        scene_id = rows[0].scene_id
        visible = await summarizable_messages(session, scene_id)

    assert {r.kind for r in rows} == {"welfare"}
    assert all(r.ooc is True for r in rows)
    assert visible == [], "nothing from this exchange may reach a summary"


async def test_a_welfare_exchange_is_excluded_from_the_persona_transcript(sessionmaker, clock):
    from app.core.prompt import build_messages

    bot, fake = await _seed(sessionmaker, 1)
    await _run(sessionmaker, bot, ScriptedWelfare(level="real", confidence=0.9), clock)

    async with sessionmaker() as session:
        messages = await build_messages(
            session, clock=clock, timezone=TIMEZONE, intensity=3, user_text="новое",
            update_id=999, transcript_turns=30,
        )
    blob = "\n".join(m.content for m in messages)
    assert DISTRESS not in blob
    assert "Я рядом" not in blob


# --- scene / none / failure all pass through ---


@pytest.mark.parametrize("level", ["none", "scene"])
async def test_scene_and_none_pass_through(sessionmaker, level, clock):
    """An in-game complaint gets a normal in-character reply."""
    bot, fake = await _seed(sessionmaker, 1)
    await _run(
        sessionmaker, bot,
        ScriptedWelfare(level=level, confidence=0.95),
        clock,
        text="это слишком сложно, ну",
    )

    assert fake.sent[0].text == "Не оправдание. Что сделаешь за час?"
    assert (await _state(sessionmaker)).persona_active is True


async def test_a_classifier_timeout_sends_the_normal_reply(sessionmaker, clock):
    """Plan section 10: fail open for chat."""

    class SlowClassifier(FakeLLMProvider):
        async def complete(self, messages, *, conversation_id, json_schema=None):
            await asyncio.sleep(5)
            raise AssertionError("should have been cancelled")

    bot, fake = await _seed(sessionmaker, 1)
    await _run(
        sessionmaker, bot, SlowClassifier(), clock,
        settings=_settings(WELFARE_TIMEOUT_SECONDS=0.05),
    )

    assert fake.sent[0].text == "Не оправдание. Что сделаешь за час?"
    assert (await _state(sessionmaker)).persona_active is True


async def test_a_classifier_error_sends_the_normal_reply(sessionmaker, clock):
    class BrokenClassifier(FakeLLMProvider):
        async def complete(self, messages, *, conversation_id, json_schema=None):
            raise RuntimeError("upstream is on fire")

    bot, fake = await _seed(sessionmaker, 1)
    await _run(sessionmaker, bot, BrokenClassifier(), clock)

    assert fake.sent[0].text == "Не оправдание. Что сделаешь за час?"
    assert (await _state(sessionmaker)).persona_active is True


async def test_unparseable_classifier_output_passes_through(sessionmaker, clock):
    bot, fake = await _seed(sessionmaker, 1)
    await _run(sessionmaker, bot, FakeLLMProvider(text="кажется, всё в порядке"), clock)

    assert fake.sent[0].text == "Не оправдание. Что сделаешь за час?"
    assert (await _state(sessionmaker)).persona_active is True


async def test_a_neutral_turn_runs_no_classifier(sessionmaker, clock):
    bot, fake = await _seed(sessionmaker, 1, persona_active=False)
    cheap = ScriptedWelfare(level="real", confidence=0.9)

    await _run(sessionmaker, bot, cheap, clock, text="привет")

    assert cheap.calls == 0, "the persona is already off; there is nothing to drop"


async def test_a_pause_word_runs_no_classifier(sessionmaker, clock):
    bot, fake = await _seed(sessionmaker, 1)
    cheap = ScriptedWelfare(level="real", confidence=0.9)

    await _run(sessionmaker, bot, cheap, clock, text="пурпурный")

    assert cheap.calls == 0


# --- the buttons ---


def _callback_update(update_id: int, data: str, *, message_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Test"},
            "chat_instance": "ci",
            "data": data,
            "message": {
                "message_id": message_id,
                "date": 0,
                "chat": {"id": CHAT_ID, "type": "private"},
                "text": "Я рядом. Всё на паузе.",
            },
        },
    }


def _build_dp(sessionmaker):
    fake = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, _settings(), FakeLLMProvider(), FakeLLMProvider()))
    return dp, bot, fake


async def test_resume_turns_the_persona_back_on(sessionmaker):
    await _seed(sessionmaker, 1, persona_active=False)
    dp, bot, fake = _build_dp(sessionmaker)

    await dp.feed_update(bot, Update.model_validate(_callback_update(1, "w:resume"), context={"bot": bot}))

    state = await _state(sessionmaker)
    assert state.persona_active is True
    assert any("Возвращаюсь" in m.text for m in fake.sent)
    assert fake.edits[-1].reply_markup is None


async def test_resume_is_audited_as_a_button(sessionmaker):
    await _seed(sessionmaker, 1, persona_active=False)
    dp, bot, fake = _build_dp(sessionmaker)

    await dp.feed_update(bot, Update.model_validate(_callback_update(1, "w:resume"), context={"bot": bot}))

    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(StateChange).where(StateChange.field == "persona_active")))
            .scalars().all()
        )
    assert rows[-1].source == "button", "distinguishable from /in in the audit log"


async def test_stay_removes_the_buttons_and_changes_nothing(sessionmaker):
    await _seed(sessionmaker, 1, persona_active=False)
    dp, bot, fake = _build_dp(sessionmaker)

    await dp.feed_update(bot, Update.model_validate(_callback_update(1, "w:stay"), context={"bot": bot}))

    assert (await _state(sessionmaker)).persona_active is False
    assert fake.edits[-1].reply_markup is None
    assert welfare_ui.STAY_ACK in fake.edits[-1].text
    assert len(fake.answered) == 1


async def test_a_replayed_resume_turns_it_on_once(sessionmaker):
    await _seed(sessionmaker, 1, persona_active=False)
    dp, bot, fake = _build_dp(sessionmaker)

    for _ in range(2):
        await dp.feed_update(
            bot, Update.model_validate(_callback_update(1, "w:resume"), context={"bot": bot})
        )

    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(StateChange).where(StateChange.field == "persona_active")))
            .scalars().all()
        )
    assert len(rows) == 1


# --- wiring ---


async def test_build_router_threads_the_safety_provider_into_every_turn(sessionmaker):
    """safety_provider defaults to None so pre-2e tests keep their
    three-argument call. That default must never be what production
    gets: every turn.run() in the router has to pass it explicitly."""
    from app.tg import router as router_module

    source = inspect.getsource(router_module.build_router)
    run_calls = source.count("await turn.run(")
    threaded = source.count("safety_provider=safety_provider")
    assert run_calls > 0
    assert threaded == run_calls, (
        f"{run_calls} turn.run() call sites but only {threaded} pass safety_provider"
    )


async def test_main_builds_a_dispatcher_with_the_safety_provider(sessionmaker):
    """H2: the router's welfare provider is the safety model, not the
    cheap one. Pinned by source because the argument is positional --
    handing it the wrong provider would still run, just on the wrong
    model, which is exactly the failure this milestone is about."""
    from app import main as main_module

    source = inspect.getsource(main_module)
    # Web-chat plan track 2 appended trailing `hub` and `code_store`
    # arguments (both None unless WEB_UI_ENABLED) to this same call; the
    # assertion below is widened to match without weakening what it
    # actually pins -- the first five positional arguments, in this
    # order, are what matters for H2 (a wrong-order `hub` would still be
    # a bug, but not this one).
    # The connector's C2 appended `claude_pending` by keyword after them.
    assert (
        "build_dispatcher(\n        sessionmaker, settings, provider, safety_provider, clock, hub, code_store,"
        in source
    )
    assert (
        "provider, cheap_provider, safety_provider, llm_client = build_providers(settings)"
        in source
    )


# --- the classifier's own contract ---


@pytest.mark.parametrize(
    "raw,level,confidence",
    [
        ('{"level": "real", "confidence": 0.9}', "real", 0.9),
        ('```json\n{"level": "scene", "confidence": 0.4}\n```', "scene", 0.4),
        ('Вот: {"level": "none", "confidence": 0.1} — всё.', "none", 0.1),
    ],
)
async def test_parse_reads_json_through_the_usual_wrappers(sessionmaker, raw, level, confidence):
    verdict = welfare.parse(raw)
    assert verdict.level == level
    assert verdict.confidence == pytest.approx(confidence)


@pytest.mark.parametrize(
    "raw",
    [
        "кажется, всё в порядке",
        '{"level": "panic", "confidence": 1}',      # outside the enum
        '{"level": "real", "confidence": true}',     # bool is an int subclass
        '{"level": "real"}',                         # missing confidence
        '["real", 0.9]',                             # not an object
        "",
    ],
)
async def test_anything_unexpected_fails_open_to_none(sessionmaker, raw):
    """Fail open: a classifier that cannot be understood must not be
    able to silence the bot."""
    assert welfare.parse(raw).level == welfare.NONE


async def test_is_real_needs_both_the_level_and_the_confidence(sessionmaker):
    settings = _settings(WELFARE_MIN_CONF=0.6)
    assert welfare.Verdict("real", 0.6).is_real(settings) is True
    assert welfare.Verdict("real", 0.59).is_real(settings) is False
    assert welfare.Verdict("scene", 1.0).is_real(settings) is False
    assert welfare.Verdict().is_real(settings) is False


async def test_the_plan_prompts_are_carried_verbatim(sessionmaker):
    """Both prompts are specified word for word in plan section 10, and
    the welfare one carries helpline numbers -- getting those wrong is
    not a tuning mistake."""
    assert welfare.CLASSIFIER_PROMPT.startswith("Определи, испытывает ли пользователь")
    assert "При любом упоминании самоповреждения — `real`" in welfare.CLASSIFIER_PROMPT
    assert "Если сомневаешься между scene и real — выбирай real" in welfare.CLASSIFIER_PROMPT

    assert welfare.WELFARE_PROMPT.startswith("Роль Anchor выключена.")
    assert "3114" in welfare.WELFARE_PROMPT
    assert "112" in welfare.WELFARE_PROMPT
    assert "Никаких заданий, давления и прозвищ" in welfare.WELFARE_PROMPT
    # The fallback must carry the numbers too: it is what goes out when
    # the model call for the real reply fails, and by then the persona
    # is already off.
    assert "3114" in welfare.FALLBACK_REPLY and "112" in welfare.FALLBACK_REPLY


async def test_a_failed_welfare_reply_still_sends_something_with_the_numbers(sessionmaker, clock):
    """The persona is off by this point. Silence is not an option."""

    class ClassifiesThenBreaks(FakeLLMProvider):
        async def complete(self, messages, *, conversation_id, json_schema=None):
            if messages[0].content.startswith("Определи"):
                response = await super().complete(
                    messages, conversation_id=conversation_id, json_schema=json_schema
                )
                return type(response)(
                    text='{"level": "real", "confidence": 0.95}',
                    usage=response.usage,
                    model=response.model,
                )
            raise RuntimeError("the reply call fell over")

    bot, fake = await _seed(sessionmaker, 1)
    await _run(sessionmaker, bot, ClassifiesThenBreaks(), clock)

    assert fake.sent[0].text == welfare.FALLBACK_REPLY
    assert "3114" in fake.sent[0].text
    assert (await _state(sessionmaker)).persona_active is False
    labels = [b.text for row in fake.sent[0].reply_markup.inline_keyboard for b in row]
    assert labels == [welfare_ui.RESUME, welfare_ui.STAY]


async def test_the_classifier_sees_recent_context_and_the_new_text(sessionmaker):
    rows = [
        type("M", (), {"role": "user", "content": "давай ещё раз"})(),
        type("M", (), {"role": "assistant", "content": "три пункта"})(),
    ]
    messages = welfare.build_messages(rows, "мне реально плохо")

    assert [m.role for m in messages] == ["system", "user"]
    assert messages[0].content == welfare.CLASSIFIER_PROMPT
    assert messages[1].content == (
        "Пользователь: давай ещё раз\nAnchor: три пункта\nПользователь: мне реально плохо"
    )

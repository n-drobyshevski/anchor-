"""app/core/turn.py's P4 wiring: the intent call inside the welfare
gather, gated by PLANNER_INTENT, dropped when welfare fires, and
producing exactly the same confirm card /task and /event do.

Shaped like tests/test_welfare.py: a `ScriptedSafety` FakeLLMProvider
dispatches on the system prompt, because welfare.classify and
planner_intent.detect share one `safety_provider` in the same
asyncio.gather (app/core/turn.py).
"""

from __future__ import annotations

import datetime

import pytest
from aiogram import Bot
from sqlalchemy import select

from app.config import Settings
from app.core import welfare
from app.planner import actions as planner_actions
from app.db.models import PlannerAction, PlannerCredential, TelegramUpdate, UserState
from conftest import FakeLLMProvider, FakeSession

pytestmark = pytest.mark.asyncio

CHAT_ID = 4242
TZ = "Europe/Paris"

_INTENT_HIT_TEXT = "напомни завтра купить молоко"
_ORDINARY_TEXT = "как прошёл твой день"

_TASK_JSON = (
    '{"kind": "task", "title": "Купить молоко", "due_date": "2026-09-24", '
    '"date": null, "start_time": null, "end_time": null, "all_day": false}'
)
_NONE_JSON = (
    '{"kind": "none", "title": null, "due_date": null, "date": null, '
    '"start_time": null, "end_time": null, "all_day": false}'
)


class ScriptedSafety(FakeLLMProvider):
    """Answers welfare.classify's call and planner_intent.detect's call
    on the same provider, dispatching on the system prompt -- both run
    concurrently on `safety_provider` in app/core/turn.py's gather."""

    def __init__(
        self,
        *,
        welfare_level="none",
        welfare_conf=0.0,
        intent_json=_NONE_JSON,
        welfare_reply="Я рядом. Всё на паузе.",
        **kw,
    ):
        super().__init__(**kw)
        self.verdict_json = f'{{"level": "{welfare_level}", "confidence": {welfare_conf}}}'
        self.intent_json = intent_json
        self.welfare_reply = welfare_reply

    async def complete(self, messages, *, conversation_id, json_schema=None):
        response = await super().complete(
            messages, conversation_id=conversation_id, json_schema=json_schema
        )
        system = messages[0].content
        if system.startswith("Определи, испытывает"):
            text = self.verdict_json
        elif system.startswith("Пользователь пишет обычное сообщение"):
            text = self.intent_json
        else:
            # run_welfare_turn's own reply call, on a real verdict.
            text = self.welfare_reply
        return type(response)(text=text, usage=response.usage, model=response.model)


def _settings(**kw) -> Settings:
    base = {"DAILY_USD_CAP": 10.0, "PLANNER_ENABLED": True, "PLANNER_INTENT": True}
    base.update(kw)
    return Settings(**base)


async def _seed(sessionmaker, update_id: int = 1) -> tuple[Bot, FakeSession]:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TZ, persona_active=True))
        session.add(
            PlannerCredential(
                id=1,
                access_token="tok",
                refresh_token="ref",
                expires_at=datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc),
                status="active",
            )
        )
        await session.commit()
        session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()
    fake = FakeSession()
    return Bot(token="123456:TESTTOKEN", session=fake), fake


async def _run(sessionmaker, bot, safety, clock, *, text, settings=None, main=None):
    from app.core import turn

    await turn.run(
        sessionmaker,
        bot,
        settings or _settings(),
        main or FakeLLMProvider(text="Обычный ответ."),
        clock=clock,
        chat_id=CHAT_ID,
        update_id=1,
        user_text=text,
        safety_provider=safety,
    )


async def _pending_actions(sessionmaker) -> list[PlannerAction]:
    async with sessionmaker() as session:
        return await planner_actions.pending(session)


# --- prefilter miss: zero provider calls for the intent half -------------


async def test_prefilter_miss_never_calls_the_intent_schema(sessionmaker, clock):
    bot, fake = await _seed(sessionmaker)
    safety = ScriptedSafety()
    await _run(sessionmaker, bot, safety, clock, text=_ORDINARY_TEXT)

    # Only welfare's own call went out -- one gather branch, not two.
    assert safety.calls == 1
    assert safety.received_schemas[0].name == "anchor_welfare"
    assert await _pending_actions(sessionmaker) == []


# --- welfare wins: the intent result is dropped ---------------------------


async def test_welfare_real_drops_a_would_be_intent_card(sessionmaker, clock):
    bot, fake = await _seed(sessionmaker)
    safety = ScriptedSafety(welfare_level="real", welfare_conf=0.9, intent_json=_TASK_JSON)
    persona = FakeLLMProvider(text="Персонажный ответ.")

    await _run(sessionmaker, bot, safety, clock, text=_INTENT_HIT_TEXT, main=persona)

    assert await _pending_actions(sessionmaker) == []
    # The persona's own reply was discarded too, same as an ordinary
    # welfare-real turn (tests/test_welfare.py).
    sent_texts = [m.text for m in fake.sent]
    assert "Персонажный ответ." not in sent_texts


# --- an ordinary hit: a card is produced -----------------------------------


async def test_an_intent_hit_produces_exactly_one_pending_card(sessionmaker, clock):
    bot, fake = await _seed(sessionmaker)
    safety = ScriptedSafety(intent_json=_TASK_JSON)
    persona = FakeLLMProvider(text="Окей.")

    await _run(sessionmaker, bot, safety, clock, text=_INTENT_HIT_TEXT, main=persona)

    pending = await _pending_actions(sessionmaker)
    assert len(pending) == 1
    assert pending[0].kind == planner_actions.CREATE_TASK
    assert pending[0].payload["title"] == "Купить молоко"

    # The persona's own reply went out first, then the confirm card as
    # a follow-up -- never merged into one message.
    assert fake.sent[0].text == "Окей."
    assert len(fake.sent) == 2
    assert fake.sent[1].reply_markup is not None


async def test_an_ordinary_reply_never_claims_the_write_is_done(sessionmaker, clock):
    """The persona reply is generated from the prompt built *before* the
    intent call resolves, so it can say nothing about a card that does
    not exist yet -- this is what keeps it from claiming completion."""
    bot, fake = await _seed(sessionmaker)
    safety = ScriptedSafety(intent_json=_TASK_JSON)
    persona = FakeLLMProvider(text="Окей, услышала.")

    await _run(sessionmaker, bot, safety, clock, text=_INTENT_HIT_TEXT, main=persona)

    assert fake.sent[0].text == "Окей, услышала."


# --- flag off: nothing happens, not even the provider call ---------------


async def test_flag_off_produces_no_card_and_no_provider_call(sessionmaker, clock):
    bot, fake = await _seed(sessionmaker)
    safety = ScriptedSafety(intent_json=_TASK_JSON)
    settings = _settings(PLANNER_INTENT=False)

    await _run(sessionmaker, bot, safety, clock, text=_INTENT_HIT_TEXT, settings=settings)

    assert safety.calls == 1  # welfare only
    assert safety.received_schemas[0].name == "anchor_welfare"
    assert await _pending_actions(sessionmaker) == []

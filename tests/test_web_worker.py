"""Web rows through the real worker/router (web-chat plan track 1, design
sections 2 and 11 -- "the key one").

Pushes synthetic web updates through `process_one_update(web_bot=...)`,
the same entry point app/worker.py's claim loop uses, and checks every
safety behaviour the design claims is inherited unchanged: a plain
reply reaches the hub and queues an extract job, a pause word and
/out//in work, the spend cap gives its canned reply, a welfare verdict
produces `w:` buttons and a safety_event row, a bypassed /export or
/delete does not call the real export/purge code, web and Telegram rows
interleave in arrival order, a web row with no web_bot fails cleanly,
and the Command filter works on a synthetic message with no `entities`.
"""

from __future__ import annotations

import datetime
import decimal

import pytest
from aiogram import Bot, Dispatcher
from sqlalchemy import select, update as sql_update

from app.config import Settings
from app.core import export as export_module
from app.core import purge as purge_module
from app.core.clock import local_date
from app.db import queue
from app.db.models import Job, Message, SafetyEvent, SpendLedger, TelegramUpdate, UserState
from app.tg.router import build_router
from app.web.hub import WebHub
from app.web.ingress import build_message_update, send_text
from app.web.sink import make_web_bot
from app.worker import WebDisabled, process_one_update
from conftest import FakeLLMProvider, FakeSession

pytestmark = pytest.mark.asyncio

CHAT_ID = 4242
TIMEZONE = "Europe/Paris"


def _settings(**kw) -> Settings:
    base = {"ALLOWED_CHAT_ID": CHAT_ID, "DAILY_USD_CAP": 10.0, "WELFARE_MIN_CONF": 0.6}
    base.update(kw)
    return Settings(**base)


async def _seed_state(sessionmaker, **state) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE, **state))
        await session.commit()


def _telegram_bot() -> tuple[Bot, FakeSession]:
    fake = FakeSession()
    return Bot(token="123456:TESTTOKEN", session=fake), fake


def _telegram_update_payload(update_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Test"},
            "text": text,
        },
    }


async def _hub_texts(hub: WebHub) -> list[str]:
    sub = hub.subscribe(last_event_id=0)
    sub.close()
    return [e.data["text"] for e in sub.backlog if e.event == "message"]


class ScriptedWelfare(FakeLLMProvider):
    """Answers the classifier prompt and the reply prompt differently,
    dispatching on the system prompt exactly like tests/test_welfare.py's
    own ScriptedWelfare -- duplicated in miniature here rather than
    imported, so this file stays runnable on its own."""

    def __init__(self, level="none", confidence=0.0, reply="Я рядом. Всё на паузе.", **kw):
        super().__init__(**kw)
        self.verdict_json = f'{{"level": "{level}", "confidence": {confidence}}}'
        self.reply = reply

    async def complete(self, messages, *, conversation_id, json_schema=None):
        response = await super().complete(
            messages, conversation_id=conversation_id, json_schema=json_schema
        )
        system = messages[0].content
        text = self.verdict_json if system.startswith("Определи") else self.reply
        return type(response)(text=text, usage=response.usage, model=response.model)


def _build_router_dp(sessionmaker, settings, provider, safety_provider=None) -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, settings, provider, safety_provider))
    return dp


# --- plain text: hub delivery + extract job ---


async def test_plain_text_reaches_hub_and_queues_extract_job(sessionmaker, clock):
    await _seed_state(sessionmaker)
    settings = _settings()
    hub = WebHub()
    web_bot = make_web_bot("123456:TEST", hub)
    telegram_bot, _ = _telegram_bot()
    provider = FakeLLMProvider(text="Принято.")
    dp = _build_router_dp(sessionmaker, settings, provider)

    async with sessionmaker() as session:
        update_id = await send_text(session, settings=settings, text="привет", client_key="k1")

    processed = await process_one_update(sessionmaker, dp, telegram_bot, clock, web_bot)
    assert processed is True

    texts = await _hub_texts(hub)
    assert texts == ["Принято."]

    async with sessionmaker() as session:
        row = await session.get(TelegramUpdate, update_id)
        assert row.status == "done"
        jobs = (await session.execute(select(Job).where(Job.kind == "extract"))).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].payload["update_id"] == update_id
    assert jobs[0].dedup_key == f"extract:{update_id}"

    await telegram_bot.session.close()
    await web_bot.session.close()


# --- pause word ---


async def test_pause_word_over_web_hard_pauses(sessionmaker, clock):
    await _seed_state(sessionmaker)
    settings = _settings()
    hub = WebHub()
    web_bot = make_web_bot("123456:TEST", hub)
    telegram_bot, _ = _telegram_bot()
    provider = FakeLLMProvider(text="не должно быть отправлено")
    dp = _build_router_dp(sessionmaker, settings, provider)

    async with sessionmaker() as session:
        await send_text(session, settings=settings, text="пурпурный", client_key="k1")

    await process_one_update(sessionmaker, dp, telegram_bot, clock, web_bot)

    assert provider.calls == 0  # no LLM call for a hard pause word
    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
    assert state.persona_active is False

    texts = await _hub_texts(hub)
    assert "не должно быть отправлено" not in texts

    await telegram_bot.session.close()
    await web_bot.session.close()


# --- /out and /in ---


async def test_out_and_in_commands_over_web(sessionmaker, clock):
    await _seed_state(sessionmaker)
    settings = _settings()
    hub = WebHub()
    web_bot = make_web_bot("123456:TEST", hub)
    telegram_bot, _ = _telegram_bot()
    provider = FakeLLMProvider()
    dp = _build_router_dp(sessionmaker, settings, provider)

    async with sessionmaker() as session:
        await send_text(session, settings=settings, text="/out", client_key="k1")
    await process_one_update(sessionmaker, dp, telegram_bot, clock, web_bot)

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
    assert state.persona_active is False

    async with sessionmaker() as session:
        await send_text(session, settings=settings, text="/in", client_key="k2")
    await process_one_update(sessionmaker, dp, telegram_bot, clock, web_bot)

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
    assert state.persona_active is True

    await telegram_bot.session.close()
    await web_bot.session.close()


# --- spend cap ---


async def test_spend_cap_gives_canned_reply_over_web(sessionmaker, clock):
    await _seed_state(sessionmaker)
    settings = _settings(DAILY_USD_CAP=0.50)
    # The cap is checked against the user's local day, not UTC's.
    today = local_date(clock, TIMEZONE)
    async with sessionmaker() as session:
        session.add(SpendLedger(local_date=today, category="chat", usd_cost=decimal.Decimal("0.50")))
        await session.commit()

    hub = WebHub()
    web_bot = make_web_bot("123456:TEST", hub)
    telegram_bot, _ = _telegram_bot()
    provider = FakeLLMProvider()
    dp = _build_router_dp(sessionmaker, settings, provider)

    async with sessionmaker() as session:
        await send_text(session, settings=settings, text="ещё вопрос", client_key="k1")
    await process_one_update(sessionmaker, dp, telegram_bot, clock, web_bot)

    assert provider.calls == 0
    from app.core.turn import CAP_REPLY_TEXT

    texts = await _hub_texts(hub)
    assert texts == [CAP_REPLY_TEXT]

    await telegram_bot.session.close()
    await web_bot.session.close()


# --- welfare ---


async def test_welfare_real_verdict_gives_w_buttons_and_safety_event(sessionmaker, clock):
    await _seed_state(sessionmaker)
    settings = _settings()
    hub = WebHub()
    web_bot = make_web_bot("123456:TEST", hub)
    telegram_bot, _ = _telegram_bot()
    persona = FakeLLMProvider(text="персональный ответ, который не должен уйти")
    safety = ScriptedWelfare(level="real", confidence=0.9, reply="Я рядом. Всё на паузе.")
    dp = _build_router_dp(sessionmaker, settings, persona, safety)

    async with sessionmaker() as session:
        await send_text(
            session, settings=settings, text="стоп, мне реально хреново", client_key="k1"
        )
    await process_one_update(sessionmaker, dp, telegram_bot, clock, web_bot)

    sub = hub.subscribe(last_event_id=0)
    sub.close()
    message_events = [e for e in sub.backlog if e.event == "message"]
    assert len(message_events) == 1
    event = message_events[0]
    assert event.data["text"] == "Я рядом. Всё на паузе."
    assert event.data["kind"] == "welfare"
    keyboard_data = {b["data"] for row in event.data["keyboard"] for b in row}
    assert keyboard_data == {"w:resume", "w:stay"}

    async with sessionmaker() as session:
        events = (await session.execute(select(SafetyEvent))).scalars().all()
    assert any(e.kind == "welfare" for e in events)

    await telegram_bot.session.close()
    await web_bot.session.close()


# --- /export and /delete: layer 2 defense holds even if ingress didn't block ---


async def test_bypassed_export_does_not_call_build_export(sessionmaker, clock, monkeypatch):
    await _seed_state(sessionmaker)
    settings = _settings()
    hub = WebHub()
    web_bot = make_web_bot("123456:TEST", hub)
    telegram_bot, _ = _telegram_bot()
    provider = FakeLLMProvider()
    dp = _build_router_dp(sessionmaker, settings, provider)

    called = False

    async def _spy(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("build_export must never run for a web-sink update")

    monkeypatch.setattr(export_module, "build_export", _spy)

    # Bypasses app/web/ingress.py's own block on purpose, to prove the
    # router's is_web_sink guard is an independent layer (design's
    # adversarial review, finding 2) -- not exercised through send_text,
    # which would refuse this before it ever reached the queue.
    async with sessionmaker() as session:
        await queue.enqueue_web(
            session, lambda update_id: build_message_update(update_id, "/export", settings), "k1"
        )
    processed = await process_one_update(sessionmaker, dp, telegram_bot, clock, web_bot)
    assert processed is True
    assert called is False

    texts = await _hub_texts(hub)
    from app.tg.router import WEB_ONLY_REPLY

    assert texts == [WEB_ONLY_REPLY]

    await telegram_bot.session.close()
    await web_bot.session.close()


async def test_bypassed_delete_does_not_call_purge(sessionmaker, clock, monkeypatch):
    await _seed_state(sessionmaker)
    settings = _settings()
    hub = WebHub()
    web_bot = make_web_bot("123456:TEST", hub)
    telegram_bot, _ = _telegram_bot()
    provider = FakeLLMProvider()
    dp = _build_router_dp(sessionmaker, settings, provider)

    called = False

    async def _spy(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("delete_everything must never run for a web-sink update")

    monkeypatch.setattr(purge_module, "delete_everything", _spy)

    async with sessionmaker() as session:
        await queue.enqueue_web(
            session, lambda update_id: build_message_update(update_id, "/delete", settings), "k1"
        )
    processed = await process_one_update(sessionmaker, dp, telegram_bot, clock, web_bot)
    assert processed is True
    assert called is False

    texts = await _hub_texts(hub)
    from app.tg.router import WEB_ONLY_REPLY

    assert texts == [WEB_ONLY_REPLY]

    # No delete-confirm keyboard was ever sent, so there is nothing to
    # press: the allowlist for any message id is empty.
    assert hub.allow_press(-1, "d:yes:1") is False

    await telegram_bot.session.close()
    await web_bot.session.close()


# --- interleaving and ordering ---


async def test_web_and_telegram_rows_interleave_in_arrival_order(sessionmaker, clock):
    """The claim-order fix: created_at, not update_id sign, decides FIFO
    across transports (design's second adversarial review, finding 1).
    """
    await _seed_state(sessionmaker)
    settings = _settings()
    hub = WebHub()
    web_bot = make_web_bot("123456:TEST", hub)
    telegram_bot, telegram_fake = _telegram_bot()
    provider = FakeLLMProvider(text="ok")
    dp = _build_router_dp(sessionmaker, settings, provider)

    # The Telegram row is queued (and thus created) first...
    async with sessionmaker() as session:
        await queue.enqueue(session, 5000, _telegram_update_payload(5000, "телеграм первым"))
    # ...the web row second, well after -- even though its update_id is
    # a huge negative number that would sort first under the old
    # update_id-only ordering.
    async with sessionmaker() as session:
        web_update_id = await send_text(
            session, settings=settings, text="веб вторым", client_key="k1"
        )
    # Force the timestamps apart deterministically rather than trusting
    # two sequential `now()` calls to land in different microseconds.
    async with sessionmaker() as session:
        await session.execute(
            sql_update(TelegramUpdate)
            .where(TelegramUpdate.update_id == 5000)
            .values(created_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
        )
        await session.execute(
            sql_update(TelegramUpdate)
            .where(TelegramUpdate.update_id == web_update_id)
            .values(created_at=datetime.datetime(2026, 1, 1, 0, 0, 1, tzinfo=datetime.timezone.utc))
        )
        await session.commit()

    first = await process_one_update(sessionmaker, dp, telegram_bot, clock, web_bot)
    second = await process_one_update(sessionmaker, dp, telegram_bot, clock, web_bot)
    assert (first, second) == (True, True)

    # The Telegram row (created first) was claimed and answered first.
    assert telegram_fake.sent[0].text == "ok"
    async with sessionmaker() as session:
        telegram_row = await session.get(TelegramUpdate, 5000)
        web_row = await session.get(TelegramUpdate, web_update_id)
    assert telegram_row.status == "done"
    assert web_row.status == "done"
    assert telegram_row.locked_at < web_row.locked_at

    await telegram_bot.session.close()
    await web_bot.session.close()


# --- no web_bot: fails cleanly ---


async def test_web_row_without_web_bot_fails_cleanly(sessionmaker, clock):
    await _seed_state(sessionmaker)
    settings = _settings()
    telegram_bot, _ = _telegram_bot()
    provider = FakeLLMProvider()
    dp = _build_router_dp(sessionmaker, settings, provider)

    async with sessionmaker() as session:
        update_id = await send_text(session, settings=settings, text="привет", client_key="k1")

    processed = await process_one_update(sessionmaker, dp, telegram_bot, clock, None)
    assert processed is True  # the row was claimed...

    async with sessionmaker() as session:
        row = await session.get(TelegramUpdate, update_id)
    assert row.error == WebDisabled.__name__ == "WebDisabled"
    assert row.status == "pending"  # ...but failed cleanly, retryable, not crashed
    assert row.attempts == 1

    async with sessionmaker() as session:
        user_rows = (
            await session.execute(select(Message).where(Message.update_id == update_id))
        ).scalars().all()
    assert user_rows == []  # never reached record_inbound's turn machinery

    await telegram_bot.session.close()


# --- Command filter works without entities ---


async def test_command_filter_matches_without_entities_field(sessionmaker):
    payload = build_message_update(-1, "/out", _settings())
    assert "entities" not in payload["message"]
    from aiogram.filters import Command
    from aiogram.types import Message as TgMessage

    message = TgMessage.model_validate(payload["message"])
    result = await Command("out")(message, bot=None)
    assert result is not False

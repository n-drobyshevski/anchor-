"""/start, /state, /out and /in command tests (plan section 12 / 16).

Reuses the FakeSession/FakeLLMProvider fixtures lifted into
tests/conftest.py. /out and /in are router-wiring tests only -- their
behaviour (idempotency, state mutation) is covered in depth against
turn.run_hard_pause/run_resume directly in tests/test_turn.py; these
just confirm build_router() actually reaches them, and reaches them
before the text handler (aiogram is first-match-wins).
"""

from __future__ import annotations

import datetime
import decimal
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import select

from app.config import Settings
from app.core import prompt, turn
from app.core.state import get_state
from app.db.models import Message, SpendLedger, TelegramUpdate, UserState
from app.tg.router import START_TEXT, build_router
from conftest import FakeLLMProvider, FakeSession

TEST_CHAT_ID = 555


def _command_update(update_id: int, command: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": TEST_CHAT_ID, "type": "private"},
            "from": {"id": TEST_CHAT_ID, "is_bot": False, "first_name": "Test"},
            "text": command,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(command)}],
        },
    }


def _search_update(update_id: int, text: str = "/search") -> dict:
    """A /search command update, with `text`'s full string as the
    message (e.g. "/search какая сегодня погода"). The bot_command
    entity covers only the "/search" token, exactly like a real
    Telegram client would mark it up, so CommandObject.args parses out
    the rest as the query.
    """
    command_len = len(text.split(" ", 1)[0])
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": TEST_CHAT_ID, "type": "private"},
            "from": {"id": TEST_CHAT_ID, "is_bot": False, "first_name": "Test"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": command_len}],
        },
    }


def _build_dp(sessionmaker, settings: Settings) -> tuple[Dispatcher, Bot, FakeSession]:
    fake_session = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake_session)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, settings, FakeLLMProvider()))
    return dp, bot, fake_session


async def test_start_replies_with_fixed_text(sessionmaker):
    settings = Settings()
    dp, bot, fake = _build_dp(sessionmaker, settings)

    update = Update.model_validate(_command_update(1, "/start"), context={"bot": bot})
    await dp.feed_update(bot, update)

    assert len(fake.sent) == 1
    assert fake.sent[0].text == START_TEXT
    assert fake.sent[0].chat_id == TEST_CHAT_ID

    await bot.session.close()


async def test_state_reflects_persona_intensity_model_and_timezone(sessionmaker):
    timezone = "America/New_York"
    settings = Settings(LLM_MODEL="thedrummer/cydonia-24b-v4.1", DAILY_USD_CAP=1.00)

    async with sessionmaker() as session:
        session.add(
            UserState(
                id=1,
                chat_id=TEST_CHAT_ID,
                persona_active=True,
                intensity=4,
                timezone=timezone,
            )
        )
        session.add(
            SpendLedger(
                local_date=datetime.datetime.now(ZoneInfo(timezone)).date(),
                category="chat",
                usd_cost=decimal.Decimal("0.123456"),
            )
        )
        session.add(
            SpendLedger(
                local_date=datetime.datetime.now(ZoneInfo(timezone)).date(),
                category="chat",
                usd_cost=decimal.Decimal("0.100000"),
            )
        )
        await session.commit()

    dp, bot, fake = _build_dp(sessionmaker, settings)
    update = Update.model_validate(_command_update(2, "/state"), context={"bot": bot})
    await dp.feed_update(bot, update)

    assert len(fake.sent) == 1
    text = fake.sent[0].text

    assert "вкл" in text
    assert "4/5" in text
    assert timezone in text
    assert "thedrummer/cydonia-24b-v4.1" in text
    assert "1.00" in text  # the cap
    assert "0.22" in text  # sum of the two ledger rows, formatted to 2dp

    # The local time comes from user_state.timezone via zoneinfo, not the
    # server's local time: allow the current or next minute to avoid a
    # flaky boundary crossing.
    now = datetime.datetime.now(ZoneInfo(timezone))
    possible_times = {
        now.strftime("%Y-%m-%d %H:%M"),
        (now + datetime.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M"),
    }
    assert any(t in text for t in possible_times)

    await bot.session.close()


async def test_state_shows_persona_off(sessionmaker):
    settings = Settings()
    async with sessionmaker() as session:
        session.add(
            UserState(
                id=1,
                chat_id=TEST_CHAT_ID,
                persona_active=False,
                intensity=1,
                timezone="Europe/Paris",
            )
        )
        await session.commit()

    dp, bot, fake = _build_dp(sessionmaker, settings)
    update = Update.model_validate(_command_update(3, "/state"), context={"bot": bot})
    await dp.feed_update(bot, update)

    assert "выкл" in fake.sent[0].text
    assert "1/5" in fake.sent[0].text
    # No spend rows inserted: today's spend is zero.
    assert "0.00" in fake.sent[0].text

    await bot.session.close()


async def _seed_user_state(sessionmaker, **kwargs) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=TEST_CHAT_ID, **kwargs))
        await session.commit()


async def _seed_telegram_update(sessionmaker, update_id: int) -> None:
    """The real webhook/worker path always inserts this row before
    dp.feed_update() runs; message.update_id is a foreign key into it."""
    async with sessionmaker() as session:
        session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()


async def test_out_command_reaches_run_hard_pause(sessionmaker):
    await _seed_user_state(sessionmaker, persona_active=True, intensity=3, timezone="Europe/Paris")
    await _seed_telegram_update(sessionmaker, 10)
    settings = Settings()
    dp, bot, fake = _build_dp(sessionmaker, settings)

    update = Update.model_validate(_command_update(10, "/out"), context={"bot": bot})
    await dp.feed_update(bot, update)

    assert fake.sent[0].text == turn.PAUSE_REPLY_TEXT

    async with sessionmaker() as session:
        state = await get_state(session)
    assert state.persona_active is False

    await bot.session.close()


async def test_in_command_reaches_run_resume(sessionmaker):
    await _seed_user_state(sessionmaker, persona_active=False, intensity=3, timezone="Europe/Paris")
    await _seed_telegram_update(sessionmaker, 11)
    settings = Settings()
    dp, bot, fake = _build_dp(sessionmaker, settings)

    update = Update.model_validate(_command_update(11, "/in"), context={"bot": bot})
    await dp.feed_update(bot, update)

    assert fake.sent[0].text == turn.RESUME_REPLY_TEXT

    async with sessionmaker() as session:
        state = await get_state(session)
    assert state.persona_active is True

    await bot.session.close()


async def test_out_and_in_registered_before_text_handler(sessionmaker):
    """aiogram is first-match-wins: /out and /in must be reached as
    commands, not fall through to handle_text (which would try to run
    a full persona/neutral turn and call the LLM provider)."""
    await _seed_user_state(sessionmaker, persona_active=True, intensity=3, timezone="Europe/Paris")
    await _seed_telegram_update(sessionmaker, 12)
    settings = Settings()
    provider = FakeLLMProvider()
    fake_session = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake_session)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, settings, provider))

    update = Update.model_validate(_command_update(12, "/out"), context={"bot": bot})
    await dp.feed_update(bot, update)

    assert provider.calls == 0
    assert fake_session.sent[0].text == turn.PAUSE_REPLY_TEXT

    await bot.session.close()


# --- 1f: /search (plan milestone 1f) ---


async def test_search_with_no_query_sends_canned_reply_and_makes_no_provider_call(sessionmaker):
    await _seed_user_state(sessionmaker, persona_active=True, intensity=3, timezone="Europe/Paris")
    await _seed_telegram_update(sessionmaker, 20)
    settings = Settings(LLM_WEB_SEARCH=True)
    provider = FakeLLMProvider()
    fake_session = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake_session)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, settings, provider))

    update = Update.model_validate(_search_update(20, "/search"), context={"bot": bot})
    await dp.feed_update(bot, update)

    assert provider.calls == 0
    assert fake_session.sent[0].text == turn.SEARCH_EMPTY_REPLY_TEXT

    async with sessionmaker() as session:
        result = await session.execute(select(SpendLedger))
        assert list(result.scalars().all()) == []

    await bot.session.close()


async def test_search_with_only_whitespace_query_sends_canned_reply(sessionmaker):
    await _seed_user_state(sessionmaker, persona_active=True, intensity=3, timezone="Europe/Paris")
    await _seed_telegram_update(sessionmaker, 21)
    settings = Settings(LLM_WEB_SEARCH=True)
    provider = FakeLLMProvider()
    fake_session = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake_session)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, settings, provider))

    update = Update.model_validate(_search_update(21, "/search    "), context={"bot": bot})
    await dp.feed_update(bot, update)

    assert provider.calls == 0
    assert fake_session.sent[0].text == turn.SEARCH_EMPTY_REPLY_TEXT

    await bot.session.close()


async def test_search_declines_when_web_search_disabled(sessionmaker):
    await _seed_user_state(sessionmaker, persona_active=True, intensity=3, timezone="Europe/Paris")
    await _seed_telegram_update(sessionmaker, 22)
    settings = Settings(LLM_WEB_SEARCH=False)
    provider = FakeLLMProvider()
    fake_session = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake_session)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, settings, provider))

    update = Update.model_validate(
        _search_update(22, "/search какая сегодня погода"), context={"bot": bot}
    )
    await dp.feed_update(bot, update)

    assert provider.calls == 0
    assert fake_session.sent[0].text == turn.SEARCH_DISABLED_REPLY_TEXT

    await bot.session.close()


async def test_search_calls_provider_with_web_search_true_and_the_query_as_user_text(sessionmaker):
    await _seed_user_state(sessionmaker, persona_active=True, intensity=3, timezone="Europe/Paris")
    await _seed_telegram_update(sessionmaker, 23)
    settings = Settings(LLM_WEB_SEARCH=True)
    provider = FakeLLMProvider(text="Вот что нашлось.")
    fake_session = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake_session)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, settings, provider))

    update = Update.model_validate(
        _search_update(23, "/search какая сегодня погода"), context={"bot": bot}
    )
    await dp.feed_update(bot, update)

    assert provider.calls == 1
    assert provider.received_web_search == [True]
    contents = [m.content for m in provider.received_messages[0]]
    assert "какая сегодня погода" in contents
    assert fake_session.sent[0].text == "Вот что нашлось."

    await bot.session.close()


async def test_search_while_persona_inactive_uses_neutral_prompt_and_ooc(sessionmaker):
    await _seed_user_state(sessionmaker, persona_active=False, intensity=3, timezone="Europe/Paris")
    await _seed_telegram_update(sessionmaker, 24)
    settings = Settings(LLM_WEB_SEARCH=True)
    provider = FakeLLMProvider(text="Нейтральный ответ с поиском.")
    fake_session = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake_session)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, settings, provider))

    update = Update.model_validate(
        _search_update(24, "/search какая сегодня погода"), context={"bot": bot}
    )
    await dp.feed_update(bot, update)

    assert provider.calls == 1
    assert provider.received_web_search == [True]
    messages = provider.received_messages[0]
    assert messages[0].role == "system"
    assert messages[0].content == prompt.NEUTRAL_SYSTEM_PROMPT
    assert fake_session.sent[0].text == "Нейтральный ответ с поиском."

    async with sessionmaker() as session:
        result = await session.execute(
            select(Message).where(Message.update_id == 24, Message.role == "user")
        )
        user_row = result.scalar_one()
    assert user_row.ooc is True

    async with sessionmaker() as session:
        result = await session.execute(select(Message).where(Message.reply_to_update == 24))
        assistant_row = result.scalar_one()
    assert assistant_row.ooc is True

    await bot.session.close()


async def test_search_registered_before_text_handler(sessionmaker):
    """aiogram is first-match-wins: /search must be reached as a
    command, not fall through to handle_text."""
    await _seed_user_state(sessionmaker, persona_active=True, intensity=3, timezone="Europe/Paris")
    await _seed_telegram_update(sessionmaker, 25)
    settings = Settings(LLM_WEB_SEARCH=True)
    provider = FakeLLMProvider(text="Ответ.")
    fake_session = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake_session)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, settings, provider))

    update = Update.model_validate(_search_update(25, "/search тест"), context={"bot": bot})
    await dp.feed_update(bot, update)

    assert provider.received_web_search == [True]

    await bot.session.close()


async def test_an_ordinary_text_message_reaches_the_provider_with_search_off(sessionmaker):
    """The invariant behind the opt-in design: search costs ~$0.007 a
    turn and sends the message text to a third party, so it must happen
    only when /search asked for it.

    This asserts the ORDINARY path end to end -- a plain text message
    through the real dispatcher -- rather than the provider layer in
    isolation. Without it, flipping turn.run()'s `web_search` default to
    True passes the entire suite while every message silently starts
    searching.
    """
    await _seed_user_state(sessionmaker, persona_active=True, intensity=3, timezone="Europe/Paris")
    await _seed_telegram_update(sessionmaker, 40)
    provider = FakeLLMProvider()
    fake_session = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake_session)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, Settings(), provider))

    update = Update.model_validate(
        {
            "update_id": 40,
            "message": {
                "message_id": 40,
                "date": 1700000000,
                "chat": {"id": TEST_CHAT_ID, "type": "private"},
                "from": {"id": TEST_CHAT_ID, "is_bot": False, "first_name": "T"},
                "text": "просто обычное сообщение",
            },
        },
        context={"bot": bot},
    )
    await dp.feed_update(bot, update)

    assert provider.calls == 1
    assert provider.received_web_search == [False]

    await bot.session.close()

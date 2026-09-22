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

from app.config import Settings
from app.core import turn
from app.core.state import get_state
from app.db.models import SpendLedger, TelegramUpdate, UserState
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
    settings = Settings(XAI_MODEL="grok-4.7", DAILY_USD_CAP=1.00)

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
    assert "grok-4.7" in text
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

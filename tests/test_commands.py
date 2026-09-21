"""/start and /state command tests (plan section 12 / 16).

Reuses the FakeSession capture pattern from tests/test_worker.py: a
fake aiogram BaseSession captures outgoing SendMessage calls instead of
hitting the network.
"""

from __future__ import annotations

import datetime
import decimal
from typing import AsyncGenerator
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import Message, Update

from app.config import Settings
from app.db.models import SpendLedger, UserState
from app.tg.router import START_TEXT, build_router

TEST_CHAT_ID = 555


class FakeSession(BaseSession):
    """Captures outgoing methods instead of making real HTTP requests."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[SendMessage] = []
        self._next_message_id = 1

    async def close(self) -> None:
        pass

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):
        if isinstance(method, SendMessage):
            self.sent.append(method)
            message_id = self._next_message_id
            self._next_message_id += 1
            return Message.model_validate(
                {
                    "message_id": message_id,
                    "date": 0,
                    "chat": {"id": method.chat_id, "type": "private"},
                    "text": method.text,
                },
                context={"bot": bot},
            )
        raise NotImplementedError(f"FakeSession cannot handle {method!r}")

    async def stream_content(
        self,
        url: str,
        headers: dict | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        raise NotImplementedError
        yield b""  # pragma: no cover


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
    dp.include_router(build_router(sessionmaker, settings))
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

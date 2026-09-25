"""The Claude connector wired the way app/main.py wires it.

The other Claude tests hand one PendingStore straight to their app and
router. Production goes through `main.build_webhook_app` and
`main.build_dispatcher`, and that path once gave /oauth/authorize a
second, private store (an empty store was falsy, and `or` replaced it),
so `/claude connect` never matched a code. This file drives the real
builders with one fresh, empty store, as `main()` does.
"""

from __future__ import annotations

import re

import pytest
from aiogram import Bot
from aiogram.types import Update
from aiohttp.test_utils import TestClient, TestServer

from app.config import Settings
from app.core.clock import SystemClock
from app.db.models import TelegramUpdate, UserState
from app.main import build_dispatcher, build_webhook_app
from app.web import oauth_store
from claude_helpers import CHAT_ID, PUBLIC_URL, World
from conftest import FakeLLMProvider, FakeSession

pytestmark = pytest.mark.asyncio


def _settings() -> Settings:
    return Settings(
        MODE="webhook",
        TELEGRAM_BOT_TOKEN="123456:TEST",
        PUBLIC_URL=PUBLIC_URL,
        ALLOWED_CHAT_ID=CHAT_ID,
        CLAUDE_ACCESS_ENABLED=True,
    )


def test_an_empty_store_is_still_a_store():
    store = oauth_store.PendingStore(SystemClock())
    assert len(store) == 0
    assert bool(store) is True


async def test_authorize_and_connect_share_main_s_one_store(sessionmaker):
    settings = _settings()
    clock = SystemClock()
    store = oauth_store.PendingStore(clock)
    dp = build_dispatcher(
        sessionmaker, settings, FakeLLMProvider(), FakeLLMProvider(), clock, None, None,
        claude_pending=store,
    )
    web_bot = Bot(token=settings.TELEGRAM_BOT_TOKEN, session=FakeSession())
    app = build_webhook_app(
        settings, web_bot, dp, sessionmaker,
        engine=None, provider=None, cheap_provider=None, safety_provider=None,
        llm_client=None, clock=clock, hub=None, code_store=None, claude_pending=store,
    )
    app.on_startup.clear()
    app.on_cleanup.clear()
    assert app["claude_pending"] is store

    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone="Europe/Paris"))
        session.add(TelegramUpdate(update_id=7, payload={}))
        await session.commit()

    async with TestClient(TestServer(app)) as client:
        page = await (
            await client.get("/oauth/authorize", params=World.authorize_params())
        ).text()
    code = re.search(r"/claude connect ([A-Z0-9]{6})", page).group(1)
    assert len(store) == 1

    fake = FakeSession()
    tg_bot = Bot(token="123456:TESTTOKEN", session=fake)
    text = f"/claude connect {code}"
    update = {
        "update_id": 7,
        "message": {
            "message_id": 7,
            "date": 0,
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Test"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": 7}],
        },
    }
    await dp.feed_update(tg_bot, Update.model_validate(update, context={"bot": tg_bot}))
    assert fake.sent[-1].text.startswith("Подтверждено")
    await web_bot.session.close()

"""/weblogout (web-chat plan track 2, design section 4): the Telegram-side
kill switch for the web chat.

- revokes every web_session row
- closes every live WebHub subscription and clears the callback allowlist
- invalidates every pending Telegram login code (the CodeStore threaded
  in beside the hub)
- replies with the canned Russian confirmation
- is not even registered as a command (falls through to an ordinary
  persona turn, like any other unrecognized command text) when no `hub`
  was threaded through build_router -- the web UI disabled case (a
  low-severity finding: it used to always register and always reply
  "closed" regardless)
- is itself idempotent under a replay, like every other command gated
  on _once/mark_update_handled in this router
"""

from __future__ import annotations

import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import func, select

from app.config import Settings
from app.core.clock import FrozenClock
from app.db.models import TelegramUpdate, UserState, WebSession
from app.tg.router import WEBLOGOUT_REPLY, build_router
from app.web import auth
from app.web.hub import WebHub
from conftest import FakeLLMProvider, FakeSession

pytestmark = pytest.mark.asyncio

CHAT_ID = 555


async def _seed_telegram_update(sessionmaker, update_id: int) -> None:
    """The inbound row send_command_reply's message insert FKs against
    (app/db/models.py's Message.update_id) -- every command test in this
    style seeds one first, matching tests/test_commands.py's helper.
    """
    async with sessionmaker() as session:
        session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()


def _command_update(update_id: int, text: str = "/weblogout") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Test"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(text)}],
        },
    }


def _build_dp(sessionmaker, hub: WebHub | None, code_store: auth.CodeStore | None = None):
    fake = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)
    dp = Dispatcher()
    dp.include_router(
        build_router(
            sessionmaker,
            Settings(),
            FakeLLMProvider(),
            FakeLLMProvider(),
            hub=hub,
            code_store=code_store,
        )
    )
    return dp, bot, fake


async def test_weblogout_revokes_sessions_closes_the_hub_and_clears_pending_codes(sessionmaker):
    clock = FrozenClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    async with sessionmaker() as session:
        token, _ = await auth.create_session(session, clock, Settings())

    hub = WebHub()
    sub = hub.subscribe()
    code_store = auth.CodeStore()
    code_store.issue("some-pre-token", clock, ttl_s=300)

    await _seed_telegram_update(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, hub, code_store)
    update = Update.model_validate(_command_update(1), context={"bot": bot})
    await dp.feed_update(bot, update)

    async with sessionmaker() as session:
        result = await session.execute(select(func.count()).select_from(WebSession))
        assert result.scalar_one() == 0
        assert await auth.validate_session(session, clock, Settings(), token) is False

    events = [record async for record in sub.events()]
    assert events == []  # the poison pill closed the stream

    # The pending code from *before* /weblogout no longer verifies --
    # this is the "Если это не ты — /weblogout" promise the login-code
    # message itself makes (a low-severity finding: it used to be false).
    assert code_store.pending("some-pre-token", clock) is False

    assert len(fake.sent) == 1
    assert fake.sent[0].text == WEBLOGOUT_REPLY


async def test_weblogout_falls_through_to_an_ordinary_turn_when_hub_is_none(sessionmaker):
    """The web UI disabled case: build_router()'s default `hub=None`
    means /weblogout is never registered as a command at all, so its
    text is handled exactly like any other unrecognized command --
    an ordinary persona turn, not a canned "closed" reply."""
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone="Europe/Paris"))
        await session.commit()
    await _seed_telegram_update(sessionmaker, 2)
    dp, bot, fake = _build_dp(sessionmaker, hub=None)
    update = Update.model_validate(_command_update(2), context={"bot": bot})
    await dp.feed_update(bot, update)

    assert len(fake.sent) == 1
    assert fake.sent[0].text != WEBLOGOUT_REPLY


async def test_weblogout_replay_is_idempotent(sessionmaker):
    clock = FrozenClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    async with sessionmaker() as session:
        await auth.create_session(session, clock, Settings())

    hub = WebHub()
    await _seed_telegram_update(sessionmaker, 3)
    dp, bot, fake = _build_dp(sessionmaker, hub)
    payload = _command_update(3)

    for _ in range(2):
        update = Update.model_validate(payload, context={"bot": bot})
        await dp.feed_update(bot, update)

    # _once() gates the handler on update_id, so a replay of the same
    # update sends nothing a second time.
    assert len(fake.sent) == 1

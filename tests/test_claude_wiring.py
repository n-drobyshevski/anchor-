"""The Claude connector wired the way app/main.py wires it.

The other Claude tests hand one PendingStore straight to their app and
router. Production goes through `main.build_webhook_app` and
`main.build_dispatcher`, and that path once gave /oauth/authorize a
second, private store (an empty store was falsy, and `or` replaced it),
so `/claude connect` never matched a code. This file drives the real
builders with one fresh, empty store, as `main()` does.
"""

from __future__ import annotations

import base64
import hashlib
import re
import urllib.parse

import pytest
from aiogram import Bot
from aiogram.types import Update
from aiohttp.test_utils import TestClient, TestServer

from app.config import Settings
from app.core.clock import SystemClock
from app.db.models import TelegramUpdate, UserState, VaultFile
from app.main import build_dispatcher, build_webhook_app
from app.vault import notes_knowledge
from app.vault._chunks import Chunk
from app.web import oauth, oauth_store
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


def _command_update(update_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Test"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}],
        },
    }


async def test_search_library_is_reachable_through_the_real_app(sessionmaker):
    """C3, on top of the same wiring this file already guards: the
    connector plan's `search_library` must work when the app is the one
    app/main.py's own builders assemble, not only the ad hoc app
    claude_helpers.World builds for the rest of the Claude test suite."""
    settings = Settings(
        MODE="webhook",
        TELEGRAM_BOT_TOKEN="123456:TEST",
        PUBLIC_URL=PUBLIC_URL,
        ALLOWED_CHAT_ID=CHAT_ID,
        CLAUDE_ACCESS_ENABLED=True,
        VAULT_KNOWLEDGE_ENABLED=True,
    )
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

    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone="Europe/Paris", notes_consent=True))
        session.add_all([TelegramUpdate(update_id=8, payload={}), TelegramUpdate(update_id=9, payload={})])
        await session.commit()
        vault_file = VaultFile(path="Library/CCRU.md", role="note", note_class="knowledge")
        session.add(vault_file)
        await session.commit()
        await notes_knowledge.replace_chunks(
            session, vault_file.id, [Chunk("CCRU", "Гиперстишн и ускорение.")]
        )
        await session.commit()

    tg_bot = Bot(token="123456:TESTTOKEN", session=FakeSession())
    verifier = "verifier-" + "x" * 50
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    async with TestClient(TestServer(app)) as client:
        authorize_resp = await client.get(
            "/oauth/authorize", params=World.authorize_params(code_challenge=challenge)
        )
        page = await authorize_resp.text()
        code = re.search(r"/claude connect ([A-Z0-9]{6})", page).group(1)
        handle = re.search(r"h=([A-Za-z0-9_-]{22})", page).group(1)
        set_cookie = authorize_resp.cookies.get(oauth.COOKIE)
        cookie_value = set_cookie.value if set_cookie is not None else None

        await dp.feed_update(
            tg_bot,
            Update.model_validate(
                _command_update(8, f"/claude connect {code}"), context={"bot": tg_bot}
            ),
        )
        headers = {"Cookie": f"{oauth.COOKIE}={cookie_value}"} if cookie_value else {}
        status_resp = await client.get(
            "/oauth/authorize/status", params={"h": handle}, headers=headers, allow_redirects=False
        )
        auth_code = urllib.parse.parse_qs(
            urllib.parse.urlsplit(status_resp.headers["Location"]).query
        )["code"][0]

        token_resp = await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "code_verifier": verifier,
                "client_id": oauth_store.CLIENT_ID,
                "redirect_uri": oauth_store.REDIRECT_URI,
                "resource": f"{PUBLIC_URL}/mcp/claude",
            },
        )
        tokens = await token_resp.json()

        await dp.feed_update(
            tg_bot,
            Update.model_validate(_command_update(9, "/claude library on"), context={"bot": tg_bot}),
        )

        call = await client.post(
            "/mcp/claude",
            json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "search_library", "arguments": {"query": "гиперстишн ускорение"}},
            },
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        body = await call.json()

    assert body["result"]["isError"] is False
    assert body["result"]["content"][0]["text"] == "«CCRU»: Гиперстишн и ускорение."
    await web_bot.session.close()
    await tg_bot.session.close()

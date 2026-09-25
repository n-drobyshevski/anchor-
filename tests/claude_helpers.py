"""Shared scaffolding for the Claude connector tests (C2).

One aiohttp app with the authorization server, `/mcp/claude` and (for
cross-client tests) Grok's route; one aiogram dispatcher for the
Telegram side; one FrozenClock and one PendingStore shared by both, as
app/main.py shares them in production. Everything synthetic, 127.0.0.1.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import re
import time
import urllib.parse

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from aiohttp import web
from sqlalchemy import func, select

from app.config import Settings
from app.core.clock import FrozenClock
from app.db.models import OauthRequest, TelegramUpdate, UserState
from app.tg.router import build_router
from app.web import mcp, mcp_claude, oauth, oauth_store
from conftest import FakeLLMProvider, FakeSession

CHAT_ID = 555
PUBLIC_URL = "https://anchor.example"
R = PUBLIC_URL + "/mcp/claude"
START = datetime.datetime(2026, 9, 25, 12, tzinfo=datetime.timezone.utc)
VERIFIER = "verifier-" + "x" * 50
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
)
STATE = "state-sentinel-5f2c"

_CODE_RE = re.compile(r"/claude connect ([A-Z0-9]{6})")
_HANDLE_RE = re.compile(r"h=([A-Za-z0-9_-]{22})")


def settings(**overrides) -> Settings:
    values = dict(
        CLAUDE_ACCESS_ENABLED=True,
        GROK_ACCESS_ENABLED=True,
        MODE="webhook",
        PUBLIC_URL=PUBLIC_URL,
        ALLOWED_CHAT_ID=CHAT_ID,
        CLAUDE_MAX_CALLS_PER_MINUTE=30,
        GROK_MAX_CALLS_PER_MINUTE=30,
    )
    values.update(overrides)
    return Settings(**values)


class World:
    """The web app, the Telegram dispatcher, the clock and the store."""

    def __init__(self, sessionmaker, cfg: Settings | None = None) -> None:
        self.sessionmaker = sessionmaker
        self.settings = cfg or settings()
        self.clock = FrozenClock(START)
        self.pending = oauth_store.PendingStore(self.clock)
        self.web_fake = FakeSession()
        app = web.Application()
        app["settings"] = self.settings
        app["sessionmaker"] = sessionmaker
        app["clock"] = self.clock
        app["bot"] = Bot(token="123456:TESTTOKEN", session=self.web_fake)
        if self.settings.CLAUDE_ACCESS_ENABLED:
            oauth.register(app, self.settings, self.pending)
            mcp_claude.register(app, self.settings)
        if self.settings.GROK_ACCESS_ENABLED:
            mcp.register(app, self.settings)
        self.app = app
        self.tg_fake = FakeSession()
        self.tg_bot = Bot(token="123456:TESTTOKEN", session=self.tg_fake)
        self.dp = Dispatcher()
        self.dp.include_router(
            build_router(
                sessionmaker, self.settings, FakeLLMProvider(), FakeLLMProvider(),
                self.clock, None, None, self.pending,
            )
        )
        self._update_id = 100

    async def seed(self) -> None:
        async with self.sessionmaker() as session:
            session.add(UserState(id=1, chat_id=CHAT_ID, timezone="Europe/Paris"))
            session.add_all([TelegramUpdate(update_id=i, payload={}) for i in range(100, 400)])
            await session.commit()

    # --- Telegram ---

    async def command(self, text: str) -> str:
        self._update_id += 1
        update = {
            "update_id": self._update_id,
            "message": {
                "message_id": self._update_id,
                "date": 0,
                "chat": {"id": CHAT_ID, "type": "private"},
                "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Test"},
                "text": text,
                "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}],
            },
        }
        before = len(self.tg_fake.sent)
        await self.dp.feed_update(self.tg_bot, Update.model_validate(update, context={"bot": self.tg_bot}))
        assert len(self.tg_fake.sent) > before, text
        return self.tg_fake.sent[-1].text

    async def press(self, data: str) -> None:
        self._update_id += 1
        update = {
            "update_id": self._update_id,
            "callback_query": {
                "id": f"cb{self._update_id}",
                "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Test"},
                "chat_instance": "ci",
                "data": data,
                "message": {
                    "message_id": 1,
                    "date": 0,
                    "chat": {"id": CHAT_ID, "type": "private"},
                    "text": "x",
                },
            },
        }
        await self.dp.feed_update(self.tg_bot, Update.model_validate(update, context={"bot": self.tg_bot}))

    def epoch(self) -> int:
        return int(self.clock.now_utc().timestamp())

    # --- the browser and claude.ai ---

    @staticmethod
    def authorize_params(**overrides) -> dict:
        params = dict(
            response_type="code",
            client_id=oauth_store.CLIENT_ID,
            redirect_uri=oauth_store.REDIRECT_URI,
            code_challenge=CHALLENGE,
            code_challenge_method="S256",
            resource=R,
            scope="anchor.read",
            state=STATE,
        )
        params.update(overrides)
        return {k: v for k, v in params.items() if v is not None}

    async def start(self, client, cookie: str | None = None, address: str = "203.0.113.7", **overrides):
        """Open /oauth/authorize. Returns (handle, confirmation code, cookie)."""
        headers = {"X-Forwarded-For": f"198.51.100.1, {address}"}
        if cookie:
            headers["Cookie"] = f"{oauth.COOKIE}={cookie}"
        resp = await client.get(
            "/oauth/authorize", params=self.authorize_params(**overrides), headers=headers,
            allow_redirects=False,
        )
        assert resp.status == 200, await resp.text()
        page = await resp.text()
        new_cookie = resp.cookies.get(oauth.COOKIE)
        return (
            _HANDLE_RE.search(page).group(1),
            _CODE_RE.search(page).group(1),
            new_cookie.value if new_cookie is not None else cookie,
        )

    @staticmethod
    async def poll(client, handle: str, cookie: str | None):
        headers = {"Cookie": f"{oauth.COOKIE}={cookie}"} if cookie else {}
        return await client.get(
            "/oauth/authorize/status", params={"h": handle}, headers=headers, allow_redirects=False
        )

    async def auth_code(self, client) -> str:
        """The whole browser dance up to claude.ai's callback: the code."""
        handle, code, cookie = await self.start(client)
        assert (await self.command(f"/claude connect {code}")).startswith("Подтверждено")
        resp = await self.poll(client, handle, cookie)
        assert resp.status == 302
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(resp.headers["Location"]).query)
        return query["code"][0]

    @staticmethod
    async def exchange(client, code: str, **overrides):
        form = dict(
            grant_type="authorization_code",
            code=code,
            code_verifier=VERIFIER,
            client_id=oauth_store.CLIENT_ID,
            redirect_uri=oauth_store.REDIRECT_URI,
            resource=R,
        )
        form.update(overrides)
        return await client.post("/oauth/token", data={k: v for k, v in form.items() if v is not None})

    async def connect(self, client) -> dict:
        """A full connection: returns the token response body."""
        resp = await self.exchange(client, await self.auth_code(client))
        assert resp.status == 200, await resp.text()
        return await resp.json()

    @staticmethod
    async def refresh(client, refresh_token: str, **overrides):
        form = dict(
            grant_type="refresh_token",
            refresh_token=refresh_token,
            client_id=oauth_store.CLIENT_ID,
        )
        form.update(overrides)
        return await client.post("/oauth/token", data={k: v for k, v in form.items() if v is not None})

    @staticmethod
    async def mcp(client, access_token: str | None, method: str = "tools/list", params=None):
        body = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            body["params"] = params
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
        return await client.post("/mcp/claude", json=body, headers=headers)

    async def open_window(self, scopes=("journal",), ttl_index: int = 0) -> None:
        mask = sum(1 << ["memory", "journal", "dialogs", "state"].index(s) for s in scopes)
        await self.press(f"cl:ok:{mask}:0:{ttl_index}:{self.epoch()}")

    async def request_rows(self) -> int:
        async with self.sessionmaker() as session:
            return (await session.execute(select(func.count()).select_from(OauthRequest))).scalar_one()


def now_epoch() -> int:
    return int(time.time())

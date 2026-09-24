"""Grok access: grants, the MCP endpoint, and the /grok and /revoke commands.

The promises pinned here (docs/grok-access.md):
- nothing is readable without a grant the user pressed [Разрешить] for;
- a grant reads only its scopes, only its look-back, only until it
  expires or is revoked;
- the token is never stored and never logged;
- every read is reported to the user in Telegram.
"""

from __future__ import annotations

import datetime
import json
import logging
import time

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from app.config import Settings
from app.core import grants
from app.core.clock import FrozenClock, SystemClock
from app.db.models import AccessGrant, Journal, Memory, Message, TelegramUpdate, UserState
from app.tg import grok as grok_ui
from app.tg.router import build_router
from app.web import mcp
from conftest import FakeLLMProvider, FakeSession

pytestmark = pytest.mark.asyncio

CHAT_ID = 555
PUBLIC_URL = "https://anchor.example"


def _settings(**overrides) -> Settings:
    values = dict(
        GROK_ACCESS_ENABLED=True,
        MODE="webhook",
        PUBLIC_URL=PUBLIC_URL,
        ALLOWED_CHAT_ID=CHAT_ID,
        GROK_MAX_CALLS_PER_MINUTE=30,
    )
    values.update(overrides)
    return Settings(**values)


async def _seed(sessionmaker) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone="Europe/Paris", due_action="бег"))
        # Commands store their reply against the update they answer.
        session.add_all([TelegramUpdate(update_id=i, payload={}) for i in range(2, 6)])
        session.add_all(
            [
                Memory(kind="identity", text="любит горы", source="user"),
                Journal(local_date=datetime.date.today(), text="хороший день"),
                Message(role="user", content="свежее", created_at=now),
                Message(role="user", content="старое", created_at=now - datetime.timedelta(days=20)),
                Message(role="user", content="вне роли", ooc=True, created_at=now),
                Message(role="assistant", content="забота", kind="welfare", created_at=now),
            ]
        )
        await session.commit()


async def _grant(sessionmaker, clock=None, **kwargs) -> tuple[str, AccessGrant]:
    kwargs.setdefault("scopes", ["memory"])
    kwargs.setdefault("ttl_hours", 1)
    async with sessionmaker() as session:
        return await grants.create_grant(session, clock or SystemClock(), **kwargs)


def _app(sessionmaker, settings: Settings, clock=None) -> tuple[web.Application, FakeSession]:
    fake = FakeSession()
    app = web.Application()
    app["settings"] = settings
    app["sessionmaker"] = sessionmaker
    app["clock"] = clock or SystemClock()
    app["bot"] = Bot(token="123456:TESTTOKEN", session=fake)
    if settings.GROK_ACCESS_ENABLED:
        mcp.register(app, settings)
    return app, fake


def _rpc(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


async def _call(client, token: str, tool: str, arguments: dict | None = None) -> dict:
    resp = await client.post(
        f"/mcp/{token}", json=_rpc("tools/call", {"name": tool, "arguments": arguments or {}})
    )
    assert resp.status == 200
    return await resp.json()


# --- grants ---


async def test_token_is_not_stored_only_its_hash(sessionmaker):
    token, grant = await _grant(sessionmaker)
    async with sessionmaker() as session:
        row = await session.get(AccessGrant, grant.id)
    assert row.token_sha256 == grants.hash_token(token)
    assert token not in row.token_sha256


async def test_grant_expires_and_revokes(sessionmaker):
    start = datetime.datetime(2026, 9, 1, 12, tzinfo=datetime.timezone.utc)
    clock = FrozenClock(start)
    token, _ = await _grant(sessionmaker, clock, ttl_hours=1)
    async with sessionmaker() as session:
        assert await grants.find_active_grant(session, clock, token) is not None
        clock.advance(datetime.timedelta(hours=2))
        assert await grants.find_active_grant(session, clock, token) is None

    clock.set(start)
    token2, _ = await _grant(sessionmaker, clock)
    async with sessionmaker() as session:
        assert await grants.revoke_all(session, clock) == 2
        assert await grants.find_active_grant(session, clock, token2) is None


async def test_ttl_is_capped(sessionmaker):
    _, grant = await _grant(sessionmaker, ttl_hours=10_000, max_hours=24)
    assert grant.expires_at - grant.created_at == datetime.timedelta(hours=24)


async def test_a_grant_needs_a_scope(sessionmaker):
    with pytest.raises(ValueError):
        await _grant(sessionmaker, scopes=[])


# --- the MCP endpoint ---


async def test_initialize_and_tools_follow_scopes(sessionmaker):
    await _seed(sessionmaker)
    token, _ = await _grant(sessionmaker, scopes=["memory", "state"])
    app, _ = _app(sessionmaker, _settings())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            f"/mcp/{token}", json=_rpc("initialize", {"protocolVersion": "2025-06-18"})
        )
        body = await resp.json()
        assert body["result"]["protocolVersion"] == "2025-06-18"
        assert "tools" in body["result"]["capabilities"]

        resp = await client.post(
            f"/mcp/{token}", json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        assert resp.status == 202

        body = await (await client.post(f"/mcp/{token}", json=_rpc("tools/list"))).json()
        names = {t["name"] for t in body["result"]["tools"]}
        assert names == {"get_memory", "get_state"}
        assert all(t["annotations"]["readOnlyHint"] for t in body["result"]["tools"])


async def test_memory_read_and_notification(sessionmaker):
    await _seed(sessionmaker)
    token, grant = await _grant(sessionmaker, scopes=["memory"])
    app, fake = _app(sessionmaker, _settings())
    async with TestClient(TestServer(app)) as client:
        body = await _call(client, token, "get_memory")
        payload = json.loads(body["result"]["content"][0]["text"])
        assert [m["text"] for m in payload["memories"]] == ["любит горы"]
        await _call(client, token, "get_memory")

    # One notice for two reads: the second falls inside NOTIFY_EVERY.
    assert len(fake.sent) == 1
    assert fake.sent[0].chat_id == CHAT_ID
    assert "память" in fake.sent[0].text and "/revoke" in fake.sent[0].text
    async with sessionmaker() as session:
        assert (await session.get(AccessGrant, grant.id)).use_count == 2


async def test_a_tool_outside_the_grant_is_refused(sessionmaker):
    await _seed(sessionmaker)
    token, _ = await _grant(sessionmaker, scopes=["memory"])
    app, fake = _app(sessionmaker, _settings())
    async with TestClient(TestServer(app)) as client:
        body = await _call(client, token, "get_dialogs")
    assert body["error"]["code"] == -32602
    assert fake.sent == []


async def test_dialogs_respect_lookback_and_exclusions(sessionmaker):
    await _seed(sessionmaker)
    token, _ = await _grant(sessionmaker, scopes=["dialogs"], dialog_days=7)
    app, _ = _app(sessionmaker, _settings())
    async with TestClient(TestServer(app)) as client:
        # Asks for 90 days; the grant allows 7.
        body = await _call(client, token, "get_dialogs", {"days": 90})
    texts = [m["text"] for m in json.loads(body["result"]["content"][0]["text"])["messages"]]
    assert texts == ["свежее"]


@pytest.mark.parametrize("case", ["unknown", "expired", "revoked", "malformed", "disabled"])
async def test_refusals_are_all_the_same_404(sessionmaker, case):
    token, grant = await _grant(sessionmaker)
    settings = _settings()
    if case == "unknown":
        token = "A" * 43
    elif case == "malformed":
        token = "short"
    elif case in ("expired", "revoked"):
        async with sessionmaker() as session:
            row = await session.get(AccessGrant, grant.id)
            if case == "revoked":
                row.revoked_at = datetime.datetime.now(datetime.timezone.utc)
            else:
                row.created_at = row.created_at - datetime.timedelta(hours=3)
                row.expires_at = row.created_at + datetime.timedelta(hours=1)
            await session.commit()
    elif case == "disabled":
        settings = _settings(GROK_ACCESS_ENABLED=False)
    app, _ = _app(sessionmaker, settings)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(f"/mcp/{token}", json=_rpc("tools/list"))
        assert resp.status == 404
        assert await resp.text() == "404: Not Found"


async def test_rate_limit(sessionmaker):
    token, _ = await _grant(sessionmaker)
    app, _ = _app(sessionmaker, _settings(GROK_MAX_CALLS_PER_MINUTE=2))
    async with TestClient(TestServer(app)) as client:
        statuses = [
            (await client.post(f"/mcp/{token}", json=_rpc("ping"))).status for _ in range(3)
        ]
    assert statuses == [200, 200, 429]


async def test_get_is_405_and_batches_are_refused(sessionmaker):
    token, _ = await _grant(sessionmaker)
    app, _ = _app(sessionmaker, _settings())
    async with TestClient(TestServer(app)) as client:
        assert (await client.get(f"/mcp/{token}")).status == 405
        resp = await client.post(f"/mcp/{token}", json=[_rpc("ping")])
        assert resp.status == 400


async def test_token_and_content_never_reach_the_logs(sessionmaker, caplog):
    await _seed(sessionmaker)
    token, _ = await _grant(sessionmaker, scopes=["memory", "dialogs"], dialog_days=7)
    app, _ = _app(sessionmaker, _settings())
    caplog.set_level(logging.DEBUG)
    async with TestClient(TestServer(app)) as client:
        await _call(client, token, "get_memory")
        await _call(client, token, "get_dialogs")
    for record in caplog.records:
        rendered = record.getMessage() + json.dumps(record.__dict__, default=str)
        assert token not in rendered
        assert "любит горы" not in rendered and "свежее" not in rendered


# --- /grok and /revoke ---


def _command_update(update_id: int, text: str) -> dict:
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


def _callback_update(update_id: int, data: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
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


def _dp(sessionmaker, settings: Settings):
    fake = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, settings, FakeLLMProvider(), FakeLLMProvider()))
    return dp, bot, fake


async def _feed(dp, bot, update: dict) -> None:
    await dp.feed_update(bot, Update.model_validate(update, context={"bot": bot}))


async def _grant_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        return len((await session.execute(select(AccessGrant))).scalars().all())


async def test_grok_opens_a_picker_with_everything_off(sessionmaker):
    await _seed(sessionmaker)
    dp, bot, fake = _dp(sessionmaker, _settings())
    await _feed(dp, bot, _command_update(2, "/grok"))

    labels = [b.text for row in fake.sent[-1].reply_markup.inline_keyboard for b in row]
    assert all(label.startswith("▫️") for label in labels[:4])
    assert grok_ui.ALLOW in labels
    assert "xAI" in fake.sent[-1].text
    assert await _grant_count(sessionmaker) == 0


async def test_toggle_then_allow_creates_one_grant_and_shows_the_url(sessionmaker):
    await _seed(sessionmaker)
    dp, bot, fake = _dp(sessionmaker, _settings())
    issued = int(time.time())

    await _feed(dp, bot, _callback_update(3, f"g:t0:0:0:0:{issued}"))
    toggled = [b.text for row in fake.edits[-1].reply_markup.inline_keyboard for b in row]
    assert toggled[0].startswith("✅")
    assert await _grant_count(sessionmaker) == 0

    await _feed(dp, bot, _callback_update(4, f"g:ok:1:0:0:{issued}"))
    assert await _grant_count(sessionmaker) == 1
    text = fake.edits[-1].text
    assert f"{PUBLIC_URL}/mcp/" in text
    token = text.split(f"{PUBLIC_URL}/mcp/")[1].split()[0]
    async with sessionmaker() as session:
        grant = await grants.find_active_grant(session, SystemClock(), token)
    assert grant.scopes == ["memory"]


async def test_allow_with_nothing_selected_creates_nothing(sessionmaker):
    await _seed(sessionmaker)
    dp, bot, fake = _dp(sessionmaker, _settings())
    await _feed(dp, bot, _callback_update(3, f"g:ok:0:0:0:{int(time.time())}"))
    assert await _grant_count(sessionmaker) == 0
    assert fake.answered[-1].text == grok_ui.PICK_SOMETHING


async def test_a_stale_allow_creates_nothing(sessionmaker):
    await _seed(sessionmaker)
    dp, bot, fake = _dp(sessionmaker, _settings())
    await _feed(dp, bot, _callback_update(3, f"g:ok:1:0:0:{int(time.time()) - 3600}"))
    assert await _grant_count(sessionmaker) == 0


async def test_grok_refuses_when_disabled(sessionmaker):
    await _seed(sessionmaker)
    dp, bot, fake = _dp(sessionmaker, _settings(GROK_ACCESS_ENABLED=False))
    await _feed(dp, bot, _command_update(2, "/grok"))
    assert fake.sent[-1].text == grok_ui.DISABLED
    await _feed(dp, bot, _callback_update(3, f"g:ok:1:0:0:{int(time.time())}"))
    assert await _grant_count(sessionmaker) == 0


async def test_revoke_closes_everything(sessionmaker):
    await _seed(sessionmaker)
    token, _ = await _grant(sessionmaker)
    dp, bot, fake = _dp(sessionmaker, _settings(GROK_ACCESS_ENABLED=False))
    await _feed(dp, bot, _command_update(2, "/revoke"))
    assert fake.sent[-1].text == grok_ui.REVOKED_TEXT.format(count=1)
    async with sessionmaker() as session:
        assert await grants.find_active_grant(session, SystemClock(), token) is None

"""Claude's windows, /claude, /revoke, and the two clients side by side.

Connector plan sections 6 and 10. A connected Claude reads nothing
outside a window it was given in Telegram; what it reads inside one is
byte for byte what Grok would read; each read is announced; /revoke
closes both clients' doors; neither client's credential works on the
other's endpoint; the limiters are separate.
"""

from __future__ import annotations

import datetime
import json
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from app.core import grants
from app.db.models import AccessGrant, Journal, Memory, Message
from app.tg import claude as claude_ui
from app.tg import data as data_ui
from app.web import ingress, oauth_store
from claude_helpers import CHAT_ID, R, START, World, settings

pytestmark = pytest.mark.asyncio

CLOSED = "Доступ закрыт. Открой его в Telegram: /claude"


async def _world(sessionmaker, **overrides) -> World:
    world = World(sessionmaker, settings(**overrides) if overrides else None)
    await world.seed()
    async with sessionmaker() as session:
        session.add_all(
            [
                Memory(kind="identity", text="любит горы", source="user"),
                Journal(local_date=START.date(), text="хороший день"),
                Message(role="user", content="свежее", created_at=START),
                Message(role="user", content="вне роли", ooc=True, created_at=START),
                Message(role="assistant", content="забота", kind="welfare", created_at=START),
            ]
        )
        await session.commit()
    return world


async def _call(world, client, token, tool, arguments=None) -> dict:
    resp = await world.mcp(
        client, token, "tools/call", {"name": tool, "arguments": arguments or {}}
    )
    assert resp.status == 200
    return await resp.json()


def _payload(body: dict) -> dict:
    assert body["result"]["isError"] is False, body
    return json.loads(body["result"]["content"][0]["text"])


# --- windows ---


async def test_tools_are_listed_but_nothing_is_readable_without_a_window(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        listed = await (await world.mcp(client, tokens["access_token"])).json()
        names = {t["name"] for t in listed["result"]["tools"]}
        assert names == {"get_memory", "get_journal", "get_dialogs", "get_state"}
        body = await _call(world, client, tokens["access_token"], "get_journal")
    assert body["result"] == {"content": [{"type": "text", "text": CLOSED}], "isError": True}
    assert world.tg_fake.sent[-1].text.startswith("Подтверждено")  # no read notice


async def test_a_window_reads_its_scopes_and_is_announced(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        await world.open_window(["journal"])
        assert world.tg_fake.edits[-1].text.startswith("Окно открыто до")
        journal = _payload(await _call(world, client, tokens["access_token"], "get_journal"))
        assert [j["text"] for j in journal["journal"]] == ["хороший день"]
        outside = await _call(world, client, tokens["access_token"], "get_memory")
        assert outside["result"]["isError"] is True
        assert outside["result"]["content"][0]["text"] == CLOSED
    notices = [m.text for m in world.web_fake.sent]
    async with sessionmaker() as session:
        connection = await oauth_store.current_connection(session, world.clock)
    assert notices == [f"Claude (подключение #{connection.id}) прочитал: журнал (1). Закрыть: /revoke"]


async def test_an_expired_window_reads_nothing(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        await world.open_window(["journal"])
        world.clock.advance(datetime.timedelta(minutes=59))
        # Keep the access token fresh: the window, not the token, ends here.
        rotated = await (await world.refresh(client, tokens["refresh_token"])).json()
        world.clock.advance(datetime.timedelta(minutes=2))
        body = await _call(world, client, rotated["access_token"], "get_journal")
    assert body["result"]["isError"] is True


async def test_a_new_window_closes_the_old_one(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        await world.open_window(["journal"])
        await world.open_window(["memory"])
        assert (await _call(world, client, tokens["access_token"], "get_journal"))["result"]["isError"]
        assert not (await _call(world, client, tokens["access_token"], "get_memory"))["result"]["isError"]
    async with sessionmaker() as session:
        windows = (
            await session.execute(select(AccessGrant).where(AccessGrant.client == "claude"))
        ).scalars().all()
    assert len(windows) == 2
    assert sum(1 for w in windows if w.revoked_at is None) == 1


async def test_a_window_cannot_outlive_the_ceiling(sessionmaker):
    world = await _world(sessionmaker, CLAUDE_WINDOW_MAX_HOURS=1)
    async with TestClient(TestServer(world.app)) as client:
        await world.connect(client)
        # With a 1 h ceiling, only the 1 h choice exists; index 1 is stale.
        await world.open_window(["journal"], ttl_index=1)
        assert world.tg_fake.edits[-1].text == "Устарело."
        await world.open_window(["journal"], ttl_index=0)
    async with sessionmaker() as session:
        window = (
            await session.execute(select(AccessGrant).where(AccessGrant.client == "claude"))
        ).scalar_one()
    assert window.expires_at - window.created_at == datetime.timedelta(hours=1)


async def test_dialogs_through_claude_keep_grok_s_exclusions(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        await world.open_window(["dialogs"])
        dialogs = _payload(await _call(world, client, tokens["access_token"], "get_dialogs"))
    assert [m["text"] for m in dialogs["messages"]] == ["свежее"]


# --- the two clients ---


async def test_both_clients_read_identical_payloads(sessionmaker):
    world = await _world(sessionmaker)
    async with sessionmaker() as session:
        grok_token, _ = await grants.create_grant(
            session, world.clock, scopes=["memory", "journal", "dialogs", "state"], ttl_hours=1,
            dialog_days=7,
        )
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        await world.open_window(["memory", "journal", "dialogs", "state"])
        for tool in ("get_memory", "get_journal", "get_dialogs", "get_state"):
            call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool}}
            via_grok = await (await client.post(f"/mcp/{grok_token}", json=call)).json()
            via_claude = await _call(world, client, tokens["access_token"], tool)
            assert via_grok["result"] == via_claude["result"], tool


async def test_neither_credential_works_on_the_other_endpoint(sessionmaker):
    world = await _world(sessionmaker)
    async with sessionmaker() as session:
        grok_token, _ = await grants.create_grant(session, world.clock, scopes=["memory"], ttl_hours=1)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        assert (await world.mcp(client, grok_token)).status == 401
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        for claude_token in (tokens["access_token"], tokens["refresh_token"]):
            assert (await client.post(f"/mcp/{claude_token}", json=ping)).status == 404
        # And a refresh token is not an access token.
        assert (await world.mcp(client, tokens["refresh_token"])).status == 401


async def test_the_limiters_are_independent(sessionmaker):
    world = await _world(sessionmaker, CLAUDE_MAX_CALLS_PER_MINUTE=2, GROK_MAX_CALLS_PER_MINUTE=2)
    async with sessionmaker() as session:
        grok_token, _ = await grants.create_grant(session, world.clock, scopes=["memory"], ttl_hours=1)
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        claude = [(await world.mcp(client, tokens["access_token"], "ping")).status for _ in range(3)]
        grok = [(await client.post(f"/mcp/{grok_token}", json=ping)).status for _ in range(2)]
    assert claude == [200, 200, 429]
    assert grok == [200, 200]


async def test_revoke_closes_claude_windows_and_grok_grants(sessionmaker):
    world = await _world(sessionmaker)
    async with sessionmaker() as session:
        grok_token, _ = await grants.create_grant(session, world.clock, scopes=["memory"], ttl_hours=1)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        await world.open_window(["journal"])
        assert await world.command("/revoke") == "Доступ закрыт: Grok (1), Claude (1)."
        assert (await _call(world, client, tokens["access_token"], "get_journal"))["result"]["isError"]
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        assert (await client.post(f"/mcp/{grok_token}", json=ping)).status == 404
        # The connection itself survives /revoke: a new window works.
        await world.open_window(["journal"])
        assert not (await _call(world, client, tokens["access_token"], "get_journal"))["result"]["isError"]


async def test_revoke_with_only_claude_windows(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.connect(client)
        await world.open_window(["journal"])
        assert await world.command("/revoke") == "Доступ закрыт: Grok (0), Claude (1)."
        assert await world.command("/revoke") == "Открытых доступов нет."


async def test_grok_s_picker_lists_only_grok_grants(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.connect(client)
        await world.open_window(["journal"])
    from app.tg import grok as grok_ui

    text = await grok_ui.opening_text(sessionmaker, world.clock, 0, 0)
    assert "Сейчас открыто" not in text


# --- /claude ---


async def test_claude_refuses_when_disabled(sessionmaker):
    world = await _world(sessionmaker, CLAUDE_ACCESS_ENABLED=False)
    assert await world.command("/claude") == claude_ui.DISABLED
    assert await world.command("/claude connect ABCDEF") == claude_ui.DISABLED


async def test_claude_without_a_connection_shows_the_setup(sessionmaker):
    world = await _world(sessionmaker)
    reply = await world.command("/claude")
    assert reply.startswith("Нет подключения.")
    assert R in reply
    assert world.tg_fake.sent[-1].reply_markup is None


async def test_claude_with_a_connection_shows_status_and_an_all_off_picker(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.connect(client)
    reply = await world.command("/claude")
    assert reply.startswith("Подключение #")
    assert "Anthropic" in reply
    labels = [b.text for row in world.tg_fake.sent[-1].reply_markup.inline_keyboard for b in row]
    assert all(label.startswith("▫️") for label in labels[:4])
    assert claude_ui.OPEN in labels


async def test_a_stale_or_empty_press_opens_nothing(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.connect(client)
    await world.press(f"cl:ok:2:0:0:{world.epoch() - 3600}")
    await world.press(f"cl:ok:0:0:0:{world.epoch()}")
    await world.press("cl:ok:junk")
    async with sessionmaker() as session:
        assert (await session.execute(select(AccessGrant))).first() is None


async def test_a_press_after_disconnect_opens_nothing(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.connect(client)
    await world.command("/claude disconnect")
    await world.open_window(["journal"])
    assert world.tg_fake.edits[-1].text == claude_ui.GONE_TEXT
    async with sessionmaker() as session:
        assert (await session.execute(select(AccessGrant))).first() is None


async def test_unknown_subcommands_get_usage(sessionmaker):
    world = await _world(sessionmaker)
    assert await world.command("/claude status now") == claude_ui.USAGE
    assert await world.command("/claude connect") == claude_ui.USAGE


def test_the_web_chat_cannot_reach_claude():
    assert ingress.is_blocked_command("/claude")
    assert ingress.is_blocked_command("/CLAUDE@anchor_bot connect ABCDEF")
    assert "cl:ok:2:0:0:0".startswith(ingress.BLOCKED_CALLBACK_PREFIX)
    assert not ingress.is_blocked_command("/revoke")


async def test_a_web_sink_bot_gets_nothing(sessionmaker):
    world = await _world(sessionmaker)
    world.tg_bot.is_web_sink = True
    async with TestClient(TestServer(world.app)) as client:
        _handle, code, _cookie = await world.start(client)
        reply = await world.command(f"/claude connect {code}")
    assert not reply.startswith("Подтверждено")
    assert await world.request_rows() == 0


# --- /delete ---


async def test_delete_wipes_the_connection_and_the_pending_store(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        await world.start(client)
        assert len(world.pending) == 1
        await data_ui.handle_delete_callback(
            sessionmaker,
            world.tg_bot,
            world.settings,
            world.clock,
            callback_id="cb",
            chat_id=CHAT_ID,
            message_id=1,
            data=f"d:yes:{int(time.time())}",
            claude_pending=world.pending,
        )
        assert len(world.pending) == 0
        assert (await world.mcp(client, tokens["access_token"])).status == 401


async def test_delete_also_voids_pending_web_login_codes(sessionmaker):
    """A web login code issued just before /delete must not open a
    session after it (the same wipe as Claude's pending requests)."""
    from app.web import auth as web_auth

    world = await _world(sessionmaker)
    store = web_auth.CodeStore()
    code = store.issue("pre-token", world.clock, ttl_s=300)
    await data_ui.handle_delete_callback(
        sessionmaker,
        world.tg_bot,
        world.settings,
        world.clock,
        callback_id="cb",
        chat_id=CHAT_ID,
        message_id=1,
        data=f"d:yes:{int(time.time())}",
        code_store=store,
    )
    assert store.verify("pre-token", code, world.clock) is False

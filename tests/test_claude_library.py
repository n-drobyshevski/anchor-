"""The library (C3): the standing switch, search_library, and its
refusals. Connector plan sections 6 and 9; docs/decisions.md, "C3 --
search_library without the failed threshold" and "Index knowledge
notes only".
"""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select, update

from app.core import grants
from app.db.models import NoteChunkKnowledge, UserState, VaultFile
from app.tg import claude as claude_ui
from app.vault import notes_knowledge
from app.vault._chunks import Chunk
from app.web import ingress
from claude_helpers import World, settings

pytestmark = pytest.mark.asyncio

CLOSED_TEXT = "Библиотека закрыта. Включи в Telegram: /claude library on"
NOTES_OFF_TEXT = "Заметки выключены. Включи в Telegram: /vault notes on"
EMPTY_TEXT = "В библиотеке ничего не нашлось."


async def _world(sessionmaker, **overrides) -> World:
    overrides.setdefault("VAULT_KNOWLEDGE_ENABLED", True)
    world = World(sessionmaker, settings(**overrides))
    await world.seed()
    return world


async def _set_notes(sessionmaker, on: bool) -> None:
    async with sessionmaker() as session:
        await session.execute(update(UserState).values(notes_consent=on))
        await session.commit()


async def _seed_knowledge(sessionmaker, chunks: list[Chunk]) -> None:
    await _set_notes(sessionmaker, True)
    async with sessionmaker() as session:
        vault_file = VaultFile(path="Library/CCRU.md", role="note", note_class="knowledge")
        session.add(vault_file)
        await session.commit()
        await notes_knowledge.replace_chunks(session, vault_file.id, chunks)
        await session.commit()


async def _search(world, client, token, query: str = "гиперстишн ускорение") -> dict:
    resp = await world.mcp(
        client, token, "tools/call", {"name": "search_library", "arguments": {"query": query}}
    )
    assert resp.status == 200
    return (await resp.json())["result"]


# --- the standing switch itself ---


async def test_a_new_connection_starts_with_the_library_off(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.connect(client)
    reply = await world.command("/claude")
    assert "Библиотека: выключена." in reply
    async with sessionmaker() as session:
        from app.db.models import OauthConnection

        row = (await session.execute(select(OauthConnection))).scalar_one()
    assert row.library_read is False


async def test_a_replaced_connection_also_starts_with_the_library_off(sessionmaker):
    """A second connection revokes the first (connector plan section 5)
    -- and the fresh one it replaces it with starts off too, exactly
    like the very first connection ever made."""
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.connect(client)
        await world.command("/claude library on")
        await world.connect(client)  # a second connection, replacing the first
    reply = await world.command("/claude")
    assert "Библиотека: выключена." in reply


async def test_claude_library_with_no_connection(sessionmaker):
    world = await _world(sessionmaker)
    assert await world.command("/claude library on") == claude_ui.LIBRARY_NO_CONNECTION


async def test_claude_library_on_and_off_and_the_status_line(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.connect(client)
    assert await world.command("/claude library on") == claude_ui.LIBRARY_SET_ON
    assert "Библиотека: включена." in await world.command("/claude")
    assert await world.command("/claude library off") == claude_ui.LIBRARY_SET_OFF
    assert "Библиотека: выключена." in await world.command("/claude")
    assert await world.command("/claude library maybe") == claude_ui.LIBRARY_USAGE


async def test_revoke_turns_the_library_off_without_disconnecting(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.connect(client)
    await world.command("/claude library on")
    await world.command("/revoke")
    assert "Библиотека: выключена." in await world.command("/claude")
    # The connection itself survived /revoke (same guarantee as the
    # window tests in tests/test_claude_access.py).
    assert "Нет подключения" not in await world.command("/claude")


async def test_disconnect_turns_the_library_off(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.connect(client)
    await world.command("/claude library on")
    await world.command("/claude disconnect")
    assert (await world.command("/claude")).startswith("Нет подключения.")
    async with sessionmaker() as session:
        from app.db.models import OauthConnection

        row = (await session.execute(select(OauthConnection))).scalar_one()
    assert row.revoked_at is not None
    assert row.library_read is False


# --- search_library through the MCP endpoint ---


async def test_search_library_is_closed_with_the_switch_off(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        result = await _search(world, client, tokens["access_token"])
    assert result == {"content": [{"type": "text", "text": CLOSED_TEXT}], "isError": True}


async def test_search_library_needs_no_window_at_all(sessionmaker):
    """The defining C3 property: the switch, not a window."""
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        await world.command("/claude library on")
        await _seed_knowledge(sessionmaker, [Chunk("CCRU", "Гиперстишн и ускорение.")])
        # No open_window() call anywhere above.
        result = await _search(world, client, tokens["access_token"])
    assert result["isError"] is False
    assert result["content"][0]["text"] == "«CCRU»: Гиперстишн и ускорение."


async def test_search_library_refuses_when_notes_are_off(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        await world.command("/claude library on")
        await _set_notes(sessionmaker, False)
        result = await _search(world, client, tokens["access_token"])
    assert result == {"content": [{"type": "text", "text": NOTES_OFF_TEXT}], "isError": True}


async def test_search_library_refuses_when_the_setting_is_off(sessionmaker):
    world = await _world(sessionmaker, VAULT_KNOWLEDGE_ENABLED=False)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        await world.command("/claude library on")
        await _set_notes(sessionmaker, True)
        result = await _search(world, client, tokens["access_token"])
    assert result["isError"] is True
    assert result["content"][0]["text"] == NOTES_OFF_TEXT


async def test_search_library_empty_result_is_not_an_error(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        await world.command("/claude library on")
        await _set_notes(sessionmaker, True)
        result = await _search(world, client, tokens["access_token"], query="ничего похожего тут нет")
    assert result == {"content": [{"type": "text", "text": EMPTY_TEXT}], "isError": False}


async def test_search_library_carries_no_id_or_path(sessionmaker):
    """The strongest form of this check: the whole JSON-RPC response
    (not just the one text field, so an id or path smuggled into a
    different key would still be caught) equals exactly the known-good
    heading-and-text string -- there is no room in an exact-equality
    assertion for a chunk id, a file id or the file's path to sneak in
    anywhere, including as an extra field or a different formatting."""
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        await world.command("/claude library on")
        await _seed_knowledge(sessionmaker, [Chunk("CCRU", "Гиперстишн и ускорение.")])
        async with sessionmaker() as session:
            # Sanity: there really is an id and a path behind this chunk,
            # so "never appears" below is a real claim, not vacuous.
            assert (await session.execute(select(NoteChunkKnowledge.id))).scalar_one() >= 1
            assert (await session.execute(select(VaultFile.path))).scalar_one() == "Library/CCRU.md"
        result = await _search(world, client, tokens["access_token"])
    assert result == {
        "content": [{"type": "text", "text": "«CCRU»: Гиперстишн и ускорение."}],
        "isError": False,
    }
    assert "Library/CCRU.md" not in json.dumps(result, ensure_ascii=False)


async def test_search_library_never_returns_personal_chunks(sessionmaker):
    from app.vault import notes_personal

    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        await world.command("/claude library on")
        await _set_notes(sessionmaker, True)
        async with sessionmaker() as session:
            personal = VaultFile(path="Жизнь/Бег.md", role="note", note_class="personal")
            session.add(personal)
            await session.commit()
            await notes_personal.replace_chunks(
                session, personal.id, [Chunk("Бег", "Гиперстишн и ускорение.")]
            )
            await session.commit()
        result = await _search(world, client, tokens["access_token"])
    assert result == {"content": [{"type": "text", "text": EMPTY_TEXT}], "isError": False}


async def test_search_library_is_always_listed_but_grok_never_gets_it(sessionmaker):
    world = await _world(sessionmaker)
    async with sessionmaker() as session:
        grok_token, _ = await grants.create_grant(session, world.clock, scopes=["memory"], ttl_hours=1)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        listed = await (await world.mcp(client, tokens["access_token"])).json()
        names = {t["name"] for t in listed["result"]["tools"]}
        assert "search_library" in names

        grok_listed = await (await client.post(
            f"/mcp/{grok_token}", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )).json()
        grok_names = {t["name"] for t in grok_listed["result"]["tools"]}
        assert "search_library" not in grok_names

        grok_call = await (await client.post(
            f"/mcp/{grok_token}",
            json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "search_library", "arguments": {"query": "x"}},
            },
        )).json()
    assert grok_call.get("error", {}).get("code") == -32602


def test_the_web_chat_cannot_run_claude_library_on():
    """/claude library on|off is still just the /claude command as far
    as app/web/ingress.py's blocklist sees it -- it checks the command
    word only, never the arguments -- so it is blocked by the same
    entry that blocks plain /claude and /claude connect."""
    assert ingress.is_blocked_command("/claude library on")
    assert ingress.is_blocked_command("/claude library off")
    assert ingress.is_blocked_command("/CLAUDE@anchor_bot library on")


# --- read counting ---


async def test_a_refused_call_does_not_count_a_read(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        await _search(world, client, tokens["access_token"])  # switch off: refused
        await world.command("/claude library on")
        await _search(world, client, tokens["access_token"])  # notes off: refused
    async with sessionmaker() as session:
        assert await grants.library_read_count(session, world.clock.now_utc().date()) == 0


async def test_a_successful_call_counts_and_the_row_holds_no_text(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        await world.command("/claude library on")
        await _seed_knowledge(sessionmaker, [Chunk("CCRU", "Гиперстишн и ускорение.")])
        await _search(world, client, tokens["access_token"])
        await _search(world, client, tokens["access_token"], query="ничего похожего тут нет")
    async with sessionmaker() as session:
        from app.db.models import ClaudeLibraryRead

        row = (await session.execute(select(ClaudeLibraryRead))).scalar_one()
    assert row.count == 2
    assert not hasattr(row, "query") and not hasattr(row, "text") and not hasattr(row, "heading")


# --- /delete ---


async def test_delete_wipes_the_switch_and_the_counter(sessionmaker):
    from app.config import Settings
    from app.core import purge
    from app.db.models import ClaudeLibraryRead, OauthConnection

    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.connect(client)
        await world.command("/claude library on")
    async with sessionmaker() as session:
        await grants.record_library_read(session, world.clock.now_utc().date())
        connection = (await session.execute(select(OauthConnection))).scalar_one()
        assert connection.library_read is True
        await purge.delete_everything(session, Settings(), world.clock)
    async with sessionmaker() as session:
        assert (await session.execute(select(OauthConnection))).first() is None
        assert (await session.execute(select(ClaudeLibraryRead))).first() is None

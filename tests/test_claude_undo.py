"""Undo: the MCP tool, `/claude undo`, `/claude undo all` (W2b, plan
section 6.2).
"""

from __future__ import annotations

import datetime
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from app.core import claude_write_limits as limits
from app.db.models import ClaudeChangeset, OauthConnection
from app.tg import claude as claude_ui
from app.web import mcp_core
from claude_helpers import World, settings
from tests.claude_write_fake import FakeKnowledgeVault, sha, start_fake_vaultd

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def world_factory(sessionmaker):
    """Builds a World wired to a real, running fake vaultd HTTP server --
    needed here (unlike tests/test_claude_write.py's MCP-only tests)
    because `/claude undo` runs through app/tg/claude.py's own
    `VaultClient.from_settings`, not through the MCP dispatcher's
    injectable `vault_client_factory`; both paths must see the same
    vault, so both go over the same real HTTP round trip.
    """
    servers = []

    async def make(**overrides) -> tuple[World, FakeKnowledgeVault]:
        vault, server = await start_fake_vaultd()
        servers.append(server)
        world = World(sessionmaker, settings(VAULT_URL=vault.url, VAULT_API_TOKEN="t" * 20, **overrides))
        await world.seed()
        return world, vault

    yield make
    for server in servers:
        await server.close()


async def _call(world, client, token, name, arguments) -> dict:
    resp = await world.mcp(client, token, "tools/call", {"name": name, "arguments": arguments})
    assert resp.status == 200, await resp.text()
    return (await resp.json())["result"]


async def _tool_payload(world, client, token, name, arguments) -> dict:
    result = await _call(world, client, token, name, arguments)
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


async def _create(world, client, token, title: str) -> dict:
    return await _tool_payload(
        world, client, token, "create_note", {"folder": "Library", "title": title, "body": "x"}
    )


async def _open_write(world, client) -> dict:
    tokens = await world.connect(client)
    await world.command("/claude library on")
    await world.command("/claude library write on")
    return tokens


# --- the MCP tool ---------------------------------------------------------


async def test_undo_changeset_tool_restores_the_file(world_factory):
    world, vault = await world_factory()
    async with TestClient(TestServer(world.app)) as client:
        tokens = await _open_write(world, client)
        payload = await _create(world, client, tokens["access_token"], "New")
        result = await _tool_payload(
            world, client, tokens["access_token"], "undo_changeset", {"id": payload["changeset_id"]}
        )
    assert result == {"restored": 1, "refused": 0}
    assert "Library/New.md" not in vault.files


async def test_undo_of_an_unknown_changeset_is_the_one_refusal_text(world_factory):
    world, vault = await world_factory()
    async with TestClient(TestServer(world.app)) as client:
        tokens = await _open_write(world, client)
        result = await _call(world, client, tokens["access_token"], "undo_changeset", {"id": 999999})
    assert result == {"content": [{"type": "text", "text": mcp_core.WRITE_REFUSED_TEXT}], "isError": True}


async def test_undo_refused_count_when_the_file_changed_since(world_factory):
    world, vault = await world_factory()
    vault.files["Library/CCRU.md"] = "old"
    async with TestClient(TestServer(world.app)) as client:
        tokens = await _open_write(world, client)
        payload = await _tool_payload(
            world, client, tokens["access_token"], "update_note",
            {"path": "Library/CCRU.md", "new_body": "new", "base_hash": sha("old")},
        )
        # The user edits the file on their phone before Claude undoes.
        vault.files["Library/CCRU.md"] = "edited on phone"
        result = await _tool_payload(
            world, client, tokens["access_token"], "undo_changeset", {"id": payload["changeset_id"]}
        )
    assert result == {"restored": 0, "refused": 1}
    assert vault.files["Library/CCRU.md"] == "edited on phone"


async def _seed_undo_rows(sessionmaker, connection_id: int, clock, count: int) -> None:
    """`count` already-recorded undo changesets within the last hour --
    the precondition for the undo-cap tests, seeded directly rather
    than by actually undoing `count` real changesets (which would
    itself collide with CHANGESETS_PER_HOUR and the OAuth access
    token's own 60-minute TTL long before UNDOS_PER_HOUR is reached)."""
    async with sessionmaker() as session:
        for i in range(count):
            when = clock.now_utc() - datetime.timedelta(minutes=5 * (count - i))
            session.add(
                ClaudeChangeset(
                    connection_id=connection_id,
                    vault_ref=f"seed-undo-{i}",
                    kind="undo",
                    files=1,
                    bytes=0,
                    created_at=when,
                    last_write_at=when,
                )
            )
        await session.commit()


async def test_undo_cap_per_hour(world_factory, sessionmaker):
    world, vault = await world_factory()
    async with TestClient(TestServer(world.app)) as client:
        tokens = await _open_write(world, client)
        payload = await _create(world, client, tokens["access_token"], "New")
        async with sessionmaker() as session:
            connection = (await session.execute(select(OauthConnection))).scalars().one()
        await _seed_undo_rows(sessionmaker, connection.id, world.clock, limits.UNDOS_PER_HOUR)
        result = await _call(
            world, client, tokens["access_token"], "undo_changeset", {"id": payload["changeset_id"]}
        )
    assert result == {"content": [{"type": "text", "text": mcp_core.WRITE_REFUSED_TEXT}], "isError": True}
    assert "Library/New.md" in vault.files  # the cap refused it; nothing was restored


async def test_undo_changeset_works_with_write_switch_off_but_not_without_a_connection(world_factory):
    world, vault = await world_factory()
    async with TestClient(TestServer(world.app)) as client:
        tokens = await _open_write(world, client)
        payload = await _create(world, client, tokens["access_token"], "New")
        await world.command("/claude library write off")
        result = await _tool_payload(
            world, client, tokens["access_token"], "undo_changeset", {"id": payload["changeset_id"]}
        )
    assert result == {"restored": 1, "refused": 0}


async def test_undo_changeset_refuses_without_a_connection(world_factory):
    world, vault = await world_factory()
    async with TestClient(TestServer(world.app)) as client:
        tokens = await _open_write(world, client)
        payload = await _create(world, client, tokens["access_token"], "New")
        await world.command("/claude disconnect")
        result = await world.mcp(
            client, tokens["access_token"], "tools/call",
            {"name": "undo_changeset", "arguments": {"id": payload["changeset_id"]}},
        )
    assert result.status == 401


# --- an undo cannot be undone ---------------------------------------------


async def test_an_undo_cannot_itself_be_undone(world_factory, sessionmaker):
    world, vault = await world_factory()
    async with TestClient(TestServer(world.app)) as client:
        tokens = await _open_write(world, client)
        payload = await _create(world, client, tokens["access_token"], "New")
        await _tool_payload(
            world, client, tokens["access_token"], "undo_changeset", {"id": payload["changeset_id"]}
        )
        async with sessionmaker() as session:
            undo_row = (
                await session.execute(
                    select(ClaudeChangeset).where(ClaudeChangeset.kind == "undo")
                )
            ).scalars().one()
        result = await _call(
            world, client, tokens["access_token"], "undo_changeset", {"id": undo_row.id}
        )
    assert result == {"content": [{"type": "text", "text": mcp_core.WRITE_REFUSED_TEXT}], "isError": True}


# --- /claude undo / undo all ----------------------------------------------


async def test_claude_undo_undoes_the_most_recent_changeset(world_factory):
    world, vault = await world_factory()
    async with TestClient(TestServer(world.app)) as client:
        tokens = await _open_write(world, client)
        await _create(world, client, tokens["access_token"], "A")
        world.clock.advance(datetime.timedelta(minutes=11))
        await _create(world, client, tokens["access_token"], "B")
    reply = await world.command("/claude undo")
    assert reply == "Откатил: 1 файл."
    assert "Library/B.md" not in vault.files
    assert "Library/A.md" in vault.files


async def test_claude_undo_all_undoes_last_24h_newest_first(world_factory):
    world, vault = await world_factory()
    async with TestClient(TestServer(world.app)) as client:
        tokens = await _open_write(world, client)
        await _create(world, client, tokens["access_token"], "A")
        world.clock.advance(datetime.timedelta(minutes=11))
        await _create(world, client, tokens["access_token"], "B")
    reply = await world.command("/claude undo all")
    assert reply == "Откатил: 2 файла."
    assert "Library/A.md" not in vault.files
    assert "Library/B.md" not in vault.files


async def test_claude_undo_all_only_reaches_24_hours(world_factory, sessionmaker):
    """`/claude undo all` reaches the last 24h, not further -- a
    changeset older than that is left alone."""
    world, vault = await world_factory()
    async with TestClient(TestServer(world.app)) as client:
        tokens = await _open_write(world, client)
        await _create(world, client, tokens["access_token"], "Recent")
    async with sessionmaker() as session:
        connection = (await session.execute(select(OauthConnection))).scalars().one()
        old = world.clock.now_utc() - datetime.timedelta(hours=25)
        session.add(
            ClaudeChangeset(
                connection_id=connection.id, vault_ref="old-one", kind="write",
                files=1, bytes=1, created_at=old, last_write_at=old,
            )
        )
        await session.commit()
    # A real entry, not an empty one: if the 24h window were broken
    # (e.g. unbounded), this changeset would actually get restored
    # (the file deleted, since it is a "create" entry) rather than the
    # assertions passing vacuously because there was nothing to undo.
    from tests.claude_write_fake import _Entry, sha

    vault.files["Library/OldFile.md"] = "still there"
    vault.changesets["old-one"] = [_Entry("Library/OldFile.md", None, sha("still there"))]
    reply = await world.command("/claude undo all")
    assert reply == "Откатил: 1 файл."
    assert "Library/Recent.md" not in vault.files
    assert "Library/OldFile.md" in vault.files  # untouched: older than 24h
    async with sessionmaker() as session:
        old_row = (
            await session.execute(select(ClaudeChangeset).where(ClaudeChangeset.vault_ref == "old-one"))
        ).scalars().one()
    assert old_row.undone_at is None


async def test_claude_undo_nothing_to_undo(world_factory):
    world, vault = await world_factory()
    async with TestClient(TestServer(world.app)) as client:
        await _open_write(world, client)
    assert await world.command("/claude undo") == claude_ui.UNDO_NOTHING
    assert await world.command("/claude undo all") == claude_ui.UNDO_NOTHING


async def test_claude_undo_reports_refused_count_and_plural(world_factory):
    world, vault = await world_factory()
    vault.files["Library/CCRU.md"] = "old"
    async with TestClient(TestServer(world.app)) as client:
        tokens = await _open_write(world, client)
        await world.mcp(
            client, tokens["access_token"], "tools/call",
            {"name": "update_note", "arguments": {
                "path": "Library/CCRU.md", "new_body": "new", "base_hash": sha("old")
            }},
        )
        vault.files["Library/CCRU.md"] = "edited on phone"
    reply = await world.command("/claude undo")
    assert reply == "Откатил: 0 файлов. Не откатил 1: их изменили после Claude."


async def test_claude_undo_works_with_write_off_but_not_without_a_connection(world_factory):
    world, vault = await world_factory()
    async with TestClient(TestServer(world.app)) as client:
        tokens = await _open_write(world, client)
        await _create(world, client, tokens["access_token"], "A")
        await world.command("/claude library write off")
    # Write is off, but undo only restores the user's own text -- it works.
    assert await world.command("/claude undo") == "Откатил: 1 файл."
    assert await world.command("/claude undo") == claude_ui.UNDO_NOTHING
    # Disconnecting removes the connection undo itself still needs.
    await world.command("/claude disconnect")
    assert await world.command("/claude undo") == claude_ui.LIBRARY_NO_CONNECTION


async def test_claude_undo_no_connection(world_factory):
    world, vault = await world_factory()
    assert await world.command("/claude undo") == claude_ui.LIBRARY_NO_CONNECTION


async def test_claude_undo_cap_reply(world_factory, sessionmaker):
    world, vault = await world_factory()
    async with TestClient(TestServer(world.app)) as client:
        tokens = await _open_write(world, client)
        await _create(world, client, tokens["access_token"], "New")
        async with sessionmaker() as session:
            connection = (await session.execute(select(OauthConnection))).scalars().one()
    await _seed_undo_rows(sessionmaker, connection.id, world.clock, limits.UNDOS_PER_HOUR)
    assert await world.command("/claude undo") == claude_ui.UNDO_CAP


async def test_web_chat_cannot_run_claude_undo(sessionmaker):
    """the web chat cannot run /claude at all (BLOCKED_COMMANDS already
    has claude); this is that guard proven for /claude undo
    specifically."""
    from app.web import ingress

    assert ingress.is_blocked_command("/claude undo") is True
    assert ingress.is_blocked_command("/claude undo all") is True

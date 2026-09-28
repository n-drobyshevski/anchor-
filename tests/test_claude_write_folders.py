"""Rev. 3: folder auto-creation and the move budget, on the bot side
(anchor-claude-write-plan.md section 14).
"""

from __future__ import annotations

import datetime
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.core import claude_write_limits as limits
from app.db.models import ClaudeChangeset, OauthConnection
from app.vault.client import Tree, TreeNote
from app.web import claude_write, mcp_core
from app.web.claude_write import Refused
from claude_helpers import World, settings
from tests.claude_write_fake import FakeKnowledgeVault

pytestmark = pytest.mark.asyncio

WRITE_REFUSED_TEXT = mcp_core.WRITE_REFUSED_TEXT


async def _world(sessionmaker, vault: FakeKnowledgeVault | None = None, **overrides):
    world = World(sessionmaker, settings(**overrides))
    await world.seed()
    vault = vault if vault is not None else FakeKnowledgeVault()
    world.app["vault_client_factory"] = lambda _settings, _v=vault: _v
    return world, vault


async def _connect_and_open(world: World, client) -> dict:
    tokens = await world.connect(client)
    await world.command("/claude library on")
    await world.command("/claude library write on")
    return tokens


async def _call(world, client, token, name, arguments) -> dict:
    resp = await world.mcp(client, token, "tools/call", {"name": name, "arguments": arguments})
    assert resp.status == 200, await resp.text()
    return (await resp.json())["result"]


async def _tool_payload(world, client, token, name, arguments) -> dict:
    result = await _call(world, client, token, name, arguments)
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


async def _connection(sessionmaker) -> OauthConnection:
    now = datetime.datetime.now(datetime.timezone.utc)
    async with sessionmaker() as session:
        connection = OauthConnection(
            client_id="direct-test", created_at=now, expires_at=now + datetime.timedelta(days=30)
        )
        session.add(connection)
        await session.commit()
        await session.refresh(connection)
        return connection


# --- create_note's nested folder sanitising ---------------------------------


async def test_create_note_accepts_a_nested_folder(sessionmaker):
    world, vault = await _world(sessionmaker)
    vault.next_folders_created = 2
    async with TestClient(TestServer(world.app)) as client:
        tokens = await _connect_and_open(world, client)
        payload = await _tool_payload(
            world, client, tokens["access_token"], "create_note",
            {"folder": "Library/Philosophy/Stoicism", "title": "Seneca", "body": "x"},
        )
    assert payload["path"] == "Library/Philosophy/Stoicism/Seneca.md"


@pytest.mark.parametrize(
    "folder",
    # "" is excluded here: an empty `folder` argument never reaches
    # sanitize_folder at all -- mcp_core.py's own `_str_arg` refuses it
    # as a malformed tool call before any write-path code runs, which
    # test_sanitize_folder_rejects_bad_segments below covers directly.
    ["Library/..", "Library/../Evil", "Library//Sub", "Library/.hidden", "/", ".."],
)
async def test_create_note_rejects_a_bad_folder_segment(sessionmaker, folder):
    world, vault = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await _connect_and_open(world, client)
        result = await _call(
            world, client, tokens["access_token"], "create_note",
            {"folder": folder, "title": "New", "body": "x"},
        )
    assert result == {"content": [{"type": "text", "text": WRITE_REFUSED_TEXT}], "isError": True}
    assert not vault.files


def test_sanitize_folder_rejects_bad_segments():
    for folder in ["Library/..", "Library/../Evil", "Library//Sub", "Library/.hidden", "", "/", ".."]:
        with pytest.raises(Refused) as exc:
            claude_write.sanitize_folder(folder)
        assert exc.value.code == "bad_folder"


def test_sanitize_folder_accepts_a_nested_path():
    assert claude_write.sanitize_folder("Library/Philosophy/Stoicism") == "Library/Philosophy/Stoicism"
    assert claude_write.sanitize_folder("/Library/") == "Library"


# --- mirrored caps, ledger-backed -------------------------------------------


async def test_cap_folders_per_changeset_is_ledger_backed(sessionmaker):
    world, vault = await _world(sessionmaker)
    connection = await _connection(sessionmaker)
    async with sessionmaker() as session:
        row = ClaudeChangeset(
            connection_id=connection.id, vault_ref="cs", kind="write",
            folders=limits.FOLDERS_PER_CHANGESET, created_at=world.clock.now_utc(),
            last_write_at=world.clock.now_utc(),
        )
        session.add(row)
        await session.commit()
    async with sessionmaker() as session:
        with pytest.raises(Refused) as exc:
            await claude_write.create_note(session, world.clock, vault, connection.id, "Library", "N", "x")
    assert exc.value.code == "cap_folders"
    assert vault.calls == []


async def test_cap_folders_per_changeset_also_blocks_rename(sessionmaker):
    world, vault = await _world(sessionmaker)
    connection = await _connection(sessionmaker)
    async with sessionmaker() as session:
        row = ClaudeChangeset(
            connection_id=connection.id, vault_ref="cs", kind="write",
            folders=limits.FOLDERS_PER_CHANGESET, created_at=world.clock.now_utc(),
            last_write_at=world.clock.now_utc(),
        )
        session.add(row)
        await session.commit()
    async with sessionmaker() as session:
        with pytest.raises(Refused) as exc:
            await claude_write.rename_note(
                session, world.clock, vault, connection.id, "Library/A.md", "Library/B.md", "a" * 64
            )
    assert exc.value.code == "cap_folders"
    assert vault.calls == []


async def test_cap_moves_per_changeset_is_ledger_backed(sessionmaker):
    world, vault = await _world(sessionmaker)
    connection = await _connection(sessionmaker)
    async with sessionmaker() as session:
        row = ClaudeChangeset(
            connection_id=connection.id, vault_ref="cs", kind="write",
            moves=limits.MOVE_FILES_PER_CHANGESET, created_at=world.clock.now_utc(),
            last_write_at=world.clock.now_utc(),
        )
        session.add(row)
        await session.commit()
    async with sessionmaker() as session:
        with pytest.raises(Refused) as exc:
            await claude_write.rename_note(
                session, world.clock, vault, connection.id, "Library/A.md", "Library/B.md", "a" * 64
            )
    assert exc.value.code == "cap_moves"
    assert vault.calls == []


async def test_rename_note_no_longer_spends_the_content_files_cap(sessionmaker):
    """Rev. 3: a changeset that already has FILES_PER_CHANGESET content
    writes can still rename a note -- the two budgets are independent."""
    world, vault = await _world(sessionmaker)
    vault.files["Library/Old.md"] = "content"
    async with TestClient(TestServer(world.app)) as client:
        tokens = await _connect_and_open(world, client)
        for i in range(limits.FILES_PER_CHANGESET):
            await _tool_payload(
                world, client, tokens["access_token"], "create_note",
                {"folder": "Library", "title": f"N{i}", "body": "x"},
            )
        from tests.claude_write_fake import sha

        payload = await _tool_payload(
            world, client, tokens["access_token"], "rename_note",
            {"path": "Library/Old.md", "new_path": "Library/New.md", "base_hash": sha("content")},
        )
    assert payload["path"] == "Library/New.md"


async def test_create_note_records_folders_created_on_the_ledger(sessionmaker):
    world, vault = await _world(sessionmaker)
    vault.next_folders_created = 2
    async with TestClient(TestServer(world.app)) as client:
        tokens = await _connect_and_open(world, client)
        payload = await _tool_payload(
            world, client, tokens["access_token"], "create_note",
            {"folder": "Library/New", "title": "N", "body": "x"},
        )
    from sqlalchemy import select

    async with sessionmaker() as session:
        row = (await session.execute(select(ClaudeChangeset).where(ClaudeChangeset.id == payload["changeset_id"]))).scalar_one()
    assert row.folders == 2


async def test_rename_note_records_moves_and_folders_on_the_ledger(sessionmaker):
    world, vault = await _world(sessionmaker)
    vault.files["Library/Old.md"] = "content"
    vault.next_relinked = 3
    vault.next_folders_created = 1
    async with TestClient(TestServer(world.app)) as client:
        tokens = await _connect_and_open(world, client)
        from tests.claude_write_fake import sha

        payload = await _tool_payload(
            world, client, tokens["access_token"], "rename_note",
            {"path": "Library/Old.md", "new_path": "Library/New.md", "base_hash": sha("content")},
        )
    from sqlalchemy import select

    async with sessionmaker() as session:
        row = (await session.execute(select(ClaudeChangeset).where(ClaudeChangeset.id == payload["changeset_id"]))).scalar_one()
    assert row.moves == 4  # 1 + relinked
    assert row.folders == 1
    assert row.files == 0  # never spent -- rev. 3's own budget


async def test_cap_folders_per_day_is_ledger_backed(sessionmaker):
    """Each seeded changeset is its own row, over an hour before "now"
    (CHANGESETS_PER_HOUR would otherwise refuse minting a new one for
    an unrelated reason before the folder-day check is ever reached),
    but all still after local midnight (FOLDERS_PER_DAY sums the local
    calendar day, not a rolling 24h)."""
    world, vault = await _world(sessionmaker)
    connection = await _connection(sessionmaker)
    async with sessionmaker() as session:
        for i in range(limits.FOLDERS_PER_DAY):
            when = world.clock.now_utc() - datetime.timedelta(minutes=65 + 30 * i)
            session.add(
                ClaudeChangeset(
                    connection_id=connection.id, vault_ref=f"seed-{i}", kind="write",
                    folders=1, created_at=when, last_write_at=when,
                )
            )
        await session.commit()
    async with sessionmaker() as session:
        with pytest.raises(Refused) as exc:
            await claude_write.create_note(session, world.clock, vault, connection.id, "Library", "N", "x")
    assert exc.value.code == "cap_folders_day"
    assert vault.calls == []


async def test_cap_moves_per_day_is_ledger_backed(sessionmaker):
    world, vault = await _world(sessionmaker)
    connection = await _connection(sessionmaker)
    async with sessionmaker() as session:
        for i in range(limits.MOVES_PER_DAY):
            when = world.clock.now_utc() - datetime.timedelta(minutes=65 + 3 * i)
            session.add(
                ClaudeChangeset(
                    connection_id=connection.id, vault_ref=f"seed-{i}", kind="write",
                    moves=1, created_at=when, last_write_at=when,
                )
            )
        await session.commit()
    async with sessionmaker() as session:
        with pytest.raises(Refused) as exc:
            await claude_write.rename_note(
                session, world.clock, vault, connection.id, "Library/A.md", "Library/B.md", "a" * 64
            )
    assert exc.value.code == "cap_moves_day"
    assert vault.calls == []


# --- list_tree ---------------------------------------------------------------


async def test_list_tree_happy_path(sessionmaker):
    world, vault = await _world(sessionmaker)
    vault.tree = Tree(
        folders=["Library", "Library/Philosophy"],
        notes=[TreeNote(path="Library/CCRU.md", title="CCRU")],
        truncated=False,
    )
    async with TestClient(TestServer(world.app)) as client:
        tokens = await _connect_and_open(world, client)
        payload = await _tool_payload(world, client, tokens["access_token"], "list_tree", {})
    assert payload["folders"] == ["Library", "Library/Philosophy"]
    assert payload["notes"] == [{"path": "Library/CCRU.md", "title": "CCRU"}]
    assert payload["truncated"] is False


async def test_list_tree_gated_by_the_write_switch(sessionmaker):
    world, vault = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        await world.command("/claude library on")
        result = await _call(world, client, tokens["access_token"], "list_tree", {})
    assert result == {"content": [{"type": "text", "text": mcp_core.WRITE_CLOSED_TEXT}], "isError": True}


async def test_grok_never_lists_or_accepts_list_tree(sessionmaker):
    world, _vault = await _world(sessionmaker, GROK_ACCESS_ENABLED=True)
    from app.core import grants

    async with sessionmaker() as session:
        token, _grant = await grants.create_grant(
            session, world.clock, scopes=["memory", "journal", "dialogs", "state"], ttl_hours=1
        )
    async with TestClient(TestServer(world.app)) as client:
        listed = await client.post(
            f"/mcp/{token}", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        names = {t["name"] for t in (await listed.json())["result"]["tools"]}
        assert "list_tree" not in names

        call = await client.post(
            f"/mcp/{token}",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_tree", "arguments": {}}},
        )
        body = await call.json()
    assert "error" in body
    assert body["error"]["code"] == -32602

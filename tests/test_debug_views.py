"""The `anchor_debug` role sees operational metadata, never conversation text.

Migration 9e4b2c7a1f05 gives Claude Code a way to debug production
without reading the dialogs: content-free views in the `debug` schema
and a role that can read them and nothing else. The role boundary is
the part that must hold even if every client-side guard is bypassed,
so it is asserted here in SQL.
"""

from __future__ import annotations

import pytest
import sqlalchemy
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.db.models import Base, Message, VaultChunk, VaultFile, VaultHold, VaultStatus

# Free-text columns: what a user wrote, what the model wrote, or what was
# derived from either. None may appear as a column of any debug view.
CONTENT_COLUMNS = {
    "telegram_update": {"payload"},
    "job": {"payload"},
    "scene": {"summary"},
    "message": {"content"},
    "user_state": {"due_action", "chat_id"},
    "state_change": {"old_value", "new_value"},
    "persona_version": {"body"},
    "memory": {"text"},
    "pending_memory": {"text"},
    "journal": {"text"},
    "proposal": {"value", "reason"},
    "checkin": {"note"},
    "outbound": {"tick_note"},
    "study_job": {"packet", "query"},
    "study_clip": {"text", "url", "title", "text_sha256"},
    "study_card": {"text", "quote", "source_url"},
    # 8a. A path is a file name the user chose, a note's title is
    # content, and a hash of a short fact confirms a guess at its text.
    "vault_file": {"path", "disk_sha256", "render_digest"},
    "vault_hold": {"payload"},
    "vault_chunk": {"heading", "text", "tsv"},
    # Timestamps only; forgets_window is a JSON array of them.
    "vault_status": set(),
}


async def _debug_columns(session) -> dict[str, set[str]]:
    rows = await session.execute(
        text(
            "select table_name, column_name from information_schema.columns "
            "where table_schema = 'debug'"
        )
    )
    columns: dict[str, set[str]] = {}
    for table, column in rows:
        columns.setdefault(table, set()).add(column)
    return columns


async def test_no_debug_view_exposes_a_content_column(sessionmaker):
    async with sessionmaker() as session:
        columns = await _debug_columns(session)
    assert columns, "debug schema has no views"
    for view, cols in columns.items():
        leaked = cols & CONTENT_COLUMNS.get(view, set())
        assert not leaked, f"debug.{view} exposes {leaked}"


def test_every_table_with_content_is_classified():
    # A new table must be classified here before it can get a view, so
    # the leak check above never silently skips one.
    text_columns = {
        table.name
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, (sqlalchemy.String, sqlalchemy.JSON))
        and column.name not in {"status", "kind", "role", "model", "error", "source"}
    }
    unclassified = text_columns - set(CONTENT_COLUMNS) - {
        "spend_ledger",
        "safety_event",
    }
    assert not unclassified, f"classify these tables in CONTENT_COLUMNS: {unclassified}"


async def test_debug_role_reads_views_but_not_tables(sessionmaker):
    async with sessionmaker() as session:
        session.add(Message(role="user", content="очень личное"))
        await session.commit()

    async with sessionmaker() as session:
        await session.execute(text("set local role anchor_debug"))
        row = (
            await session.execute(text("select role, content_len from debug.message"))
        ).one()
        assert row == ("user", len("очень личное"))

    for table in ("message", "telegram_update", "memory", "journal", "scene"):
        async with sessionmaker() as session:
            await session.execute(text("set local role anchor_debug"))
            with pytest.raises(ProgrammingError, match="permission denied"):
                await session.execute(text(f"select * from public.{table}"))


VAULT_VIEWS = ("vault_file", "vault_hold", "vault_chunk", "vault_status")


async def test_debug_role_reads_the_vault_views_granted_by_their_own_migration(sessionmaker):
    """9e4b2c7a1f05's GRANT ON ALL TABLES only covered the views that
    existed then; b8d24f6e0a17 must grant its own, or these fail."""
    async with sessionmaker() as session:
        hold = VaultHold(kind="mass_delete", payload={"file_ids": [1, 2, 3]})
        session.add(hold)
        await session.flush()
        note = VaultFile(path="Секретная заметка.md", role="note", disk_sha256="a" * 64)
        session.add(note)
        await session.flush()
        session.add(VaultChunk(file_id=note.id, ord=0, heading="Секрет", text="очень личное"))
        session.add(VaultStatus(id=1))
        await session.commit()

    for view in VAULT_VIEWS:
        async with sessionmaker() as session:
            await session.execute(text("set local role anchor_debug"))
            rows = (await session.execute(text(f"select * from debug.{view}"))).all()
            assert len(rows) == 1
            assert "Секрет" not in repr(rows)
            assert "a" * 64 not in repr(rows)

    async with sessionmaker() as session:
        await session.execute(text("set local role anchor_debug"))
        hold_row = (await session.execute(text("select file_count from debug.vault_hold"))).one()
        assert hold_row == (3,)
        seen = (await session.execute(text("select seen from debug.vault_file"))).one()
        assert seen == (True,)

    for table in VAULT_VIEWS:
        async with sessionmaker() as session:
            await session.execute(text("set local role anchor_debug"))
            with pytest.raises(ProgrammingError, match="permission denied"):
                await session.execute(text(f"select * from public.{table}"))

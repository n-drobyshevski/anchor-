"""The `anchor_debug` role sees operational metadata, never conversation text.

Migration 9e4b2c7a1f05 gives Claude Code a way to debug production
without reading the dialogs: content-free views in the `debug` schema
and a role that can read them and nothing else. The role boundary is
the part that must hold even if every client-side guard is bypassed,
so it is asserted here in SQL.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.db.models import Message

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
    "access_grant": {"token_sha256"},
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


async def test_every_debug_view_source_is_classified(sessionmaker):
    # A view may only exist over a table whose free-text columns are
    # listed above, so the leak check never silently skips one. Tables
    # with no view (web_session, planner_credential, ...) are simply
    # invisible to anchor_debug and need no entry.
    async with sessionmaker() as session:
        views = set(await _debug_columns(session))
    unclassified = views - set(CONTENT_COLUMNS) - {"spend_ledger", "safety_event"}
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

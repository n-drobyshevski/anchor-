"""027b3c0b323d: claude_changeset.folders and .moves (rev. 3).

Modelled on tests/test_claude_write_migration.py -- same scratch
database fixture, same "upgrade, poke it, downgrade, poke it again"
shape.
"""

from __future__ import annotations

import pytest

from tests.test_vault_notes_migration import _alembic, _run, scratch_database  # noqa: F401

BEFORE = "b1c4d8e29f6a"
AFTER = "027b3c0b323d"

CONNECTION = (
    "INSERT INTO oauth_connection (client_id, created_at, expires_at) "
    "VALUES ('c', now(), now() + interval '30 days') RETURNING id"
)


def _columns(url: str, schema: str, table: str) -> list[str]:
    rows = _run(
        url,
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_schema = '{schema}' AND table_name = '{table}' ORDER BY ordinal_position",
    )
    return [r["column_name"] for r in rows]


def test_folders_and_moves_default_to_zero(scratch_database):  # noqa: F811
    _alembic(scratch_database, "upgrade", AFTER)
    connection_id = _run(scratch_database, CONNECTION)[0]["id"]
    _run(
        scratch_database,
        "INSERT INTO claude_changeset (connection_id, vault_ref, kind, created_at, last_write_at) "
        f"VALUES ({connection_id}, 'v1', 'write', now(), now())",
    )
    rows = _run(scratch_database, "SELECT folders, moves FROM claude_changeset")
    assert [dict(r) for r in rows] == [{"folders": 0, "moves": 0}]

    _alembic(scratch_database, "downgrade", BEFORE)
    names = _columns(scratch_database, "public", "claude_changeset")
    assert "folders" not in names and "moves" not in names
    assert _run(scratch_database, "SELECT count(*) AS n FROM claude_changeset")[0]["n"] == 1
    _alembic(scratch_database, "upgrade", "head")


def test_check_constraints_reject_negative_values(scratch_database):  # noqa: F811
    _alembic(scratch_database, "upgrade", AFTER)
    connection_id = _run(scratch_database, CONNECTION)[0]["id"]

    with pytest.raises(Exception, match="ck_claude_changeset_folders"):
        _run(
            scratch_database,
            "INSERT INTO claude_changeset (connection_id, vault_ref, kind, folders, created_at, last_write_at) "
            f"VALUES ({connection_id}, 'v2', 'write', -1, now(), now())",
        )
    with pytest.raises(Exception, match="ck_claude_changeset_moves"):
        _run(
            scratch_database,
            "INSERT INTO claude_changeset (connection_id, vault_ref, kind, moves, created_at, last_write_at) "
            f"VALUES ({connection_id}, 'v3', 'write', -1, now(), now())",
        )
    _alembic(scratch_database, "downgrade", BEFORE)
    _alembic(scratch_database, "upgrade", "head")


def test_no_path_or_text_columns_added(scratch_database):  # noqa: F811
    _alembic(scratch_database, "upgrade", AFTER)
    names = set(_columns(scratch_database, "public", "claude_changeset"))
    assert {"folders", "moves"} <= names
    assert not any("path" in n or "text" in n for n in names)
    _alembic(scratch_database, "downgrade", BEFORE)
    _alembic(scratch_database, "upgrade", "head")

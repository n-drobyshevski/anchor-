"""b1c4d8e29f6a: the write switch and the changeset ledger.

Modelled exactly on tests/test_claude_library_migration.py -- same
scratch database fixture, same "upgrade, poke it, downgrade, poke it
again" shape.
"""

from __future__ import annotations

import pytest

from tests.test_vault_notes_migration import _alembic, _run, scratch_database  # noqa: F401

BEFORE = "aae4f596191d"
AFTER = "b1c4d8e29f6a"

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


def _tables(url: str) -> list[str]:
    rows = _run(url, "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
    return [r["table_name"] for r in rows]


def test_library_write_defaults_off_and_it_downgrades(scratch_database):  # noqa: F811
    _alembic(scratch_database, "upgrade", AFTER)
    _run(scratch_database, CONNECTION)
    rows = _run(scratch_database, "SELECT library_write FROM oauth_connection")
    assert [r["library_write"] for r in rows] == [False]
    assert _columns(scratch_database, "debug", "oauth_connection")[-1] == "library_write"

    _alembic(scratch_database, "downgrade", BEFORE)
    assert "library_write" not in _columns(scratch_database, "public", "oauth_connection")
    assert "library_write" not in _columns(scratch_database, "debug", "oauth_connection")
    assert _run(scratch_database, "SELECT count(*) AS n FROM oauth_connection")[0]["n"] == 1
    _alembic(scratch_database, "upgrade", "head")


def test_claude_changeset_created_with_check_and_cascade(scratch_database):  # noqa: F811
    _alembic(scratch_database, "upgrade", AFTER)
    connection_id = _run(scratch_database, CONNECTION)[0]["id"]
    _run(
        scratch_database,
        "INSERT INTO claude_changeset (connection_id, vault_ref, kind, created_at, last_write_at) "
        f"VALUES ({connection_id}, 'v1', 'write', now(), now())",
    )
    assert _run(scratch_database, "SELECT count(*) AS n FROM claude_changeset")[0]["n"] == 1

    with pytest.raises(Exception, match="ck_claude_changeset_kind"):
        _run(
            scratch_database,
            "INSERT INTO claude_changeset (connection_id, vault_ref, kind, created_at, last_write_at) "
            f"VALUES ({connection_id}, 'v2', 'bogus', now(), now())",
        )

    _run(scratch_database, f"DELETE FROM oauth_connection WHERE id = {connection_id}")
    assert _run(scratch_database, "SELECT count(*) AS n FROM claude_changeset")[0]["n"] == 0

    _alembic(scratch_database, "downgrade", BEFORE)
    assert "claude_changeset" not in _tables(scratch_database)
    _alembic(scratch_database, "upgrade", "head")


def test_claude_changeset_has_no_text_or_path_columns(scratch_database):  # noqa: F811
    _alembic(scratch_database, "upgrade", AFTER)
    names = set(_columns(scratch_database, "public", "claude_changeset"))
    assert names == {
        "id", "connection_id", "vault_ref", "kind", "files", "bytes",
        "refused", "created", "renamed", "created_at", "last_write_at", "undone_at",
    }
    assert not any("path" in n or "text" in n for n in names)
    _alembic(scratch_database, "downgrade", BEFORE)
    _alembic(scratch_database, "upgrade", "head")

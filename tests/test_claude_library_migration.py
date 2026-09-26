"""aae4f596191d: the library switch, the widened scope CHECK, the counter.

Modelled exactly on tests/test_access_grant_migration.py -- same scratch
database fixture (tests/test_vault_notes_migration.py), same "upgrade,
poke it, downgrade, poke it again" shape.
"""

from __future__ import annotations

import pytest

from tests.test_vault_notes_migration import _alembic, _run, scratch_database  # noqa: F401

BEFORE = "e6c1a9d3b527"
AFTER = "aae4f596191d"

CONNECTION = (
    "INSERT INTO oauth_connection (client_id, created_at, expires_at) "
    "VALUES ('c', now(), now() + interval '30 days')"
)


def _columns(url: str, schema: str, table: str) -> list[str]:
    rows = _run(
        url,
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_schema = '{schema}' AND table_name = '{table}' ORDER BY ordinal_position",
    )
    return [r["column_name"] for r in rows]


def test_library_read_defaults_off_and_it_downgrades(scratch_database):  # noqa: F811
    _alembic(scratch_database, "upgrade", AFTER)
    _run(scratch_database, CONNECTION)
    rows = _run(scratch_database, "SELECT library_read FROM oauth_connection")
    assert [r["library_read"] for r in rows] == [False]
    assert _columns(scratch_database, "debug", "oauth_connection")[-1] == "library_read"

    _alembic(scratch_database, "downgrade", BEFORE)
    assert "library_read" not in _columns(scratch_database, "public", "oauth_connection")
    assert "library_read" not in _columns(scratch_database, "debug", "oauth_connection")
    assert _run(scratch_database, "SELECT count(*) AS n FROM oauth_connection")[0]["n"] == 1
    _alembic(scratch_database, "upgrade", "head")


def test_scopes_check_accepts_notes_knowledge_refuses_notes_personal(scratch_database):  # noqa: F811
    _alembic(scratch_database, "upgrade", AFTER)
    _run(
        scratch_database,
        "INSERT INTO access_grant (client, token_sha256, scopes, expires_at) "
        "VALUES ('grok', 'a', array['notes_knowledge'], now() + interval '1 hour')",
    )
    assert _run(scratch_database, "SELECT count(*) AS n FROM access_grant")[0]["n"] == 1

    with pytest.raises(Exception, match="ck_access_grant_scopes"):
        _run(
            scratch_database,
            "INSERT INTO access_grant (client, token_sha256, scopes, expires_at) "
            "VALUES ('grok', 'b', array['notes_personal'], now() + interval '1 hour')",
        )

    # The row above carries the scope only the new CHECK allows: clear it
    # first, or the downgrade's ADD CONSTRAINT (against the old, narrower
    # array) fails on data the new constraint let in -- a genuine effect
    # of "the CHECK is stricter going backwards", not a migration bug.
    _run(scratch_database, "DELETE FROM access_grant")
    _alembic(scratch_database, "downgrade", BEFORE)
    with pytest.raises(Exception, match="ck_access_grant_scopes"):
        _run(
            scratch_database,
            "INSERT INTO access_grant (client, token_sha256, scopes, expires_at) "
            "VALUES ('grok', 'c', array['notes_knowledge'], now() + interval '1 hour')",
        )
    _alembic(scratch_database, "upgrade", "head")


def test_claude_library_read_table_created_and_dropped(scratch_database):  # noqa: F811
    _alembic(scratch_database, "upgrade", AFTER)
    _run(
        scratch_database,
        "INSERT INTO claude_library_read (local_date, count) VALUES ('2026-09-26', 3)",
    )
    rows = _run(scratch_database, "SELECT count FROM claude_library_read")
    assert [r["count"] for r in rows] == [3]

    with pytest.raises(Exception, match="ck_claude_library_read_count"):
        _run(
            scratch_database,
            "INSERT INTO claude_library_read (local_date, count) VALUES ('2026-09-27', -1)",
        )

    _alembic(scratch_database, "downgrade", BEFORE)
    tables = [
        r["table_name"]
        for r in _run(
            scratch_database,
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'",
        )
    ]
    assert "claude_library_read" not in tables
    _alembic(scratch_database, "upgrade", "head")

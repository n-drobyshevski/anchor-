"""5d2e8a1f0c47: claude_write_limit and vault_status.limits_push_pending.

Same scratch database fixture and "upgrade, poke it, downgrade, poke it
again" shape as tests/test_claude_write_folders_migration.py.
"""

from __future__ import annotations

import pytest

from tests.test_claude_write_folders_migration import _columns
from tests.test_vault_notes_migration import _alembic, _run, scratch_database  # noqa: F401

BEFORE = "027b3c0b323d"
AFTER = "5d2e8a1f0c47"


def test_table_and_flag_round_trip(scratch_database):  # noqa: F811
    _alembic(scratch_database, "upgrade", AFTER)
    _run(
        scratch_database,
        "INSERT INTO claude_write_limit (name, value, updated_at) VALUES ('creates_per_day', 5, now())",
    )
    _run(scratch_database, "INSERT INTO vault_status (id) VALUES (1)")
    rows = _run(scratch_database, "SELECT limits_push_pending FROM vault_status")
    assert [dict(r) for r in rows] == [{"limits_push_pending": False}]
    assert set(_columns(scratch_database, "public", "claude_write_limit")) == {"name", "value", "updated_at"}

    _alembic(scratch_database, "downgrade", BEFORE)
    assert _columns(scratch_database, "public", "claude_write_limit") == []
    assert "limits_push_pending" not in _columns(scratch_database, "public", "vault_status")
    _alembic(scratch_database, "upgrade", "head")


def test_check_constraint_rejects_a_negative_value(scratch_database):  # noqa: F811
    _alembic(scratch_database, "upgrade", AFTER)
    with pytest.raises(Exception, match="ck_claude_write_limit_value"):
        _run(
            scratch_database,
            "INSERT INTO claude_write_limit (name, value, updated_at) VALUES ('moves_per_day', -1, now())",
        )
    _alembic(scratch_database, "downgrade", BEFORE)
    _alembic(scratch_database, "upgrade", "head")

"""8b4f1d3a9e26: vault_status.claude_counters_reset_at.

Same "upgrade, poke it, downgrade, poke it again" shape as
tests/test_claude_write_limit_migration.py.
"""

from __future__ import annotations

from tests.test_claude_write_folders_migration import _columns
from tests.test_vault_notes_migration import _alembic, _run, scratch_database  # noqa: F401

BEFORE = "5d2e8a1f0c47"
AFTER = "8b4f1d3a9e26"


def test_column_round_trip(scratch_database):  # noqa: F811
    _alembic(scratch_database, "upgrade", AFTER)
    _run(scratch_database, "INSERT INTO vault_status (id) VALUES (1)")
    rows = _run(scratch_database, "SELECT claude_counters_reset_at FROM vault_status")
    assert [dict(r) for r in rows] == [{"claude_counters_reset_at": None}]

    _alembic(scratch_database, "downgrade", BEFORE)
    assert "claude_counters_reset_at" not in _columns(scratch_database, "public", "vault_status")
    _alembic(scratch_database, "upgrade", "head")

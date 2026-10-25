"""b4e7d2a9c613 adds `user_state.awaiting_at`, the /menu text prompt's clock.

The upgrade adds a nullable timestamp with no default: the singleton
row, and any row a deployment already holds, reads as "no step pending"
(NULL), which is also what app/core/menu_prompt.py treats as "not live".
The debug view is left alone (the view's `awaiting` column already says
whether a step is open; the timestamp of a button press is not worth
exposing). The downgrade drops the column and goes back to the revision
before. Runs on its own throwaway database, like the other migration
tests.
"""

from __future__ import annotations

from tests.test_vault_notes_migration import _alembic, _run, scratch_database  # noqa: F401

BEFORE = "a5d1c8e3f702"
AFTER = "b4e7d2a9c613"


def _columns(url: str, schema: str, table: str) -> dict[str, dict]:
    rows = _run(
        url,
        "SELECT column_name, data_type, is_nullable, column_default "
        f"FROM information_schema.columns WHERE table_schema = '{schema}' "
        f"AND table_name = '{table}' ORDER BY ordinal_position",
    )
    return {r["column_name"]: r for r in rows}


def test_upgrade_adds_a_nullable_timestamp_and_downgrade_removes_it(scratch_database):  # noqa: F811
    _alembic(scratch_database, "upgrade", BEFORE)
    assert "awaiting_at" not in _columns(scratch_database, "public", "user_state")
    # A live row from before the column existed, with a step pending.
    _run(
        scratch_database,
        "INSERT INTO user_state (id, chat_id, vault_epoch, awaiting, awaiting_ref) "
        "VALUES (1, 555, 'abcdef', 'checkin_note', 7)",
    )

    _alembic(scratch_database, "upgrade", AFTER)
    column = _columns(scratch_database, "public", "user_state")["awaiting_at"]
    assert column["data_type"] == "timestamp with time zone"
    assert column["is_nullable"] == "YES"
    assert column["column_default"] is None
    row = _run(scratch_database, "SELECT awaiting, awaiting_ref, awaiting_at FROM user_state")[0]
    assert (row["awaiting"], row["awaiting_ref"], row["awaiting_at"]) == ("checkin_note", 7, None)

    # It takes a timestamp, and NULL again.
    _run(scratch_database, "UPDATE user_state SET awaiting_at = now() WHERE id = 1")
    assert _run(scratch_database, "SELECT awaiting_at FROM user_state")[0]["awaiting_at"] is not None
    _run(scratch_database, "UPDATE user_state SET awaiting_at = NULL WHERE id = 1")

    # The debug view does not expose it.
    assert "awaiting_at" not in _columns(scratch_database, "debug", "user_state")

    _alembic(scratch_database, "downgrade", BEFORE)
    assert "awaiting_at" not in _columns(scratch_database, "public", "user_state")
    row = _run(scratch_database, "SELECT awaiting, awaiting_ref FROM user_state")[0]
    assert (row["awaiting"], row["awaiting_ref"]) == ("checkin_note", 7)
    _alembic(scratch_database, "upgrade", "head")
    assert "awaiting_at" in _columns(scratch_database, "public", "user_state")

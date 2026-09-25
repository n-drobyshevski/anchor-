"""d4a7b9e2c1f3 adds `access_grant.client` without touching a grant.

Existing grants are Grok's, so the upgrade fills `client` with `grok`
and the capability URLs already handed out keep working; the downgrade
drops the column and the view goes back to its old columns. Runs on its
own throwaway database, like the 8e migration tests.
"""

from __future__ import annotations

from tests.test_vault_notes_migration import _alembic, _run, scratch_database  # noqa: F401

BEFORE = "c3e8f5a1d2b6"
AFTER = "d4a7b9e2c1f3"
GRANT = (
    "INSERT INTO access_grant (token_sha256, scopes, expires_at) "
    "VALUES ('abc', array['memory'], now() + interval '1 hour')"
)


def _columns(url: str, schema: str) -> list[str]:
    rows = _run(
        url,
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_schema = '{schema}' AND table_name = 'access_grant' ORDER BY ordinal_position",
    )
    return [r["column_name"] for r in rows]


def test_existing_grants_become_grok_s_and_it_downgrades(scratch_database):  # noqa: F811
    _alembic(scratch_database, "upgrade", BEFORE)
    _run(scratch_database, GRANT)
    _alembic(scratch_database, "upgrade", AFTER)
    rows = _run(scratch_database, "SELECT client, token_sha256 FROM access_grant")
    assert [(r["client"], r["token_sha256"]) for r in rows] == [("grok", "abc")]
    assert _columns(scratch_database, "debug")[-1] == "client"
    assert "token_sha256" not in _columns(scratch_database, "debug")

    _alembic(scratch_database, "downgrade", BEFORE)
    assert "client" not in _columns(scratch_database, "public")
    assert "client" not in _columns(scratch_database, "debug")
    assert _run(scratch_database, "SELECT count(*) AS n FROM access_grant")[0]["n"] == 1
    _alembic(scratch_database, "upgrade", "head")

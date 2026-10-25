"""d8a3f6c1b925: llm_model_override and its debug view.

Same scratch database fixture and "upgrade, poke it, downgrade, poke it
again" shape as tests/test_claude_write_limit_migration.py.
"""

from __future__ import annotations

import pytest

from tests.test_claude_write_folders_migration import _columns
from tests.test_vault_notes_migration import _alembic, _run, scratch_database  # noqa: F401

BEFORE = "b4e7d2a9c613"
AFTER = "d8a3f6c1b925"

INSERT = "INSERT INTO llm_model_override (role, model{extra}) VALUES ('{role}', '{model}'{values})"


def _insert(url: str, role: str, model: str, **prices) -> None:
    extra = "".join(f", {k}" for k in prices)
    values = "".join(f", {v}" for v in prices.values())
    _run(url, INSERT.format(role=role, model=model, extra=extra, values=values))


def test_table_and_view_round_trip(scratch_database):  # noqa: F811
    _alembic(scratch_database, "upgrade", AFTER)
    _insert(scratch_database, "persona", "openai/gpt-4.1-nano", price_in=0.1, price_out=0.4)
    assert set(_columns(scratch_database, "public", "llm_model_override")) == {
        "role", "model", "price_in", "price_cached", "price_out", "updated_at",
    }
    # The debug view shows the role, the model and when -- not the prices.
    assert _columns(scratch_database, "debug", "llm_model_override") == ["role", "model", "updated_at"]
    rows = _run(scratch_database, "SELECT role, model FROM debug.llm_model_override")
    assert [tuple(r) for r in rows] == [("persona", "openai/gpt-4.1-nano")]

    _alembic(scratch_database, "downgrade", BEFORE)
    assert _columns(scratch_database, "public", "llm_model_override") == []
    assert _columns(scratch_database, "debug", "llm_model_override") == []
    _alembic(scratch_database, "upgrade", "head")
    assert _columns(scratch_database, "public", "llm_model_override") != []


def test_checks_hold(scratch_database):  # noqa: F811
    _alembic(scratch_database, "upgrade", AFTER)
    # Only the welfare shadow may be switched off with an empty model.
    _insert(scratch_database, "welfare_shadow", "")
    with pytest.raises(Exception, match="ck_llm_model_override_model"):
        _insert(scratch_database, "persona", "")
    with pytest.raises(Exception, match="ck_llm_model_override_role"):
        _insert(scratch_database, "nonsense", "a/b")
    with pytest.raises(Exception, match="ck_llm_model_override_price"):
        _insert(scratch_database, "cheap", "a/b", price_in=-1)


def _spec(url: str) -> dict[str, dict]:
    rows = _run(
        url,
        "SELECT column_name, data_type, is_nullable, column_default, numeric_precision, numeric_scale "
        "FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = 'llm_model_override' ORDER BY ordinal_position",
    )
    return {r["column_name"]: r for r in rows}


def test_columns_keys_and_defaults(scratch_database):  # noqa: F811
    """The table's shape, as app/llm/models.py reads and writes it: one
    row per role (the primary key), prices that may all be missing, and
    a clock that fills itself in."""
    _alembic(scratch_database, "upgrade", AFTER)
    spec = _spec(scratch_database)
    assert [name for name in spec] == [
        "role", "model", "price_in", "price_cached", "price_out", "updated_at"
    ]
    for name in ("role", "model", "updated_at"):
        assert spec[name]["is_nullable"] == "NO", name
    for name in ("price_in", "price_cached", "price_out"):
        column = spec[name]
        assert column["is_nullable"] == "YES", name
        assert column["data_type"] == "numeric"
        assert (column["numeric_precision"], column["numeric_scale"]) == (12, 6)
        assert column["column_default"] is None
    assert spec["updated_at"]["data_type"] == "timestamp with time zone"
    assert "now()" in spec["updated_at"]["column_default"]

    _insert(scratch_database, "cheap", "a/b")
    row = _run(
        scratch_database,
        "SELECT price_in, price_cached, price_out, updated_at IS NOT NULL AS stamped "
        "FROM llm_model_override",
    )[0]
    assert (row["price_in"], row["price_cached"], row["price_out"], row["stamped"]) == (
        None, None, None, True
    )
    # One row per role: a second pick is an upsert, never a second row.
    with pytest.raises(Exception, match="llm_model_override_pkey"):
        _insert(scratch_database, "cheap", "c/d")


def test_prices_keep_six_places_and_zero_is_allowed(scratch_database):  # noqa: F811
    _alembic(scratch_database, "upgrade", AFTER)
    _insert(scratch_database, "persona", "a/b", price_in=0.075, price_cached=0, price_out=0.3)
    row = _run(scratch_database, "SELECT price_in, price_cached, price_out FROM llm_model_override")[0]
    assert [str(row[k]) for k in ("price_in", "price_cached", "price_out")] == [
        "0.075000", "0.000000", "0.300000"
    ]
    # Each price is checked on its own.
    for column in ("price_cached", "price_out"):
        with pytest.raises(Exception, match="ck_llm_model_override_price"):
            _insert(scratch_database, "judge", "a/b", **{column: -0.5})


def test_the_debug_role_reads_the_view_and_the_view_has_no_prices(scratch_database):  # noqa: F811
    """`anchor_debug` is cluster-wide, so it exists only where the test
    cluster was set up with it; the grant is conditional in the migration
    for the same reason."""
    roles = _run(scratch_database, "SELECT 1 AS present FROM pg_roles WHERE rolname = 'anchor_debug'")
    if not roles:
        pytest.skip("no anchor_debug role in this cluster")
    _alembic(scratch_database, "upgrade", AFTER)
    allowed = _run(
        scratch_database,
        "SELECT has_table_privilege('anchor_debug', 'debug.llm_model_override', 'SELECT') AS ok",
    )[0]["ok"]
    assert allowed is True
    # ... and not the base table, whose prices the view leaves out.
    raw = _run(
        scratch_database,
        "SELECT has_table_privilege('anchor_debug', 'public.llm_model_override', 'SELECT') AS ok",
    )[0]["ok"]
    assert raw is False


def test_downgrade_forgets_the_picks_and_upgrade_starts_empty(scratch_database):  # noqa: F811
    """Rolling back puts every role back on its env model: the rows go
    with the table, and a later upgrade does not bring them back."""
    _alembic(scratch_database, "upgrade", AFTER)
    _insert(scratch_database, "persona", "a/b", price_in=1, price_out=2)
    _insert(scratch_database, "welfare_shadow", "")
    _alembic(scratch_database, "downgrade", BEFORE)
    _alembic(scratch_database, "upgrade", AFTER)
    assert _run(scratch_database, "SELECT count(*) AS n FROM llm_model_override")[0]["n"] == 0


def test_the_migration_names_the_roles_the_app_does():
    """The CHECK is written out in the migration, apart from the ORM's
    (tests/test_model_resolver.py ties that one to `ROLES`): a role
    added to one and not the other could be chosen in the menu and then
    refused by the database at the first «Да»."""
    import importlib.util
    import pathlib

    from app.llm.models import ROLES

    path = next(pathlib.Path("migrations/versions").glob("d8a3f6c1b925_*.py"))
    spec = importlib.util.spec_from_file_location("llm_model_override_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    named = {part.strip(" '") for part in module.ROLES.split(",")}
    assert named == set(ROLES)


def test_it_is_on_the_one_line_of_history():
    """One head, and this revision is on the way to it: a second head
    would make `alembic upgrade head` refuse on the next deploy."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    (head,) = script.get_heads()
    assert AFTER in {rev.revision for rev in script.walk_revisions(base="base", head=head)}
    assert script.get_revision(AFTER).down_revision == BEFORE

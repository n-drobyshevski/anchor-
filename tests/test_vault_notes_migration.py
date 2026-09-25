"""c3e8f5a1d2b6 refuses to guess a note's class (8e plan section 5).

8a created `vault_chunk` empty and nothing on `main` writes it, or a
`role='note'` file row, before 8d. If either has rows anyway, the
migration stops and names the cause rather than filing them under a
class. Each test runs on its own throwaway database, because it moves
the schema up and down; the session database stays at head.
"""

from __future__ import annotations

import asyncio
import os
import random
import string
import subprocess

import asyncpg
import pytest

from tests.conftest import REPO_ROOT, _admin_dsn, _admin_host_port, _find_pg_isready

BEFORE_8E = "b8d24f6e0a17"


def _alembic(database_url: str, action: str, revision: str) -> None:
    from alembic import command
    from alembic.config import Config

    prior = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        getattr(command, action)(cfg, revision)
    finally:
        if prior is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prior


@pytest.fixture
def scratch_database():
    pg_isready = _find_pg_isready()
    host, port = _admin_host_port()
    if pg_isready is None or subprocess.run(
        [pg_isready, "-h", host, "-p", str(port)], capture_output=True, check=False
    ).returncode:
        pytest.skip("PostgreSQL is required for migration tests")
    name = "anchor_mig_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    admin = _admin_dsn()
    url = f"{admin.rsplit('/', 1)[0]}/{name}"

    async def admin_exec(sql: str) -> None:
        conn = await asyncpg.connect(admin)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(admin_exec(f"CREATE DATABASE \"{name}\" TEMPLATE template0 LOCALE 'C.UTF-8' ENCODING 'UTF8'"))
    try:
        yield url
    finally:
        asyncio.run(
            admin_exec(
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{name}' "
                "AND pid <> pg_backend_pid()"
            )
        )
        asyncio.run(admin_exec(f'DROP DATABASE IF EXISTS "{name}"'))


def _run(url: str, *statements: str):
    async def go():
        conn = await asyncpg.connect(url)
        try:
            out = None
            for sql in statements:
                out = await conn.fetch(sql)
            return out
        finally:
            await conn.close()

    return asyncio.run(go())


def test_it_refuses_over_existing_chunks(scratch_database):
    _alembic(scratch_database, "upgrade", BEFORE_8E)
    _run(
        scratch_database,
        "INSERT INTO vault_file (path, role) VALUES ('Бег.md', 'note')",
        "INSERT INTO vault_chunk (file_id, ord, text) SELECT id, 0, 'x' FROM vault_file",
    )
    with pytest.raises(Exception, match="vault_chunk has rows"):
        _alembic(scratch_database, "upgrade", "head")
    # Nothing was changed: the old table is still there with its row.
    assert _run(scratch_database, "SELECT count(*) AS n FROM vault_chunk")[0]["n"] == 1


def test_it_refuses_over_note_file_rows(scratch_database):
    _alembic(scratch_database, "upgrade", BEFORE_8E)
    _run(scratch_database, "INSERT INTO vault_file (path, role) VALUES ('Бег.md', 'note')")
    with pytest.raises(Exception, match="role='note' rows"):
        _alembic(scratch_database, "upgrade", "head")


def test_it_upgrades_an_empty_vault_and_downgrades_cleanly(scratch_database):
    _alembic(scratch_database, "upgrade", BEFORE_8E)
    _run(scratch_database, "INSERT INTO vault_file (path, role) VALUES ('Anchor/Memory/0001-abcdef.md', 'fact')")
    _alembic(scratch_database, "upgrade", "head")
    tables = {
        r["table_name"]
        for r in _run(
            scratch_database,
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'",
        )
    }
    assert {"note_chunk_personal", "note_chunk_knowledge"} <= tables
    assert "vault_chunk" not in tables
    consent = _run(scratch_database, "SELECT notes_consent FROM user_state")
    assert all(row["notes_consent"] is False for row in consent)
    _alembic(scratch_database, "downgrade", BEFORE_8E)
    assert _run(scratch_database, "SELECT count(*) AS n FROM vault_chunk")[0]["n"] == 0
    _alembic(scratch_database, "upgrade", "head")

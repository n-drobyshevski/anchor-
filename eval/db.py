"""A throwaway database for one eval run (phase-3 plan section 9).

Mirrors tests/conftest.py's approach rather than importing it: `tests/`
is not an importable package and an eval that depended on the test
suite's fixtures would break the moment either moved. The duplication
is ~40 lines and buys independence.

Created with TEMPLATE template0 and LOCALE 'C.UTF-8' for the same
reason conftest.py pins it: under a plain `C` locale pg_trgm stops
seeing Cyrillic, silently. Nothing in the eval retrieves memories
today, but the database the harness measures against should be the
database production runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import string
import urllib.parse

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

REPO_ADMIN_URL = os.environ.get(
    "ANCHOR_ADMIN_DATABASE_URL", "postgresql://anchor:anchor@127.0.0.1:5432/postgres"
)


def _async_url(raw: str) -> str:
    if raw.startswith("postgresql://"):
        return "postgresql+asyncpg://" + raw[len("postgresql://") :]
    return raw


def _with_database(raw: str, name: str) -> str:
    parsed = urllib.parse.urlsplit(raw)
    return urllib.parse.urlunsplit(parsed._replace(path=f"/{name}"))


def _upgrade(database_url: str) -> None:
    """`alembic upgrade head` against a throwaway database.

    migrations/env.py reads DATABASE_URL from app.config rather than
    from alembic.ini, so the URL is passed through the environment for
    the duration of this call -- exactly as tests/conftest.py does.

    It is also run in a worker thread by the caller: env.py calls
    asyncio.run(), which raises inside an already-running loop.
    """
    import pathlib

    from alembic import command
    from alembic.config import Config

    root = pathlib.Path(__file__).resolve().parent.parent
    prior = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        cfg = Config(str(root / "alembic.ini"))
        cfg.set_main_option("script_location", str(root / "migrations"))
        command.upgrade(cfg, "head")
    finally:
        if prior is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prior


@contextlib.asynccontextmanager
async def throwaway_sessionmaker(admin_url: str | None = None):
    """Yield a sessionmaker on a fresh, migrated database; drop it after.

    `admin_url` (5d) lets a caller point the admin connection somewhere
    other than `REPO_ADMIN_URL` -- app/core/amendments.py's
    `amendment_trial` job uses it to reach `ANCHOR_ADMIN_DATABASE_URL`
    (or `DATABASE_URL` with its database name swapped for `postgres`)
    rather than whatever a *manual* `eval.run.py` invocation happens to
    have in its environment. Defaulting to None preserves this
    function's exact previous behaviour for every existing caller.
    """
    suffix = "".join(random.choices(string.ascii_lowercase, k=8))
    name = f"anchor_eval_{suffix}"

    admin_engine = create_async_engine(
        _async_url(admin_url or REPO_ADMIN_URL), isolation_level="AUTOCOMMIT"
    )
    from sqlalchemy import text as sql_text

    async with admin_engine.connect() as conn:
        await conn.execute(
            sql_text(f"CREATE DATABASE {name} TEMPLATE template0 LOCALE 'C.UTF-8'")
        )

    raw_url = _with_database(admin_url or REPO_ADMIN_URL, name)
    await asyncio.to_thread(_upgrade, raw_url)

    engine = create_async_engine(_async_url(raw_url))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin_engine.connect() as conn:
            await conn.execute(sql_text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        await admin_engine.dispose()

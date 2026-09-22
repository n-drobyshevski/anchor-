"""Shared test fixtures.

The DB fixture reuses an already-running Postgres cluster (checked via
pg_isready). It creates a throwaway `anchor_test_<rand>` database for
the whole test session, runs `alembic upgrade head` programmatically
against it, and drops it at teardown. TEST_DATABASE_URL overrides the
whole thing when set (that database's lifecycle is then not ours to
manage). When no usable cluster is found, DB-dependent tests are
skipped rather than erroring.

**Postgres 18, and the locale.** Production runs Postgres 18
(Railway's postgres-ssl:18), so the fixture targets 18 and warns when
it finds an older major version -- Phase 1 was written against 16 and
still passes on both, but 2b's pg_trgm work should be exercised on the
version that actually serves it. See scripts/setup-postgres.sh.

The locale matters far more than the major version, and is pinned
explicitly: the test database is created with TEMPLATE template0 and
LOCALE 'C.UTF-8' rather than inheriting whatever template1 happens to
carry. Under a plain `C` locale pg_trgm silently stops seeing Cyrillic
-- show_trgm('привет мир') returns zero trigrams and every similarity()
is 0, with no error anywhere -- which would make 2b's memory retrieval
quietly return nothing. Pinning the locale here means the test suite
can never accidentally pass under a locale production does not use.

Cleanup between tests is TRUNCATE, not transaction rollback: the SKIP
LOCKED queue test needs two connections that both see committed rows,
which a rolled-back-per-test transaction would hide from each other.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import random
import shutil
import string
import subprocess
import urllib.parse
import warnings
from pathlib import Path
from typing import AsyncGenerator

import asyncpg
import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    SendChatAction,
    SendDocument,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import Message as TgMessage
from sqlalchemy import text

from app.core.clock import FrozenClock, SystemClock, combine_local
from app.db.models import Base
from app.db.session import create_engine_and_sessionmaker
from app.llm.provider import LLMResponse, LLMUsage

REPO_ROOT = Path(__file__).resolve().parent.parent


# Newest first: the version production runs comes first, and the search
# falls back rather than failing outright on a machine that only has an
# older cluster.
PREFERRED_PG_MAJORS = (18, 17, 16)
PRODUCTION_PG_MAJOR = 18


def _find_pg_isready() -> str | None:
    for major in PREFERRED_PG_MAJORS:
        candidate = f"/usr/lib/postgresql/{major}/bin/pg_isready"
        if os.path.exists(candidate):
            return candidate
    return shutil.which("pg_isready")


def _admin_dsn() -> str:
    """DSN used only to create/drop the throwaway test database."""
    return os.environ.get(
        "ANCHOR_ADMIN_DATABASE_URL", "postgresql://anchor:anchor@127.0.0.1:5432/postgres"
    )


def _admin_host_port() -> tuple[str, int]:
    """Host and port from the admin DSN, for the pg_isready probe.

    Parsed rather than hardcoded to 127.0.0.1:5432 so that pointing
    ANCHOR_ADMIN_DATABASE_URL at a second cluster (a Postgres 18 one on
    5433, say) probes *that* cluster rather than reporting a different
    one as ready.
    """
    parsed = urllib.parse.urlparse(_admin_dsn())
    return parsed.hostname or "127.0.0.1", parsed.port or 5432


def _run_alembic_upgrade(database_url: str) -> None:
    """Run `alembic upgrade head` programmatically against `database_url`.

    migrations/env.py reads DATABASE_URL from app.config, so we point it
    at the throwaway test database via the environment for the duration
    of this call.
    """
    from alembic import command
    from alembic.config import Config

    prior = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        command.upgrade(cfg, "head")
    finally:
        if prior is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prior


@pytest.fixture(scope="session")
def test_database_url() -> str:
    override = os.environ.get("TEST_DATABASE_URL")
    if override:
        # Normalize to the asyncpg driver exactly as app.config does. A bare
        # postgresql:// URL makes SQLAlchemy reach for psycopg2, which is not
        # a dependency of this project.
        raw_url = override.replace("postgresql+asyncpg://", "postgresql://", 1)
        asyncpg_url = raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        # The caller owns this database's lifecycle, but it still needs the
        # schema; `alembic upgrade head` is idempotent, so re-running is safe.
        _run_alembic_upgrade(raw_url)
        yield asyncpg_url
        return

    pg_isready = _find_pg_isready()
    if pg_isready is None:
        pytest.skip("pg_isready not found; PostgreSQL is required for DB tests")

    host, port = _admin_host_port()
    ready = subprocess.run(
        [pg_isready, "-h", host, "-p", str(port)], capture_output=True, check=False
    )
    if ready.returncode != 0:
        pytest.skip(f"PostgreSQL is not accepting connections on {host}:{port}")

    db_name = "anchor_test_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    admin_dsn = _admin_dsn()
    base = admin_dsn.rsplit("/", 1)[0]
    raw_url = f"{base}/{db_name}"
    asyncpg_url = raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    async def _create() -> None:
        conn = await asyncpg.connect(admin_dsn)
        try:
            server_major = int(conn.get_server_version().major)
            if server_major < PRODUCTION_PG_MAJOR:
                warnings.warn(
                    f"tests are running on PostgreSQL {server_major}; production runs "
                    f"{PRODUCTION_PG_MAJOR}. Run scripts/setup-postgres.sh to match it.",
                    stacklevel=1,
                )
            # LOCALE/TEMPLATE pinned on purpose -- see the module docstring.
            await conn.execute(
                f'CREATE DATABASE "{db_name}" '
                "TEMPLATE template0 LOCALE 'C.UTF-8' ENCODING 'UTF8'"
            )
        finally:
            await conn.close()

    async def _drop() -> None:
        conn = await asyncpg.connect(admin_dsn)
        try:
            await conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid()"
            )
            await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        finally:
            await conn.close()

    asyncio.run(_create())
    try:
        _run_alembic_upgrade(raw_url)
        yield asyncpg_url
    finally:
        asyncio.run(_drop())


@pytest_asyncio.fixture()
async def sessionmaker(test_database_url: str):
    """A fresh engine/sessionmaker per test, truncated at teardown.

    A fresh engine per test avoids binding asyncpg connections to an
    event loop other than the current test's (pytest-asyncio's default
    fixture loop scope here is "function").
    """
    engine, maker = create_engine_and_sessionmaker(test_database_url)
    try:
        yield maker
    finally:
        async with maker() as session:
            # Derived from the metadata rather than hand-listed. The
            # hand-written list had silently gone stale twice: `outbound`
            # (3a) survived only because it FKs `message` and got caught
            # by CASCADE, and `safety_event` (H2) has no FK at all, so
            # its rows leaked between tests in the same file and made
            # assertions pass or fail depending on test order.
            tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
            await session.execute(
                text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE")
            )
            await session.commit()
        await engine.dispose()


class FakeSession(BaseSession):
    """Captures outgoing methods instead of making real HTTP requests.

    Lifted out of test_worker.py/test_messages.py (1a/1b), where it was
    duplicated verbatim, and extended with a SendChatAction branch: 1c's
    turn.run() pings sendChatAction every 4s while waiting on the LLM
    (app/tg/send.py), and without this branch every turn test would hit
    make_request's NotImplementedError the instant typing starts.

    2b adds the inline-keyboard methods (plan section 11). `edits`
    records every editMessageText so tests can assert that paging edits
    one message rather than sending a new one per tap, and `answered`
    records every answerCallbackQuery -- a button that is never answered
    spins in the real client until Telegram times it out, so "did we
    answer?" is a property worth asserting rather than assuming.
    """

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[SendMessage] = []
        self.chat_actions: list[SendChatAction] = []
        self.edits: list[EditMessageText] = []
        self.answered: list[AnswerCallbackQuery] = []
        self.documents: list[SendDocument] = []
        self._next_message_id = 1

    async def close(self) -> None:
        pass

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):
        if isinstance(method, SendMessage):
            self.sent.append(method)
            message_id = self._next_message_id
            self._next_message_id += 1
            return TgMessage.model_validate(
                {
                    "message_id": message_id,
                    "date": 0,
                    "chat": {"id": method.chat_id, "type": "private"},
                    "text": method.text,
                },
                context={"bot": bot},
            )
        if isinstance(method, SendChatAction):
            self.chat_actions.append(method)
            return True
        if isinstance(method, AnswerCallbackQuery):
            self.answered.append(method)
            return True
        if isinstance(method, EditMessageText):
            self.edits.append(method)
            return TgMessage.model_validate(
                {
                    "message_id": method.message_id,
                    "date": 0,
                    "chat": {"id": method.chat_id, "type": "private"},
                    "text": method.text,
                },
                context={"bot": bot},
            )
        if isinstance(method, SendDocument):
            # SendDocument.__returning__ is Message, so this must hand
            # back a TgMessage like the SendMessage branch, not True.
            # `method.document` is the BufferedInputFile itself, so a
            # test reads .filename and parses .data with no HTTP layer
            # and nothing uploaded.
            self.documents.append(method)
            message_id = self._next_message_id
            self._next_message_id += 1
            return TgMessage.model_validate(
                {
                    "message_id": message_id,
                    "date": 0,
                    "chat": {"id": method.chat_id, "type": "private"},
                },
                context={"bot": bot},
            )
        if isinstance(method, EditMessageReplyMarkup):
            return True
        raise NotImplementedError(f"FakeSession cannot handle {method!r}")

    async def stream_content(
        self,
        url: str,
        headers: dict | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        raise NotImplementedError
        yield b""  # pragma: no cover


def make_bot(token: str = "123456:TESTTOKEN") -> tuple[Bot, FakeSession]:
    """A Bot wired to a fresh FakeSession, for tests that don't need the
    session object directly (most do, to assert on .sent)."""
    fake_session = FakeSession()
    return Bot(token=token, session=fake_session), fake_session


class FakeLLMProvider:
    """A canned `LLMProvider` -- every test uses this, never the network.

    `raises` is a list of exceptions consumed one per call, front first;
    once exhausted (or if empty), `complete()` returns the canned
    LLMResponse. This is what lets test_turn.py drive turn.py's retry
    loop deterministically: e.g. `raises=[LLMRetryableError(), LLMRetryableError()]`
    fails twice then succeeds on the third call, and a longer list (or
    a non-retryable LLMError) exercises the final-failure path.
    """

    def __init__(
        self,
        text: str = "Тестовый ответ Anchor.",
        usage: LLMUsage | None = None,
        model: str = "cydonia-fake",
        raises: list[Exception] | None = None,
    ) -> None:
        self.calls = 0
        self.text = text
        self.usage = usage or LLMUsage(
            input_tokens=100, cached_tokens=20, output_tokens=50, cost_usd=None
        )
        self.model = model
        self._raises = list(raises) if raises else []
        self.closed = False
        self.received_messages: list[list] = []
        self.received_conversation_ids: list[str] = []
        self.received_schemas: list = []

    async def complete(
        self,
        messages,
        *,
        conversation_id: str,
        json_schema=None,
    ) -> LLMResponse:
        self.calls += 1
        self.received_messages.append(messages)
        self.received_conversation_ids.append(conversation_id)
        self.received_schemas.append(json_schema)
        if self._raises:
            raise self._raises.pop(0)
        return LLMResponse(text=self.text, usage=self.usage, model=self.model)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture()
def fake_llm_provider() -> FakeLLMProvider:
    return FakeLLMProvider()


# --- 3a: the clock (phase-3 plan section 3) ---------------------------
#
# Every time-dependent function under app/core/ now takes a Clock. Most
# tests do not care what time it is and just need *a* clock, so `clock`
# hands them the real one. Tests that do care build a FrozenClock at the
# instant they mean -- `frozen_clock` is the factory for that, and the
# Paris DST dates live in tests/test_clock.py.


@pytest.fixture()
def clock() -> SystemClock:
    """The real clock, for tests whose behaviour does not depend on time."""
    return SystemClock()


@pytest.fixture()
def frozen_clock():
    """Factory: `frozen_clock(2026, 10, 25, 9, 0, tz="Europe/Paris")`.

    Takes a *local* wall-clock reading in `tz` and returns a FrozenClock
    pinned to the matching instant, because every Phase 3 rule is stated
    in local time ("09:00", "quiet from 22:30") and converting by hand in
    each test is where the DST bugs would hide.
    """

    def _make(
        year: int,
        month: int,
        day: int,
        hour: int = 0,
        minute: int = 0,
        second: int = 0,
        *,
        tz: str = "Europe/Paris",
    ) -> FrozenClock:
        return FrozenClock(
            combine_local(
                datetime.date(year, month, day),
                datetime.time(hour, minute, second),
                tz,
            )
        )

    return _make

"""Shared test fixtures.

The DB fixture reuses the already-running Postgres 16 cluster (checked
via pg_isready). It creates a throwaway `anchor_test_<rand>` database
for the whole test session, runs `alembic upgrade head` programmatically
against it, and drops it at teardown. TEST_DATABASE_URL overrides the
whole thing when set (that database's lifecycle is then not ours to
manage). When PG16 binaries are absent, DB-dependent tests are skipped
rather than erroring.

Cleanup between tests is TRUNCATE, not transaction rollback: the SKIP
LOCKED queue test needs two connections that both see committed rows,
which a rolled-back-per-test transaction would hide from each other.
"""

from __future__ import annotations

import asyncio
import os
import random
import shutil
import string
import subprocess
from pathlib import Path
from typing import AsyncGenerator

import asyncpg
import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendChatAction, SendMessage, TelegramMethod
from aiogram.types import Message as TgMessage
from sqlalchemy import text

from app.db.session import create_engine_and_sessionmaker
from app.llm.provider import LLMResponse, LLMUsage

REPO_ROOT = Path(__file__).resolve().parent.parent


def _find_pg_isready() -> str | None:
    found = shutil.which("pg_isready")
    if found:
        return found
    for candidate in (
        "/usr/lib/postgresql/16/bin/pg_isready",
        "/usr/lib/postgresql/*/bin/pg_isready",
    ):
        if os.path.exists(candidate):
            return candidate
    return None


def _admin_dsn() -> str:
    """DSN used only to create/drop the throwaway test database."""
    return os.environ.get(
        "ANCHOR_ADMIN_DATABASE_URL", "postgresql://anchor:anchor@127.0.0.1:5432/postgres"
    )


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
        pytest.skip("pg_isready not found; PostgreSQL 16 binaries are required for DB tests")

    ready = subprocess.run(
        [pg_isready, "-h", "127.0.0.1", "-p", "5432"], capture_output=True, check=False
    )
    if ready.returncode != 0:
        pytest.skip("PostgreSQL is not accepting connections on 127.0.0.1:5432")

    db_name = "anchor_test_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    admin_dsn = _admin_dsn()
    base = admin_dsn.rsplit("/", 1)[0]
    raw_url = f"{base}/{db_name}"
    asyncpg_url = raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    async def _create() -> None:
        conn = await asyncpg.connect(admin_dsn)
        try:
            await conn.execute(f'CREATE DATABASE "{db_name}"')
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
            await session.execute(
                text(
                    "TRUNCATE TABLE telegram_update, message, user_state, "
                    "state_change, persona_version, spend_ledger "
                    "RESTART IDENTITY CASCADE"
                )
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
    """

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[SendMessage] = []
        self.chat_actions: list[SendChatAction] = []
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
        model: str = "grok-4.7-fake",
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

    async def complete(self, messages, *, conversation_id: str) -> LLMResponse:
        self.calls += 1
        self.received_messages.append(messages)
        self.received_conversation_ids.append(conversation_id)
        if self._raises:
            raise self._raises.pop(0)
        return LLMResponse(text=self.text, usage=self.usage, model=self.model)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture()
def fake_llm_provider() -> FakeLLMProvider:
    return FakeLLMProvider()

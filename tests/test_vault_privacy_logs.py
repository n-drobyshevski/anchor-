"""Logs carry no path, name or content from the vault (phase-8 plan section 13).

Runs one `sync` pass that ingests a new fact, quarantines a file, opens
a rule hold, and forgets a fact whose file vanished -- then asserts
each of those flows produced at least one log record, and that no
record's message or `extra` contains any path, file name, fact text or
property value used in the test.
"""

from __future__ import annotations

import datetime
import logging

import pytest

from app.config import Settings
from app.core import memory
from app.core.clock import FrozenClock
from app.db.models import UserState
from app.vault import limits
from app.vault.sync import run_vault_sync
from vault_fake import FakeVault

EPOCH = "abcdef"
TOKEN = "vault-token-" + "v" * 32
NOW = datetime.datetime(2026, 9, 25, 10, 0, tzinfo=datetime.timezone.utc)

LOGGERS = ("app.vault.sync", "app.vault.ingest", "app.vault.deletions", "app.vault.holds")

SECRET_PATHS = [
    "Anchor/Memory/Утро.md",
    "Anchor/Memory/Мусор.md",
    "Anchor/Memory/Правило.md",
]
SECRET_TEXTS = [
    "Любит вставать рано",
    "мой сайт https://example.com/nick тег: @nick",
    "Не звонить после десяти.",
    "Факт, который забудут",
]


@pytest.fixture
def live_loggers(monkeypatch):
    for name in LOGGERS:
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)


def _settings(cap: int = 50) -> Settings:
    return Settings(VAULT_MODE="sync", VAULT_API_TOKEN=TOKEN, VAULT_MAX_WRITES_PER_PASS=cap)


def _record_text(record: logging.LogRecord) -> str:
    parts = [record.getMessage()]
    for key, value in vars(record).items():
        if key in ("msg", "args", "message") or key.startswith("_"):
            continue
        parts.append(f"{key}={value!r}")
    return " ".join(parts)


async def test_ingest_quarantine_delete_and_hold_are_logged_without_secrets(
    sessionmaker, caplog, live_loggers
):
    vault = FakeVault()
    clock = FrozenClock(NOW)
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, timezone="Europe/Paris", vault_epoch=EPOCH))
        await session.commit()

    # A new fact from a file (ingest -> created_facts).
    vault.files[SECRET_PATHS[0]] = (
        f"---\nanchor: fact\nkind: preference\npinned: false\nfact: {SECRET_TEXTS[0]}\n---\n"
    )
    # A file that quarantines (bad kind).
    vault.files[SECRET_PATHS[1]] = (
        f"---\nanchor: fact\nkind: hobby\npinned: false\nfact: {SECRET_TEXTS[1]}\n---\n"
    )
    # A rule file, which opens a hold and applies nothing.
    vault.files[SECRET_PATHS[2]] = (
        f"---\nanchor: fact\nkind: rule\npinned: false\nfact: {SECRET_TEXTS[2]}\n---\n"
    )
    # A fact that will be deleted from the vault and forgotten.
    async with sessionmaker() as session:
        forgotten = await memory.write_memory(
            session, kind="preference", text=SECRET_TEXTS[3], source="user"
        )
        forgotten_id = forgotten.id

    def cold_running_since():
        return NOW - datetime.timedelta(seconds=limits.SYNC_WARMUP_S + 60)

    original_status = vault.status

    async def status():
        result = await original_status()
        return result.__class__(
            sync_running=result.sync_running,
            restarts=result.restarts,
            last_exit_code=result.last_exit_code,
            running_since=cold_running_since(),
        )

    vault.status = status

    caplog.set_level(logging.DEBUG)

    async with sessionmaker() as session:
        await run_vault_sync(session, _settings(), clock, vault)
    forgotten_path = f"Anchor/Memory/{forgotten_id:04d}-{EPOCH}.md"
    del vault.files[forgotten_path]
    async with sessionmaker() as session:
        await run_vault_sync(session, _settings(), clock, vault)  # missing_since set
    clock.advance(datetime.timedelta(seconds=limits.DELETE_GRACE_S + 10))
    async with sessionmaker() as session:
        result = await run_vault_sync(session, _settings(), clock, vault)

    assert result.forgotten_facts >= 1

    messages = [r.getMessage() for r in caplog.records]
    assert "vault fact quarantined" in messages
    assert "vault rule hold opened" in messages
    assert "vault fact forgotten" in messages
    assert "vault fact ingested" in messages

    haystack = "\n".join(_record_text(r) for r in caplog.records)
    for secret in SECRET_PATHS + SECRET_TEXTS + [forgotten_path]:
        assert secret not in haystack

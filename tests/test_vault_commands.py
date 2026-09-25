"""/vault, the /state line, and what every VAULT_MODE does *not* do (phase-5 plan 3, 8).

5a's kill-switch contract, pinned here:

- `off`: no request to the vault service at all, not even a status probe;
- `status`, and `mirror`/`sync` until 5b/5c ship: `GET /v1/status` and
  nothing else -- never the manifest, a file, a write, a delete or a
  purge;
- in no mode does the heartbeat enqueue a vault job.
"""

from __future__ import annotations

import datetime
import logging
import socket

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import select

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.scheduler import heartbeat
from app.db.models import Job, TelegramUpdate, UserState, VaultStatus
from app.tg import vault as vault_ui
from app.tg.router import BOT_COMMANDS, build_router
from conftest import FakeLLMProvider, FakeSession
from vault_stub import TOKEN, start_stub

CHAT_ID = 555
TIMEZONE = "Europe/Paris"
# 12:00 in Paris; the stub's running_since is 08:00 UTC = 10:00 Paris.
NOW = datetime.datetime(2026, 9, 25, 10, 0, tzinfo=datetime.timezone.utc)
MODES = ("off", "status", "mirror", "sync")


@pytest.fixture
async def stub():
    stub, server = await start_stub()
    yield stub
    await server.close()


def _settings(mode: str, url: str) -> Settings:
    return Settings(VAULT_MODE=mode, VAULT_API_TOKEN=TOKEN, VAULT_URL=url)


def _closed_port_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


def _command_update(update_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Test"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(text)}],
        },
    }


async def _seed(sessionmaker) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE))
        await session.commit()
        for update_id in range(1, 6):
            session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()


async def _run(sessionmaker, settings: Settings, text: str, *, update_id: int = 1, clock=None) -> str:
    fake = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)
    dp = Dispatcher()
    dp.include_router(
        build_router(sessionmaker, settings, FakeLLMProvider(), clock=clock or FrozenClock(NOW))
    )
    await dp.feed_update(
        bot, Update.model_validate(_command_update(update_id, text), context={"bot": bot})
    )
    return fake.sent[-1].text


def _vault_line(state_text: str) -> str:
    return next(row for row in state_text.splitlines() if row.startswith("Хранилище"))


async def _status_row(sessionmaker) -> VaultStatus | None:
    async with sessionmaker() as session:
        return await session.get(VaultStatus, 1)


# --- /vault ---


def test_vault_is_in_the_command_menu():
    assert "vault" in {c.command for c in BOT_COMMANDS}


async def test_off_says_so_and_makes_no_request(sessionmaker, stub):
    await _seed(sessionmaker)
    reply = await _run(sessionmaker, _settings("off", stub.url), "/vault")
    assert reply == vault_ui.OFF_REPLY
    assert stub.requests == []
    assert await _status_row(sessionmaker) is None


async def test_status_running(sessionmaker, stub):
    await _seed(sessionmaker)
    stub.set_status(running=True, restarts=1)
    reply = await _run(sessionmaker, _settings("status", stub.url), "/vault")
    assert reply == "Хранилище: синхронизация ок (работает с 10:00, перезапусков 1)."
    row = await _status_row(sessionmaker)
    assert row.last_ok_at == NOW
    assert row.ob_running_since == datetime.datetime(2026, 9, 25, 8, 0, tzinfo=datetime.timezone.utc)
    assert row.last_unavailable_at is None


async def test_status_stopped(sessionmaker, stub):
    await _seed(sessionmaker)
    stub.set_status(running=False, restarts=4, last_exit_code=1)
    reply = await _run(sessionmaker, _settings("status", stub.url), "/vault")
    assert reply == "Хранилище: синхронизация остановлена (перезапусков 4, код выхода 1)."


async def test_unreachable_names_the_last_answer(sessionmaker, stub):
    await _seed(sessionmaker)
    await _run(sessionmaker, _settings("status", stub.url), "/vault", update_id=1)
    later = FrozenClock(NOW + datetime.timedelta(minutes=30))
    reply = await _run(
        sessionmaker, _settings("status", _closed_port_url()), "/vault", update_id=2, clock=later
    )
    assert reply == vault_ui.UNREACHABLE_LINE + " Последний ответ — 12:00."
    row = await _status_row(sessionmaker)
    assert row.last_unavailable_at == NOW + datetime.timedelta(minutes=30)
    assert row.last_ok_at == NOW


async def test_unreachable_never_seen(sessionmaker):
    await _seed(sessionmaker)
    reply = await _run(sessionmaker, _settings("status", _closed_port_url()), "/vault")
    assert reply == vault_ui.UNREACHABLE_LINE


async def test_a_token_mismatch_is_named(sessionmaker, stub):
    await _seed(sessionmaker)
    stub.respond("GET", "/v1/status", 401, {"error": "unauthorized"})
    reply = await _run(sessionmaker, _settings("status", stub.url), "/vault")
    assert reply == vault_ui.UNAUTHORIZED_LINE
    assert TOKEN not in reply


async def test_mirror_counts_facts_and_says_edits_are_not_applied(sessionmaker, stub):
    await _seed(sessionmaker)
    reply = await _run(sessionmaker, _settings("mirror", stub.url), "/vault")
    lines = reply.splitlines()
    assert lines[0] == "Хранилище: синхронизация ок (работает с 10:00, перезапусков 0) · фактов 0"
    assert lines[1] == vault_ui.MIRROR_NOTE


async def test_sync_says_it_acts_as_mirror_until_5c(sessionmaker, stub):
    await _seed(sessionmaker)
    reply = await _run(sessionmaker, _settings("sync", stub.url), "/vault")
    lines = reply.splitlines()
    assert lines[1] == vault_ui.EARLY_MODE_NOTE.format(mode="sync")
    assert lines[2] == vault_ui.MIRROR_NOTE


# --- /state ---


async def test_state_line_off(sessionmaker, stub):
    await _seed(sessionmaker)
    text = await _run(sessionmaker, _settings("off", stub.url), "/state")
    assert _vault_line(text) == vault_ui.STATE_OFF
    assert stub.requests == []


async def test_state_line_ok_and_stopped(sessionmaker, stub):
    await _seed(sessionmaker)
    text = await _run(sessionmaker, _settings("status", stub.url), "/state", update_id=1)
    assert _vault_line(text) == vault_ui.STATE_OK
    stub.set_status(running=False, restarts=2, last_exit_code=1)
    text = await _run(sessionmaker, _settings("status", stub.url), "/state", update_id=2)
    assert _vault_line(text) == vault_ui.STATE_STOPPED


async def test_state_line_unreachable_since(sessionmaker, stub):
    await _seed(sessionmaker)
    await _run(sessionmaker, _settings("status", stub.url), "/state", update_id=1)
    text = await _run(sessionmaker, _settings("status", _closed_port_url()), "/state", update_id=2)
    assert _vault_line(text) == "Хранилище: нет связи с 12:00"


# --- what each mode does not do ---


@pytest.mark.parametrize("mode", MODES)
async def test_no_mode_does_more_than_probe_status(sessionmaker, stub, mode):
    await _seed(sessionmaker)
    settings = _settings(mode, stub.url)
    await _run(sessionmaker, settings, "/vault", update_id=1)
    await _run(sessionmaker, settings, "/state", update_id=2)
    expected = [] if mode == "off" else [("GET", "/v1/status")] * 2
    assert stub.calls() == expected


@pytest.mark.parametrize("mode", MODES)
async def test_the_heartbeat_enqueues_no_vault_job_in_any_mode(sessionmaker, stub, mode):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        await heartbeat(session, _settings(mode, stub.url), FrozenClock(NOW))
    async with sessionmaker() as session:
        kinds = (await session.execute(select(Job.kind))).scalars().all()
    assert not [kind for kind in kinds if kind.startswith("vault")]
    assert stub.requests == []


async def test_probe_logs_carry_codes_not_urls(sessionmaker, caplog, monkeypatch):
    # alembic's fileConfig (run by the database fixture) disables every
    # logger that already exists; this one must be live to be tested.
    monkeypatch.setattr(logging.getLogger("app.vault.status"), "disabled", False)
    await _seed(sessionmaker)
    url = _closed_port_url()
    with caplog.at_level(logging.DEBUG):
        await _run(sessionmaker, _settings("status", url), "/vault")
    blob = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert "unavailable" in blob
    assert url not in blob
    assert TOKEN not in blob

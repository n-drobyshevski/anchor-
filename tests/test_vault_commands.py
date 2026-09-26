"""/vault, the /state line, and what every VAULT_MODE does *not* do (phase-8 plan 3, 8).

8a's kill-switch contract, pinned here:

- `off`: no request to the vault service at all, not even a status probe;
- `status`, and `mirror`/`sync` until 8b/8c ship: `GET /v1/status` and
  nothing else -- never a file, a write, a delete or a purge. 8e adds
  exactly one request: `/vault` reads the manifest, to count notes,
  while notes consent is on (docs/decisions.md);
- in no mode does the heartbeat enqueue a vault job.
"""

from __future__ import annotations

import datetime
import logging
import socket

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import select, text, update

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.scheduler import heartbeat
from app.core import purge
from app.db.models import (
    Job,
    NoteChunkKnowledge,
    NoteChunkPersonal,
    TelegramUpdate,
    UserState,
    VaultFile,
    VaultStatus,
)
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
    assert reply == "Хранилище: синхронизация ок (работает с 10:00, перезапусков 1).\n" + vault_ui.NOTES_OFF_LINE
    row = await _status_row(sessionmaker)
    assert row.last_ok_at == NOW
    assert row.ob_running_since == datetime.datetime(2026, 9, 25, 8, 0, tzinfo=datetime.timezone.utc)
    assert row.last_unavailable_at is None


async def test_status_stopped(sessionmaker, stub):
    await _seed(sessionmaker)
    stub.set_status(running=False, restarts=4, last_exit_code=1)
    reply = await _run(sessionmaker, _settings("status", stub.url), "/vault")
    assert reply.splitlines() == [
        "Хранилище: синхронизация остановлена (перезапусков 4, код выхода 1).",
        vault_ui.NOTES_OFF_LINE,
    ]


async def test_unreachable_names_the_last_answer(sessionmaker, stub):
    await _seed(sessionmaker)
    await _run(sessionmaker, _settings("status", stub.url), "/vault", update_id=1)
    later = FrozenClock(NOW + datetime.timedelta(minutes=30))
    reply = await _run(
        sessionmaker, _settings("status", _closed_port_url()), "/vault", update_id=2, clock=later
    )
    assert reply == vault_ui.UNREACHABLE_LINE + " Последний ответ — 12:00.\n" + vault_ui.NOTES_OFF_LINE
    row = await _status_row(sessionmaker)
    assert row.last_unavailable_at == NOW + datetime.timedelta(minutes=30)
    assert row.last_ok_at == NOW


async def test_unreachable_never_seen(sessionmaker):
    await _seed(sessionmaker)
    reply = await _run(sessionmaker, _settings("status", _closed_port_url()), "/vault")
    assert reply == vault_ui.UNREACHABLE_LINE + "\n" + vault_ui.NOTES_OFF_LINE


async def test_a_token_mismatch_is_named(sessionmaker, stub):
    await _seed(sessionmaker)
    stub.respond("GET", "/v1/status", 401, {"error": "unauthorized"})
    reply = await _run(sessionmaker, _settings("status", stub.url), "/vault")
    assert reply.splitlines()[0] == vault_ui.UNAUTHORIZED_LINE
    assert TOKEN not in reply


async def test_mirror_counts_facts_and_says_edits_are_not_applied(sessionmaker, stub):
    await _seed(sessionmaker)
    reply = await _run(sessionmaker, _settings("mirror", stub.url), "/vault")
    lines = reply.splitlines()
    assert lines[0] == "Хранилище: синхронизация ок (работает с 10:00, перезапусков 0) · фактов 0"
    assert lines[1] == vault_ui.MIRROR_NOTE


async def test_sync_counts_facts_and_says_nothing_about_mirroring(sessionmaker, stub):
    # 8c: sync applies fact/kind/pinned edits, so /vault no longer claims
    # they go nowhere, and the mode is no longer "early" (implemented).
    await _seed(sessionmaker)
    reply = await _run(sessionmaker, _settings("sync", stub.url), "/vault")
    lines = reply.splitlines()
    assert lines[0] == "Хранилище: синхронизация ок (работает с 10:00, перезапусков 0) · фактов 0"
    assert vault_ui.EARLY_MODE_NOTE.format(mode="sync") not in reply
    assert vault_ui.MIRROR_NOTE not in reply


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
async def test_with_notes_consent_vault_also_reads_the_manifest_and_nothing_more(sessionmaker, stub, mode):
    """8e: the one added request, and only from /vault."""
    await _seed(sessionmaker)
    await _consent(sessionmaker, True)
    stub.respond("GET", "/v1/manifest", 200, _manifest())
    settings = _settings(mode, stub.url)
    await _run(sessionmaker, settings, "/vault", update_id=1)
    await _run(sessionmaker, settings, "/state", update_id=2)
    expected = [] if mode == "off" else [("GET", "/v1/status"), ("GET", "/v1/manifest"), ("GET", "/v1/status")]
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


# --- 8e: notes consent and the notes line ---


async def _consent(sessionmaker, on: bool) -> None:
    async with sessionmaker() as session:
        await session.execute(update(UserState).values(notes_consent=on))
        await session.commit()


async def _consent_now(sessionmaker) -> bool:
    async with sessionmaker() as session:
        return (await session.execute(select(UserState.notes_consent))).scalar_one()


def _manifest(personal: int = 0, knowledge: int = 0, **summary) -> dict:
    files = [{"path": "Anchor/Memory/0001-abcdef.md", "sha256": "a" * 64, "size": 1, "scope": "anchor"}]
    files += [
        {"path": f"Жизнь/{i}.md", "sha256": "b" * 64, "size": 1, "scope": "note", "class": "personal"}
        for i in range(personal)
    ]
    files += [
        {"path": f"Library/{i}.md", "sha256": "c" * 64, "size": 1, "scope": "note", "class": "knowledge"}
        for i in range(knowledge)
    ]
    base = {"conflict": 0, "legacy_read": 0, "unknown_value": 0, "settings": "ok"}
    return {"files": files, "summary": {**base, **summary}}


async def test_consent_is_off_by_default_and_vault_says_how_to_turn_it_on(sessionmaker, stub):
    await _seed(sessionmaker)
    reply = await _run(sessionmaker, _settings("mirror", stub.url), "/vault")
    assert reply.splitlines()[-1] == vault_ui.NOTES_OFF_LINE
    assert ("GET", "/v1/manifest") not in stub.calls()


async def test_notes_on_sets_consent_and_explains_itself(sessionmaker, stub):
    await _seed(sessionmaker)
    reply = await _run(sessionmaker, _settings("status", stub.url), "/vault notes on")
    assert reply == vault_ui.NOTES_ON_REPLY
    assert await _consent_now(sessionmaker) is True
    assert stub.requests == []


async def test_the_notes_line_counts_by_class_and_labels_what_needs_a_look(sessionmaker, stub):
    await _seed(sessionmaker)
    await _consent(sessionmaker, True)
    stub.respond("GET", "/v1/manifest", 200, _manifest(12, 40, conflict=2, legacy_read=3, unknown_value=1))
    reply = await _run(sessionmaker, _settings("status", stub.url), "/vault")
    assert reply.splitlines()[-1] == (
        "Заметки: личные 12 · знания 40 · проверить: конфликт 2, anchor: read 3"
        " · не прочитано: неизвестная метка 1"
    )
    assert "Жизнь" not in reply and "Library" not in reply


async def test_the_notes_line_shows_only_nonzero_parts(sessionmaker, stub):
    await _seed(sessionmaker)
    await _consent(sessionmaker, True)
    stub.respond("GET", "/v1/manifest", 200, _manifest(1, 0, legacy_read=1))
    reply = await _run(sessionmaker, _settings("status", stub.url), "/vault")
    assert reply.splitlines()[-1] == "Заметки: личные 1 · знания 0 · проверить: anchor: read 1"


async def test_an_unusable_settings_file_is_named(sessionmaker, stub):
    await _seed(sessionmaker)
    await _consent(sessionmaker, True)
    stub.respond("GET", "/v1/manifest", 200, _manifest(settings="invalid"))
    reply = await _run(sessionmaker, _settings("mirror", stub.url), "/vault")
    assert reply.splitlines()[-1] == vault_ui.NOTES_SETTINGS_INVALID_LINE


@pytest.mark.parametrize("manifest_status", [500, 200])
async def test_no_notes_line_when_the_manifest_fails(sessionmaker, stub, manifest_status):
    await _seed(sessionmaker)
    await _consent(sessionmaker, True)
    # 200 with a note that has no class: a protocol error, not a count.
    bad = {"files": [{"path": "x.md", "sha256": "a" * 64, "size": 1, "scope": "note"}],
           "summary": _manifest()["summary"]}
    stub.respond("GET", "/v1/manifest", manifest_status, bad)
    reply = await _run(sessionmaker, _settings("status", stub.url), "/vault")
    assert not any(line.startswith("Заметки") for line in reply.splitlines())
    assert "x.md" not in reply


async def test_no_notes_line_when_the_service_is_unreachable(sessionmaker):
    await _seed(sessionmaker)
    await _consent(sessionmaker, True)
    reply = await _run(sessionmaker, _settings("status", _closed_port_url()), "/vault")
    assert reply == vault_ui.UNREACHABLE_LINE


async def test_notes_off_empties_both_chunk_tables_and_every_note_row(sessionmaker, stub):
    """8e plan section 11: `/vault notes off` empties both chunk tables."""
    await _seed(sessionmaker)
    await _consent(sessionmaker, True)
    async with sessionmaker() as session:
        personal = VaultFile(path="Жизнь/Бег.md", role="note", note_class="personal")
        knowledge = VaultFile(path="Library/CCRU.md", role="note", note_class="knowledge")
        fact = VaultFile(path="Anchor/Memory/0001-abcdef.md", role="fact")
        session.add_all([personal, knowledge, fact])
        await session.flush()
        session.add(NoteChunkPersonal(file_id=personal.id, ord=0, text="Бегаю по утрам."))
        session.add(NoteChunkKnowledge(file_id=knowledge.id, ord=0, text="Hyperstition."))
        await session.commit()
    reply = await _run(sessionmaker, _settings("mirror", stub.url), "/vault notes off")
    assert reply == vault_ui.NOTES_OFF_REPLY
    assert await _consent_now(sessionmaker) is False
    async with sessionmaker() as session:
        for table in ("note_chunk_personal", "note_chunk_knowledge"):
            assert (await session.execute(text(f"select count(*) from {table}"))).scalar_one() == 0
        roles = (await session.execute(select(VaultFile.role))).scalars().all()
    assert roles == ["fact"]
    assert stub.requests == []


@pytest.mark.parametrize("args", ["notes", "notes maybe", "on", "notes on please", "facts off"])
async def test_anything_else_gets_the_usage_and_changes_nothing(sessionmaker, stub, args):
    await _seed(sessionmaker)
    reply = await _run(sessionmaker, _settings("mirror", stub.url), f"/vault {args}")
    assert reply == vault_ui.VAULT_USAGE
    assert await _consent_now(sessionmaker) is False
    assert stub.requests == []


async def test_delete_turns_notes_consent_off(sessionmaker, clock):
    """8e plan section 11: after /delete, notes_consent is false."""
    await _seed(sessionmaker)
    await _consent(sessionmaker, True)
    async with sessionmaker() as session:
        await purge.delete_everything(session, Settings(), clock)
    assert await _consent_now(sessionmaker) is False


async def test_the_notes_line_logs_no_path(sessionmaker, stub, caplog, monkeypatch):
    monkeypatch.setattr(logging.getLogger("app.vault.status"), "disabled", False)
    await _seed(sessionmaker)
    await _consent(sessionmaker, True)
    stub.respond("GET", "/v1/manifest", 200, _manifest(2, 2, conflict=1))
    with caplog.at_level(logging.DEBUG):
        await _run(sessionmaker, _settings("status", stub.url), "/vault", update_id=1)
        stub.respond("GET", "/v1/manifest", 500, {"error": "x"})
        await _run(sessionmaker, _settings("status", stub.url), "/vault", update_id=2)
    blob = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert "Жизнь" not in blob and "Library" not in blob and ".md" not in blob

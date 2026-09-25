"""/export (phase-2 plan sections 11 and 14).

The export's job is to be an accurate record, so the tests care about
two things beyond "it has rows": that the numbers survive the round trip
exactly, and that nothing about it reaches a log.
"""

from __future__ import annotations

import datetime
import decimal
import json
import logging

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update

from app.config import Settings
from app.core import export
from app.db import models
from app.db.models import (
    Checkin,
    Journal,
    Memory,
    Message,
    Proposal,
    Scene,
    SpendLedger,
    StateChange,
    TelegramUpdate,
    UserState,
)
from app.tg import data as data_ui
from app.tg.router import build_router
from conftest import FakeLLMProvider, FakeSession

pytestmark = pytest.mark.asyncio

CHAT_ID = 555
TIMEZONE = "Europe/Paris"
SECRET_TEXT = "пользователь живёт в Лилле"
NOTE_CHUNK_TEXT = "Бегаю по утрам в парке."


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


def _build_dp(sessionmaker):
    fake = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, Settings(), FakeLLMProvider(), FakeLLMProvider()))
    return dp, bot, fake


async def _seed_everything(sessionmaker, *extra_update_ids: int) -> None:
    """One row in each of the nine exported tables.

    `extra_update_ids` are queue rows for updates a test will feed
    afterwards -- message.update_id is a foreign key into
    telegram_update, so a command handler storing its own reply needs
    its row to exist first, exactly as the real webhook path guarantees.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    today = datetime.date.today()
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE, streak=3))
        await session.commit()
        session.add(TelegramUpdate(update_id=1, payload={"update_id": 1}))
        for update_id in extra_update_ids:
            session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()
        scene = Scene(started_at=now, ended_at=now, summary="Говорили про отчёт.")
        session.add(scene)
        await session.commit()
        await session.refresh(scene)
        session.add_all(
            [
                Message(
                    role="user", content=SECRET_TEXT, ooc=False, kind="chat",
                    update_id=1, scene_id=scene.id,
                    usd_cost=decimal.Decimal("0.000108"),
                ),
                Memory(kind="identity", text=SECRET_TEXT, source="user"),
                Checkin(local_date=today, day_rating=4, due_result="partial", note="устал"),
                Proposal(field="due_action", value="сдать отчёт", reason="договорились"),
                Journal(local_date=today, text="Поговорили про отчёт."),
                StateChange(field="intensity", old_value="3", new_value="4", source="command"),
                SpendLedger(
                    local_date=today, category="chat", usd_cost=decimal.Decimal("0.000108")
                ),
                # 3a and H2. Both reached /export only in 4a, when the
                # coverage test at the bottom of this file found them
                # missing.
                models.Outbound(
                    kind="morning",
                    local_date=today,
                    bucket=0,
                    planned_for=now,
                    status="sent",
                    sent_at=now,
                ),
                models.SafetyEvent(
                    local_date=today, kind="welfare", outcome="ok", model="fake-safety"
                ),
            ]
        )
        await session.commit()

    # 4a. Added in a second flush because study_clip and study_card need
    # the ids of the rows above them.
    async with sessionmaker() as session:
        job = models.StudyJob(kind="read", local_date=today, status="done")
        session.add(job)
        await session.flush()
        clip = models.StudyClip(
            job_id=job.id,
            url="https://example.com/sleep",
            domain="example.com",
            title="Как высыпаться",
            text="Ложитесь спать в одно и то же время каждый день.",
            http_status=200,
        )
        session.add(clip)
        await session.flush()
        session.add(
            models.StudyCard(
                job_id=job.id,
                clip_id=clip.id,
                kind="technique",
                text="Ложиться в одно и то же время.",
                quote="Ложитесь спать в одно и то же время каждый день.",
                source_url="https://example.com/sleep",
                risk_model="low",
                risk_rules="low",
                risk_final="low",
            )
        )
        await session.commit()

    # 5a: the two exported vault tables, plus the two omitted ones so the
    # omission is tested against rows that exist.
    async with sessionmaker() as session:
        hold = models.VaultHold(
            kind="rule",
            payload={"file_id": 1, "kind": "rule", "text": "не звонить после десяти", "supersedes_id": None},
        )
        session.add(hold)
        await session.flush()
        session.add(
            models.VaultFile(
                path="Anchor/Memory/0001-abcdef.md", role="fact", state="held", hold_id=hold.id
            )
        )
        note = models.VaultFile(path="Бег.md", role="note")
        session.add(note)
        await session.flush()
        session.add(models.VaultChunk(file_id=note.id, ord=0, heading="Бег", text=NOTE_CHUNK_TEXT))
        session.add(models.VaultStatus(id=1, last_ok_at=now))
        await session.commit()


# --- contents ---


async def test_export_contains_every_exported_table_with_rows(sessionmaker, clock):
    await _seed_everything(sessionmaker)
    async with sessionmaker() as session:
        payload = await export.build_export(session, clock)

    expected = {m.__tablename__ for m in export.EXPORTED_MODELS}
    assert set(payload["tables"]) == expected
    for name, rows in payload["tables"].items():
        assert rows, f"{name} exported empty despite being seeded"


async def test_export_omits_the_plumbing_tables(sessionmaker, clock):
    """telegram_update, job and pending_memory are transport and queue;
    their only real content is message text `messages` already carries.
    5a adds vault_chunk (a copy of the user's own notes, which live in
    their vault) and vault_status (timestamps)."""
    await _seed_everything(sessionmaker)
    async with sessionmaker() as session:
        payload = await export.build_export(session, clock)

    for name in (
        "telegram_update", "job", "pending_memory", "persona_version", "vault_chunk", "vault_status"
    ):
        assert name not in payload["tables"]
    assert NOTE_CHUNK_TEXT not in json.dumps(payload, default=str, ensure_ascii=False)


async def test_the_bytes_are_valid_json_and_round_trip(sessionmaker, clock):
    await _seed_everything(sessionmaker)
    async with sessionmaker() as session:
        payload = await export.build_export(session, clock)

    parsed = json.loads(export.to_bytes(payload).decode("utf-8"))
    assert set(parsed["tables"]) == set(payload["tables"])
    assert parsed["tables"]["memory"][0]["text"] == SECRET_TEXT


async def test_money_survives_exactly_as_a_string(sessionmaker, clock):
    """usd_cost is Numeric(10, 6). Through a float it would quietly stop
    being the number that was stored."""
    await _seed_everything(sessionmaker)
    async with sessionmaker() as session:
        payload = await export.build_export(session, clock)
    parsed = json.loads(export.to_bytes(payload).decode("utf-8"))

    value = parsed["tables"]["spend_ledger"][0]["usd_cost"]
    assert isinstance(value, str)
    assert decimal.Decimal(value) == decimal.Decimal("0.000108")


async def test_datetimes_and_dates_are_iso(sessionmaker, clock):
    await _seed_everything(sessionmaker)
    async with sessionmaker() as session:
        payload = await export.build_export(session, clock)
    parsed = json.loads(export.to_bytes(payload).decode("utf-8"))

    created = parsed["tables"]["memory"][0]["created_at"]
    assert "T" in created
    datetime.datetime.fromisoformat(created)  # raises if it is not ISO-8601

    local_date = parsed["tables"]["journal"][0]["local_date"]
    assert datetime.date.fromisoformat(local_date) == datetime.date.today()


async def test_cyrillic_is_readable_not_escaped(sessionmaker, clock):
    """The file is meant to be opened and read, not only re-imported."""
    await _seed_everything(sessionmaker)
    async with sessionmaker() as session:
        payload = await export.build_export(session, clock)

    raw = export.to_bytes(payload).decode("utf-8")
    assert SECRET_TEXT in raw
    assert "\\u0436" not in raw


async def test_an_empty_database_still_exports_every_table(sessionmaker, clock):
    async with sessionmaker() as session:
        payload = await export.build_export(session, clock)

    assert set(payload["tables"]) == {m.__tablename__ for m in export.EXPORTED_MODELS}
    assert all(rows == [] for rows in payload["tables"].values())


async def test_filename_uses_the_local_date(clock):
    expected = datetime.datetime.now(
        __import__("zoneinfo").ZoneInfo(TIMEZONE)
    ).strftime("%Y%m%d")
    assert export.export_filename(clock, TIMEZONE) == f"anchor-export-{expected}.json"


# --- the command ---


async def test_the_command_sends_a_document_with_the_right_name(sessionmaker):
    await _seed_everything(sessionmaker, 2)
    dp, bot, fake = _build_dp(sessionmaker)

    await dp.feed_update(
        bot, Update.model_validate(_command_update(2, "/export"), context={"bot": bot})
    )

    assert len(fake.documents) == 1
    sent = fake.documents[0]
    assert sent.document.filename.startswith("anchor-export-")
    assert sent.document.filename.endswith(".json")
    # parse_mode is explicitly None: everything this bot sends is plain text.
    assert sent.parse_mode is None

    parsed = json.loads(sent.document.data.decode("utf-8"))
    assert parsed["tables"]["memory"][0]["text"] == SECRET_TEXT


async def test_a_replayed_export_sends_one_document(sessionmaker):
    await _seed_everything(sessionmaker, 2)
    dp, bot, fake = _build_dp(sessionmaker)

    for _ in range(2):
        await dp.feed_update(
            bot, Update.model_validate(_command_update(2, "/export"), context={"bot": bot})
        )

    assert len(fake.documents) == 1


async def test_export_logs_no_contents(sessionmaker, caplog):
    """Plan section 11: "Never logs contents". Sizes and counts only."""
    await _seed_everything(sessionmaker, 2)
    dp, bot, fake = _build_dp(sessionmaker)

    with caplog.at_level(logging.DEBUG):
        await dp.feed_update(
            bot, Update.model_validate(_command_update(2, "/export"), context={"bot": bot})
        )

    blob = "\n".join(
        [r.getMessage() for r in caplog.records]
        + [str(v) for r in caplog.records for v in vars(r).values()]
    )
    assert SECRET_TEXT not in blob
    assert "Лилл" not in blob
    assert "устал" not in blob


async def test_an_oversized_export_explains_instead_of_failing(sessionmaker):
    """Without the guard the failure is an opaque Telegram API error."""
    await _seed_everything(sessionmaker, 2)
    dp, bot, fake = _build_dp(sessionmaker)

    original = data_ui.DOCUMENT_LIMIT
    data_ui.DOCUMENT_LIMIT = 10
    try:
        await dp.feed_update(
            bot, Update.model_validate(_command_update(2, "/export"), context={"bot": bot})
        )
    finally:
        data_ui.DOCUMENT_LIMIT = original

    assert fake.documents == []
    assert fake.sent[-1].text.startswith("Слишком много данных")


# --- coverage ---

# Tables deliberately left out of /export, each with the reason. A new
# table must be added to EXPORTED_MODELS or named here; there is no
# third option, and that is the whole point of the test below.
NOT_EXPORTED = {
    "telegram_update": "transport: Telegram's own envelope around text `messages` carries",
    "job": "queue plumbing; payloads reference rows that are exported",
    "pending_memory": "unclassified /remember text, exported once it becomes a memory",
    "persona_version": "a hash of a file in this repo, not user data",
    # 5a (phase-5 plan section 6).
    "vault_chunk": "a derived copy of the user's own opted-in notes, rebuildable from the vault",
    "vault_status": "operational timestamps, no content",
}


async def test_every_table_is_either_exported_or_deliberately_omitted():
    """The same pressure tests/test_delete.py puts on purge.py.

    Without this, EXPORTED_MODELS was only ever checked against itself:
    a new table added to models.py would be silently missing from
    /export, and the two tests above would still pass because both
    derive their expectation from EXPORTED_MODELS. That is a data-control
    promise failing quietly, which is the one way it must not fail.
    """
    exported = {m.__tablename__ for m in export.EXPORTED_MODELS}
    unaccounted = set(models.Base.metadata.tables) - exported - set(NOT_EXPORTED)
    assert not unaccounted, (
        "new table(s) in models.py are neither exported nor listed in "
        f"NOT_EXPORTED: {sorted(unaccounted)}"
    )


async def test_the_omission_list_does_not_name_a_table_that_is_exported():
    """Guards the guard: a stale NOT_EXPORTED entry would hide a real gap."""
    exported = {m.__tablename__ for m in export.EXPORTED_MODELS}
    assert not (exported & set(NOT_EXPORTED))
    assert set(NOT_EXPORTED) <= set(models.Base.metadata.tables)

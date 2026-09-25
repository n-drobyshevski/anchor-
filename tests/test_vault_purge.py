"""/delete reaches the vault (phase-8 plan section 10), and the 8b write-path changes.

- `/delete` queues exactly one `vault_purge` inside its own single
  transaction whenever a token is set, and draws a new epoch;
- the purge job defers on any failure without spending an attempt;
- files re-uploaded from before the delete carry the old epoch and are
  deleted by the next pass, never imported;
- `write_memory`/`set_pinned` can share the caller's transaction, and a
  supersede moves `vault_file.memory_id` to the head;
- `/forget` of an adopted technique with a real card works (a phase-4
  fix): the card becomes `forgotten`.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core import memory, purge
from app.core.clock import FrozenClock, SystemClock
from app.db.models import Job, Memory, StudyCard, StudyClip, StudyJob, UserState, VaultFile
from app.vault.kinds import VAULT_PURGE
from app.vault.sync import run_vault_sync
from app.worker import process_one_job
from conftest import FakeLLMProvider
from vault_fake import FakeVault
from vault_stub import TOKEN, start_stub

NOW = datetime.datetime(2026, 9, 25, 10, 0, tzinfo=datetime.timezone.utc)


async def _seed(sessionmaker, epoch: str = "abcdef") -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, timezone="Europe/Paris", vault_epoch=epoch))
        await session.commit()


async def _jobs(sessionmaker) -> list[Job]:
    async with sessionmaker() as session:
        return list((await session.execute(select(Job))).scalars())


# --- /delete -------------------------------------------------------------------


async def test_delete_with_a_token_queues_exactly_one_purge_and_a_new_epoch(sessionmaker):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        session.add(Job(kind="extract", payload={}, dedup_key="extract:1"))
        await session.commit()
    settings = Settings(VAULT_API_TOKEN=TOKEN)
    async with sessionmaker() as session:
        await purge.delete_everything(session, settings, SystemClock())
    [job] = await _jobs(sessionmaker)
    assert (job.kind, job.dedup_key, job.status) == (VAULT_PURGE, "vault_purge", "pending")
    async with sessionmaker() as session:
        assert (await session.get(UserState, 1)).vault_epoch != "abcdef"


async def test_delete_without_a_token_queues_nothing(sessionmaker):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        await purge.delete_everything(session, Settings(), SystemClock())
    assert await _jobs(sessionmaker) == []


async def test_the_purge_is_queued_inside_the_wipe_transaction(sessionmaker, monkeypatch):
    seen = []
    real = purge.enqueue_job

    async def spy(session, kind, payload, **kwargs):
        seen.append(kwargs.get("commit", True))
        return await real(session, kind, payload, **kwargs)

    monkeypatch.setattr(purge, "enqueue_job", spy)
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        await purge.delete_everything(session, Settings(VAULT_API_TOKEN=TOKEN), SystemClock())
    assert seen == [False]


# --- the purge job ------------------------------------------------------------


@pytest.fixture
async def stub():
    stub, server = await start_stub()
    yield stub
    await server.close()


@pytest.mark.parametrize("status", [401, 500, 503])
async def test_a_failed_purge_defers_without_spending_an_attempt(sessionmaker, stub, status):
    await _seed(sessionmaker)
    stub.respond("POST", "/v1/purge", status, {"error": "x"})
    async with sessionmaker() as session:
        session.add(Job(kind=VAULT_PURGE, payload={}, dedup_key="vault_purge"))
        await session.commit()
    settings = Settings(VAULT_MODE="status", VAULT_API_TOKEN=TOKEN, VAULT_URL=stub.url)
    before = datetime.datetime.now(datetime.timezone.utc)
    assert await process_one_job(sessionmaker, settings, FakeLLMProvider(), SystemClock())
    [job] = await _jobs(sessionmaker)
    assert job.status == "pending" and job.attempts == 0 and job.error is None
    assert job.run_after >= before + datetime.timedelta(minutes=4, seconds=50)


async def test_an_unreachable_vault_defers_the_purge_too(sessionmaker):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        session.add(Job(kind=VAULT_PURGE, payload={}, dedup_key="vault_purge"))
        await session.commit()
    settings = Settings(VAULT_MODE="off", VAULT_API_TOKEN=TOKEN, VAULT_URL="http://127.0.0.1:9")
    assert await process_one_job(sessionmaker, settings, FakeLLMProvider(), SystemClock())
    [job] = await _jobs(sessionmaker)
    assert job.status == "pending" and job.attempts == 0


async def test_a_successful_purge_completes(sessionmaker, stub):
    await _seed(sessionmaker)
    stub.respond("POST", "/v1/purge", 200, {"deleted": 3})
    async with sessionmaker() as session:
        session.add(Job(kind=VAULT_PURGE, payload={}, dedup_key="vault_purge"))
        await session.commit()
    settings = Settings(VAULT_MODE="off", VAULT_API_TOKEN=TOKEN, VAULT_URL=stub.url)
    assert await process_one_job(sessionmaker, settings, FakeLLMProvider(), SystemClock())
    [job] = await _jobs(sessionmaker)
    assert job.status == "done"
    assert stub.calls() == [("POST", "/v1/purge")]


async def test_old_epoch_files_reuploaded_after_a_delete_are_deleted_not_imported(sessionmaker):
    """The epoch's whole reason to exist (plan section 4)."""
    await _seed(sessionmaker)
    vault = FakeVault()
    clock = FrozenClock(NOW)
    settings = Settings(VAULT_MODE="mirror", VAULT_API_TOKEN=TOKEN)
    async with sessionmaker() as session:
        await memory.write_memory(session, kind="identity", text="старый факт", source="user")
    async with sessionmaker() as session:
        await run_vault_sync(session, settings, clock, vault)
    old_files = dict(vault.files)
    assert old_files

    async with sessionmaker() as session:
        await purge.delete_everything(session, settings, SystemClock())
    await vault.purge()
    async with sessionmaker() as session:
        await session.execute(Job.__table__.delete())
        await session.commit()
    # An offline phone comes back and re-uploads what it had.
    vault.files.update(old_files)
    async with sessionmaker() as session:
        await memory.write_memory(session, kind="identity", text="новый факт", source="user")
    async with sessionmaker() as session:
        result = await run_vault_sync(session, settings, clock, vault)

    assert result.orphans == len(old_files)
    assert not set(old_files) & set(vault.files)
    [content] = vault.files.values()
    assert "новый факт" in content and "старый факт" not in content
    async with sessionmaker() as session:
        texts = list((await session.execute(select(Memory.text))).scalars())
    assert texts == ["новый факт"]


# --- memory.py's 8b changes -----------------------------------------------------


async def test_commit_false_shares_the_callers_transaction(sessionmaker):
    async with sessionmaker() as session:
        written = await memory.write_memory(
            session, kind="identity", text="в одной транзакции", source="user", commit=False
        )
        assert written.id is not None
        await memory.set_pinned(session, written.id, True, commit=False)
        await session.rollback()
    async with sessionmaker() as session:
        assert list((await session.execute(select(Memory))).scalars()) == []


async def test_a_supersede_moves_the_file_pointer_to_the_head(sessionmaker):
    async with sessionmaker() as session:
        old = await memory.write_memory(session, kind="identity", text="живёт в Лилле", source="user")
        session.add(VaultFile(path="Anchor/Memory/0001-abcdef.md", role="fact", memory_id=old.id))
        await session.commit()
        new = await memory.write_memory(
            session, kind="identity", text="живёт в Руане", source="user", supersedes_id=old.id
        )
    async with sessionmaker() as session:
        [row] = list((await session.execute(select(VaultFile))).scalars())
    assert row.memory_id == new.id


async def test_forget_of_an_adopted_technique_with_a_real_card(sessionmaker):
    """Phase-4 bug: study_card.memory_id had no ON DELETE rule, so this raised."""
    async with sessionmaker() as session:
        job = StudyJob(kind="read", local_date=datetime.date(2026, 9, 25), status="done")
        session.add(job)
        await session.flush()
        clip = StudyClip(job_id=job.id, url="https://example.com/sleep", domain="example.com", text="x")
        session.add(clip)
        await session.flush()
        technique = await memory.write_memory(
            session, kind="technique", text="Ложиться в одно время.", source="adopt"
        )
        session.add(
            StudyCard(
                job_id=job.id, clip_id=clip.id, kind="technique", text="Ложиться в одно время.",
                quote="Ложитесь спать в одно и то же время.", source_url=clip.url,
                risk_model="low", risk_rules="low", risk_final="low",
                status="adopted", memory_id=technique.id,
            )
        )
        await session.commit()
        assert await memory.hard_delete(session, technique.id)
    async with sessionmaker() as session:
        [card] = list((await session.execute(select(StudyCard))).scalars())
        assert (card.status, card.memory_id) == ("forgotten", None)
        assert list((await session.execute(select(Memory))).scalars()) == []

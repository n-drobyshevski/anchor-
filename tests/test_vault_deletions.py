"""Deletions from the vault (phase-8 plan section 7.2), `sync` mode only.

Same harness as tests/test_vault_ingest.py.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core import memory
from app.core.clock import FrozenClock
from app.db.models import Journal, Memory, StudyCard, StudyClip, StudyJob, UserState, VaultFile, VaultHold
from app.vault import limits
from app.vault.sync import run_vault_sync
from vault_fake import FakeVault

EPOCH = "abcdef"
TOKEN = "vault-token-" + "v" * 32
NOW = datetime.datetime(2026, 9, 25, 10, 0, tzinfo=datetime.timezone.utc)


def _settings(cap: int = 50) -> Settings:
    return Settings(VAULT_MODE="sync", VAULT_API_TOKEN=TOKEN, VAULT_MAX_WRITES_PER_PASS=cap)


@pytest.fixture
def vault() -> FakeVault:
    return FakeVault()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


async def _seed(sessionmaker) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, timezone="Europe/Paris", vault_epoch=EPOCH))
        await session.commit()


async def _fact(sessionmaker, text: str, **kw) -> int:
    async with sessionmaker() as session:
        written = await memory.write_memory(session, kind="preference", text=text, source="user", **kw)
        return written.id


async def _pass(sessionmaker, vault, clock):
    async with sessionmaker() as session:
        return await run_vault_sync(session, _settings(), clock, vault)


def _path(memory_id: int) -> str:
    return f"Anchor/Memory/{memory_id:04d}-{EPOCH}.md"


async def _rows(sessionmaker) -> list[VaultFile]:
    async with sessionmaker() as session:
        return list((await session.execute(select(VaultFile).order_by(VaultFile.id))).scalars())


# vault_fake.FakeVault.status() always reports a fixed running_since;
# override it so `now - running_since` is a constant offset from
# whatever the frozen clock currently reads -- both sides of the
# warmup check are read within the same pass (app/vault/sync.py's
# run_vault_sync fetches status, then deletions reads the value it just
# recorded), so a constant offset gives an exact, clock-advance-proof
# boundary.
def _patch_running_since(vault: FakeVault, clock: FrozenClock, offset_seconds: float) -> None:
    original = vault.status

    async def status():
        result = await original()
        return result.__class__(
            sync_running=result.sync_running,
            restarts=result.restarts,
            last_exit_code=result.last_exit_code,
            running_since=clock.now_utc() - datetime.timedelta(seconds=offset_seconds),
        )

    vault.status = status


def _make_warm(vault: FakeVault, clock: FrozenClock) -> None:
    _patch_running_since(vault, clock, limits.SYNC_WARMUP_S + 60)


def _make_cold(vault: FakeVault, clock: FrozenClock) -> None:
    _patch_running_since(vault, clock, limits.SYNC_WARMUP_S - 60)


# --- grace and warmup ------------------------------------------------------------


async def test_a_deletion_is_not_applied_before_the_grace_period(sessionmaker, vault, clock):
    _make_warm(vault, clock)
    await _seed(sessionmaker)
    fact_id = await _fact(sessionmaker, "Любит кофе")
    await _pass(sessionmaker, vault, clock)
    del vault.files[_path(fact_id)]
    result = await _pass(sessionmaker, vault, clock)
    assert result.forgotten_facts == 0
    clock.advance(datetime.timedelta(seconds=limits.DELETE_GRACE_S - 10))
    result = await _pass(sessionmaker, vault, clock)
    assert result.forgotten_facts == 0
    async with sessionmaker() as session:
        assert await session.get(Memory, fact_id) is not None


async def test_a_deletion_is_not_applied_before_warmup(sessionmaker, vault, clock):
    # ob only just (re)started: running_since is close to now.
    _make_cold(vault, clock)
    await _seed(sessionmaker)
    fact_id = await _fact(sessionmaker, "Любит кофе")
    await _pass(sessionmaker, vault, clock)
    del vault.files[_path(fact_id)]
    await _pass(sessionmaker, vault, clock)
    clock.advance(datetime.timedelta(seconds=limits.DELETE_GRACE_S + 10))
    result = await _pass(sessionmaker, vault, clock)
    assert result.forgotten_facts == 0
    async with sessionmaker() as session:
        assert await session.get(Memory, fact_id) is not None


async def test_a_deletion_applies_after_both_grace_and_warmup(sessionmaker, vault, clock):
    _make_warm(vault, clock)
    await _seed(sessionmaker)
    fact_id = await _fact(sessionmaker, "Любит кофе")
    await _pass(sessionmaker, vault, clock)
    del vault.files[_path(fact_id)]
    await _pass(sessionmaker, vault, clock)  # missing_since set
    clock.advance(datetime.timedelta(seconds=limits.DELETE_GRACE_S + 10))
    result = await _pass(sessionmaker, vault, clock)
    assert result.forgotten_facts == 1
    async with sessionmaker() as session:
        assert await session.get(Memory, fact_id) is None
    assert await _rows(sessionmaker) == []


# --- the rolling-hour cap --------------------------------------------------------


async def test_more_than_the_cap_within_an_hour_opens_exactly_one_hold(sessionmaker, vault, clock):
    _make_warm(vault, clock)
    await _seed(sessionmaker)
    ids = [await _fact(sessionmaker, f"Факт {i}") for i in range(limits.MASS_DELETE_MAX + 1)]
    await _pass(sessionmaker, vault, clock)
    for i in ids:
        del vault.files[_path(i)]
    await _pass(sessionmaker, vault, clock)  # missing_since set for all
    clock.advance(datetime.timedelta(seconds=limits.DELETE_GRACE_S + 10))
    result = await _pass(sessionmaker, vault, clock)
    assert result.forgotten_facts == 0
    assert len(result.new_hold_ids) == 1
    async with sessionmaker() as session:
        holds = (await session.execute(select(VaultHold))).scalars().all()
    assert len(holds) == 1
    assert holds[0].kind == "mass_delete" and sorted(holds[0].payload["file_ids"]) == sorted(
        r.id for r in await _rows(sessionmaker)
    )
    for row in await _rows(sessionmaker):
        assert row.state == "held" and row.hold_id == holds[0].id
    # A further pass while the hold is pending opens no second hold.
    result2 = await _pass(sessionmaker, vault, clock)
    assert result2.new_hold_ids == []
    async with sessionmaker() as session:
        assert len((await session.execute(select(VaultHold))).scalars().all()) == 1


async def test_the_cap_is_checked_against_the_rolling_window_across_passes(sessionmaker, vault, clock):
    """One fact forgotten now, then MASS_DELETE_MAX more within the hour:
    the batch alone would be under the cap, but combined with the first
    forget it is not, so exactly one hold opens for the remaining batch."""
    _make_warm(vault, clock)
    await _seed(sessionmaker)
    first = await _fact(sessionmaker, "Первый факт")
    rest = [await _fact(sessionmaker, f"Факт {i}") for i in range(limits.MASS_DELETE_MAX)]
    await _pass(sessionmaker, vault, clock)
    del vault.files[_path(first)]
    await _pass(sessionmaker, vault, clock)
    clock.advance(datetime.timedelta(seconds=limits.DELETE_GRACE_S + 10))
    result = await _pass(sessionmaker, vault, clock)
    assert result.forgotten_facts == 1
    async with sessionmaker() as session:
        assert await session.get(Memory, first) is None

    for i in rest:
        del vault.files[_path(i)]
    clock.advance(datetime.timedelta(minutes=1))
    await _pass(sessionmaker, vault, clock)
    clock.advance(datetime.timedelta(seconds=limits.DELETE_GRACE_S + 10))
    result = await _pass(sessionmaker, vault, clock)
    assert result.forgotten_facts == 0
    assert len(result.new_hold_ids) == 1


async def test_reverting_a_mass_delete_hold_restores_the_files(sessionmaker, vault, clock):
    from app.vault import holds

    _make_warm(vault, clock)
    await _seed(sessionmaker)
    ids = [await _fact(sessionmaker, f"Факт {i}") for i in range(limits.MASS_DELETE_MAX + 1)]
    await _pass(sessionmaker, vault, clock)
    for i in ids:
        del vault.files[_path(i)]
    await _pass(sessionmaker, vault, clock)
    clock.advance(datetime.timedelta(seconds=limits.DELETE_GRACE_S + 10))
    result = await _pass(sessionmaker, vault, clock)
    [hold_id] = result.new_hold_ids
    async with sessionmaker() as session:
        decided = await holds.decide(session, hold_id, EPOCH, False, clock)
    assert decided.outcome == holds.REVERTED_RESULT
    await _pass(sessionmaker, vault, clock)
    for i in ids:
        assert _path(i) in vault.files
    async with sessionmaker() as session:
        for i in ids:
            assert await session.get(Memory, i) is not None


# --- forget_lineage's own guarantees ----------------------------------------------


async def test_forget_lineage_removes_the_whole_chain_and_writes_one_audit_row(sessionmaker):
    async with sessionmaker() as session:
        old = await memory.write_memory(session, kind="preference", text="Живёт в Лилле", source="user")
        new = await memory.write_memory(
            session, kind="preference", text="Живёт в Руане", source="user", supersedes_id=old.id
        )
        new_id = new.id
        outcome = await memory.forget_lineage(session, new_id, source="vault")
    assert outcome == memory.FORGET_OK
    async with sessionmaker() as session:
        assert await session.get(Memory, old.id) is None
        assert await session.get(Memory, new_id) is None
        from app.db.models import StateChange

        rows = (await session.execute(select(StateChange).where(StateChange.field == "memory"))).scalars().all()
    assert len(rows) == 1
    assert rows[0].old_value == str(new_id) and rows[0].new_value is None


async def test_forget_lineage_marks_the_adopted_card_forgotten(sessionmaker):
    async with sessionmaker() as session:
        adopted = await memory.write_memory(session, kind="technique", text="Спать по расписанию.", source="user")
        job = StudyJob(kind="read", local_date=datetime.date(2026, 9, 25), status="done")
        session.add(job)
        await session.flush()
        clip = StudyClip(job_id=job.id, url="https://example.com/a", domain="example.com",
                          title="t", text="x", http_status=200)
        session.add(clip)
        await session.flush()
        session.add(StudyCard(job_id=job.id, clip_id=clip.id, kind="technique", text="x", quote="y",
                               source_url="https://example.com/a", risk_model="low", risk_rules="low",
                               risk_final="low", status="adopted", memory_id=adopted.id))
        await session.commit()
        adopted_id = adopted.id
        card_id = (await session.execute(select(StudyCard.id))).scalar_one()

    async with sessionmaker() as session:
        outcome = await memory.forget_lineage(session, adopted_id, source="vault")
    assert outcome == memory.FORGET_PROTECTED
    async with sessionmaker() as session:
        card = await session.get(StudyCard, card_id)
    assert card.status == "adopted" and card.memory_id == adopted_id


async def test_a_protected_forget_brings_the_file_back(sessionmaker, vault, clock):
    """plan section 7.2: FORGET_PROTECTED restores the row rather than
    deleting it, with reason `protected`, and the file returns."""
    _make_warm(vault, clock)
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        adopted = await memory.write_memory(session, kind="technique", text="Спать по расписанию.", source="user")
        job = StudyJob(kind="read", local_date=datetime.date(2026, 9, 25), status="done")
        session.add(job)
        await session.flush()
        clip = StudyClip(job_id=job.id, url="https://example.com/a", domain="example.com",
                          title="t", text="x", http_status=200)
        session.add(clip)
        await session.flush()
        session.add(StudyCard(job_id=job.id, clip_id=clip.id, kind="technique", text="x", quote="y",
                               source_url="https://example.com/a", risk_model="low", risk_rules="low",
                               risk_final="low", status="adopted", memory_id=adopted.id))
        await session.commit()
        adopted_id = adopted.id
    await _pass(sessionmaker, vault, clock)
    path = _path(adopted_id)
    del vault.files[path]
    await _pass(sessionmaker, vault, clock)
    clock.advance(datetime.timedelta(seconds=limits.DELETE_GRACE_S + 10))
    result = await _pass(sessionmaker, vault, clock)
    assert result.forgotten_facts == 0
    # Deletions (this pass) mark the row `restore`; render (later in the
    # same pass) already recreates the file from it, so by the time the
    # pass returns the row is `ok` again and the file is back.
    async with sessionmaker() as session:
        row = (await session.execute(select(VaultFile))).scalar_one()
    assert row.state == "ok" and path in vault.files
    async with sessionmaker() as session:
        assert await session.get(Memory, adopted_id) is not None


# --- journal deletions -----------------------------------------------------------


async def test_a_deleted_journal_day_becomes_dismissed_and_is_never_recreated(sessionmaker, vault, clock):
    _make_warm(vault, clock)
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        session.add(Journal(local_date=datetime.date(2026, 9, 20), text="День."))
        await session.commit()
    await _pass(sessionmaker, vault, clock)
    path = f"Anchor/Journal/2026-09-20-{EPOCH}.md"
    assert path in vault.files
    del vault.files[path]
    await _pass(sessionmaker, vault, clock)
    clock.advance(datetime.timedelta(seconds=limits.DELETE_GRACE_S + 10))
    await _pass(sessionmaker, vault, clock)
    async with sessionmaker() as session:
        row = (
            await session.execute(select(VaultFile).where(VaultFile.role == "journal"))
        ).scalar_one()
    assert row.state == "dismissed"
    await _pass(sessionmaker, vault, clock)
    assert path not in vault.files


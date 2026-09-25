"""The sync pass in mirror mode (phase-8 plan sections 7, 7.3, 7.4, 10).

Every test runs the real pass against the throwaway database and an
in-memory vault with vaultd's compare-and-swap semantics
(tests/vault_fake.py). No network, no Obsidian.
"""

from __future__ import annotations

import datetime
import decimal

import pytest
from sqlalchemy import func, select, update

from app.config import Settings
from app.core import memory
from app.core.clock import FrozenClock
from app.db.jobs import enqueue_job
from app.db.models import (
    Checkin,
    Job,
    Journal,
    Memory,
    Message,
    StudyCard,
    StudyClip,
    StudyJob,
    UserState,
    VaultFile,
    VaultStatus,
)
from app.vault import frontmatter
from app.vault.kinds import VAULT_PURGE, VAULT_SYNC
from app.vault.sync import maybe_enqueue_vault_sync, run_vault_sync
from vault_fake import FakeVault, sha

EPOCH = "abcdef"
TOKEN = "vault-token-" + "v" * 32
# 12:00 in Paris.
NOW = datetime.datetime(2026, 9, 25, 10, 0, tzinfo=datetime.timezone.utc)
TODAY = datetime.date(2026, 9, 25)


def _settings(mode: str = "mirror", cap: int = 50) -> Settings:
    return Settings(VAULT_MODE=mode, VAULT_API_TOKEN=TOKEN, VAULT_MAX_WRITES_PER_PASS=cap)


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


async def _fact(sessionmaker, text: str, *, kind: str = "preference", **kw) -> int:
    async with sessionmaker() as session:
        written = await memory.write_memory(session, kind=kind, text=text, source="user", **kw)
        return written.id


async def _pass(sessionmaker, vault, clock, settings=None):
    async with sessionmaker() as session:
        return await run_vault_sync(session, settings or _settings(), clock, vault)


async def _rows(sessionmaker, role: str = "fact") -> list[VaultFile]:
    async with sessionmaker() as session:
        return list(
            (await session.execute(select(VaultFile).where(VaultFile.role == role).order_by(VaultFile.id)))
            .scalars()
        )


def _path(memory_id: int) -> str:
    return f"Anchor/Memory/{memory_id:04d}-{EPOCH}.md"


# --- bootstrap, idempotence, the cap -----------------------------------------


async def test_bootstrap_is_paced_by_the_write_cap(sessionmaker, vault, clock):
    await _seed(sessionmaker)
    ids = [await _fact(sessionmaker, t) for t in ("Любит кофе", "Живёт в Лилле", "Бегает по утрам")]
    first = await _pass(sessionmaker, vault, clock, _settings(cap=2))
    assert first.created == 2
    assert sorted(vault.files) == [_path(ids[0]), _path(ids[1])]
    second = await _pass(sessionmaker, vault, clock, _settings(cap=2))
    assert second.created == 1
    assert sorted(vault.files) == [_path(i) for i in ids]
    meta = frontmatter.load(vault.files[_path(ids[1])])
    assert (meta["anchor_id"], meta["fact"], meta["anchor_epoch"]) == (ids[1], "Живёт в Лилле", EPOCH)
    rows = await _rows(sessionmaker)
    assert all(r.disk_sha256 == sha(vault.files[r.path]) and r.render_digest for r in rows)


async def test_an_unchanged_database_writes_nothing(sessionmaker, vault, clock):
    await _seed(sessionmaker)
    fact_id = await _fact(sessionmaker, "Любит кофе")
    await _pass(sessionmaker, vault, clock)
    async with sessionmaker() as session:
        await session.execute(
            update(Memory).where(Memory.id == fact_id).values(use_count=7, last_used_at=NOW)
        )
        await session.commit()
    vault.calls.clear()
    result = await _pass(sessionmaker, vault, clock)
    assert result.writes == 0
    assert vault.writes() == []


# --- database changes reach the file -------------------------------------------


async def test_a_correction_updates_the_same_file_with_history(sessionmaker, vault, clock):
    await _seed(sessionmaker)
    old = await _fact(sessionmaker, "Любит работать по вечерам.")
    # Pin the old fact's date to the frozen clock: created_at defaults
    # to the database's now(), and the history line prints its local
    # date, so the test otherwise failed after midnight in Paris.
    async with sessionmaker() as session:
        await session.execute(update(Memory).where(Memory.id == old).values(created_at=NOW))
        await session.commit()
    await _pass(sessionmaker, vault, clock)
    new = await _fact(sessionmaker, "Любит работать по утрам, до 11.", supersedes_id=old)
    result = await _pass(sessionmaker, vault, clock)
    assert result.updated == 1 and result.created == 0
    [row] = await _rows(sessionmaker)
    assert row.memory_id == new and row.path == _path(old)
    content = vault.files[_path(old)]
    assert frontmatter.load(content)["anchor_id"] == new
    assert "## Раньше\n- 2026-09-25 — Любит работать по вечерам.\n" in content


async def test_forget_deletes_the_file(sessionmaker, vault, clock):
    await _seed(sessionmaker)
    fact_id = await _fact(sessionmaker, "Любит кофе")
    await _pass(sessionmaker, vault, clock)
    async with sessionmaker() as session:
        assert await memory.hard_delete(session, fact_id)
    result = await _pass(sessionmaker, vault, clock)
    assert result.deleted == 1
    assert vault.files == {}
    assert await _rows(sessionmaker) == []


async def test_forgetting_a_superseded_id_leaves_the_heads_file_alone(sessionmaker, vault, clock):
    await _seed(sessionmaker)
    old = await _fact(sessionmaker, "Живёт в Лилле")
    new = await _fact(sessionmaker, "Живёт в Руане", supersedes_id=old)
    await _pass(sessionmaker, vault, clock)
    [row] = await _rows(sessionmaker)
    assert row.memory_id == new
    async with sessionmaker() as session:
        assert await memory.hard_delete(session, old)
    vault.calls.clear()
    await _pass(sessionmaker, vault, clock)
    [after] = await _rows(sessionmaker)
    assert after.memory_id == new and after.path == _path(new)
    assert frontmatter.load(vault.files[_path(new)])["fact"] == "Живёт в Руане"
    assert ("delete", after.path) not in vault.calls


# --- the vault's edits are recorded, never applied ---------------------------------


async def test_a_mirror_edit_is_recorded_not_applied_and_extras_survive_a_rewrite(
    sessionmaker, vault, clock
):
    await _seed(sessionmaker)
    fact_id = await _fact(sessionmaker, "Любит кофе")
    await _pass(sessionmaker, vault, clock)
    path = _path(fact_id)
    edited = vault.files[path].replace("fact: Любит кофе", "fact: Любит чай\ntags: [напитки]")
    vault.files[path] = edited

    result = await _pass(sessionmaker, vault, clock)
    assert result.recorded == 1 and result.writes == 0
    assert vault.files[path] == edited
    async with sessionmaker() as session:
        assert (await session.get(Memory, fact_id)).text == "Любит кофе"

    async with sessionmaker() as session:
        await memory.set_pinned(session, fact_id, True)
    result = await _pass(sessionmaker, vault, clock)
    assert result.updated == 1
    meta = frontmatter.load(vault.files[path])
    assert meta["fact"] == "Любит кофе" and meta["pinned"] is True
    assert meta["tags"] == ["напитки"]
    assert "tags: [напитки]\n" in vault.files[path]


async def test_a_412_on_render_skips_and_the_next_pass_records_first(sessionmaker, vault, clock):
    await _seed(sessionmaker)
    fact_id = await _fact(sessionmaker, "Любит кофе")
    await _pass(sessionmaker, vault, clock)
    path = _path(fact_id)
    async with sessionmaker() as session:
        await memory.set_pinned(session, fact_id, True)

    def phone_edit(target: str) -> None:
        vault.files[target] = vault.files[target] + "\nМоя приписка.\n"
        vault.before_put = None

    vault.before_put = phone_edit
    result = await _pass(sessionmaker, vault, clock)
    assert result.updated == 0 and result.skipped == 1
    assert vault.files[path].endswith("Моя приписка.\n")

    result = await _pass(sessionmaker, vault, clock)
    assert result.recorded == 1 and result.updated == 1
    assert frontmatter.load(vault.files[path])["pinned"] is True


async def test_unreadable_properties_quarantine_until_the_file_changes(sessionmaker, vault, clock):
    await _seed(sessionmaker)
    fact_id = await _fact(sessionmaker, "Любит кофе")
    await _pass(sessionmaker, vault, clock)
    path = _path(fact_id)
    merged = vault.files[path].replace("fact: Любит кофе", "fact: Любит кофе\nfact: Любит чай")
    vault.files[path] = merged
    async with sessionmaker() as session:
        await memory.set_pinned(session, fact_id, True)

    result = await _pass(sessionmaker, vault, clock)
    assert result.quarantined == 1
    assert vault.files[path] == merged
    [row] = await _rows(sessionmaker)
    assert (row.state, row.reason) == ("quarantined", "bad_yaml")

    vault.files[path] = merged.replace("fact: Любит чай\n", "")
    result = await _pass(sessionmaker, vault, clock)
    assert result.updated == 1
    [row] = await _rows(sessionmaker)
    assert row.state == "ok"
    assert frontmatter.load(vault.files[path])["pinned"] is True


# --- crashes converge ----------------------------------------------------------


async def test_a_crash_before_the_put_leaves_a_row_that_converges(sessionmaker, vault, clock):
    await _seed(sessionmaker)
    fact_id = await _fact(sessionmaker, "Любит кофе")
    vault.crash_before_put = RuntimeError("worker killed")
    with pytest.raises(RuntimeError):
        await _pass(sessionmaker, vault, clock)
    [row] = await _rows(sessionmaker)
    assert row.disk_sha256 is None and vault.files == {}
    await _pass(sessionmaker, vault, clock)
    [row] = await _rows(sessionmaker)
    assert row.state == "ok" and row.disk_sha256 == sha(vault.files[_path(fact_id)])


async def test_a_crash_after_the_put_is_adopted_not_rewritten(sessionmaker, vault, clock):
    await _seed(sessionmaker)
    fact_id = await _fact(sessionmaker, "Любит кофе")
    vault.crash_after_put = RuntimeError("worker killed")
    with pytest.raises(RuntimeError):
        await _pass(sessionmaker, vault, clock)
    assert _path(fact_id) in vault.files
    vault.calls.clear()
    result = await _pass(sessionmaker, vault, clock)
    assert result.writes == 0 and result.quarantined == 0
    [row] = await _rows(sessionmaker)
    assert row.state == "ok" and row.render_digest is not None


# --- epoch orphans ---------------------------------------------------------------


async def test_old_epoch_files_are_deleted_never_imported(sessionmaker, vault, clock):
    await _seed(sessionmaker)
    vault.files["Anchor/Memory/0001-zzzzzz.md"] = "---\nanchor: fact\nanchor_epoch: zzzzzz\nanchor_id: 1\nfact: старое\n---\n"
    vault.files["Anchor/Journal/2026-09-01-zzzzzz.md"] = "---\nanchor: journal\nanchor_epoch: zzzzzz\n---\n"
    vault.files["Anchor/Memory/Утро.md"] = "---\nanchor: fact\nfact: мой файл\n---\n"
    vault.files["Anchor/Memory/0009-abcdef.md"] = "---\nanchor_epoch: abcdef\n---\n"
    result = await _pass(sessionmaker, vault, clock)
    assert result.orphans == 2
    assert sorted(vault.files) == ["Anchor/Memory/0009-abcdef.md", "Anchor/Memory/Утро.md"]
    async with sessionmaker() as session:
        assert (await session.execute(select(func.count()).select_from(Memory))).scalar_one() == 0


# --- when the pass does nothing ---------------------------------------------------


@pytest.mark.parametrize("mode", ["off", "status"])
async def test_off_and_status_never_touch_the_vault(sessionmaker, vault, clock, mode):
    await _seed(sessionmaker)
    await _fact(sessionmaker, "Любит кофе")
    await _pass(sessionmaker, vault, clock, _settings(mode))
    assert vault.calls == []


async def test_sync_mode_acts_as_mirror(sessionmaker, vault, clock):
    await _seed(sessionmaker)
    fact_id = await _fact(sessionmaker, "Любит кофе")
    await _pass(sessionmaker, vault, clock, _settings("sync"))
    path = _path(fact_id)
    vault.files[path] = vault.files[path].replace("Любит кофе", "Любит чай")
    await _pass(sessionmaker, vault, clock, _settings("sync"))
    async with sessionmaker() as session:
        assert (await session.get(Memory, fact_id)).text == "Любит кофе"


async def test_nothing_happens_while_a_purge_is_pending(sessionmaker, vault, clock):
    await _seed(sessionmaker)
    await _fact(sessionmaker, "Любит кофе")
    async with sessionmaker() as session:
        await enqueue_job(session, VAULT_PURGE, {}, dedup_key="vault_purge")
    await _pass(sessionmaker, vault, clock)
    assert vault.calls == []


async def test_an_unreachable_vault_is_recorded_and_the_pass_ends_normally(sessionmaker, vault, clock):
    await _seed(sessionmaker)
    vault.down = True
    result = await _pass(sessionmaker, vault, clock)
    assert result.unavailable
    async with sessionmaker() as session:
        status = await session.get(VaultStatus, 1)
    assert status.last_unavailable_at == NOW


async def test_done_passes_older_than_an_hour_are_pruned(sessionmaker, vault, clock):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        session.add_all(
            [
                Job(kind=VAULT_SYNC, payload={}, dedup_key="vault_sync:old", status="done",
                    created_at=NOW - datetime.timedelta(hours=2)),
                Job(kind=VAULT_SYNC, payload={}, dedup_key="vault_sync:recent", status="done",
                    created_at=NOW - datetime.timedelta(minutes=10)),
                Job(kind="extract", payload={}, dedup_key="extract:1", status="done",
                    created_at=NOW - datetime.timedelta(hours=5)),
            ]
        )
        await session.commit()
    await _pass(sessionmaker, vault, clock)
    async with sessionmaker() as session:
        keys = set((await session.execute(select(Job.dedup_key))).scalars())
    assert keys == {"vault_sync:recent", "extract:1"}


@pytest.mark.parametrize("mode,queued", [("off", False), ("status", False), ("mirror", True), ("sync", True)])
async def test_a_pass_is_queued_once_a_minute_in_mirror_and_sync(sessionmaker, clock, mode, queued):
    async with sessionmaker() as session:
        assert await maybe_enqueue_vault_sync(session, _settings(mode), clock) is queued
        assert await maybe_enqueue_vault_sync(session, _settings(mode), clock) is False
        clock.advance(datetime.timedelta(minutes=1))
        assert await maybe_enqueue_vault_sync(session, _settings(mode), clock) is queued


# --- techniques ---------------------------------------------------------------


async def test_a_technique_quotes_its_card_even_after_a_correction(sessionmaker, vault, clock):
    await _seed(sessionmaker)
    adopted = await _fact(sessionmaker, "Ложиться в одно и то же время.", kind="technique")
    async with sessionmaker() as session:
        job = StudyJob(kind="read", local_date=TODAY, status="done")
        session.add(job)
        await session.flush()
        clip = StudyClip(job_id=job.id, url="https://example.com/sleep", domain="example.com",
                         title="t", text="x", http_status=200)
        session.add(clip)
        await session.flush()
        session.add(StudyCard(job_id=job.id, clip_id=clip.id, kind="technique",
                              text="Ложиться в одно и то же время.",
                              quote="Ложитесь спать в одно и то же время каждый день.",
                              source_url="https://example.com/sleep", risk_model="low",
                              risk_rules="low", risk_final="low", status="adopted",
                              memory_id=adopted))
        await session.commit()
    head = await _fact(sessionmaker, "Ложиться и вставать в одно время.", kind="technique",
                       supersedes_id=adopted)
    await _pass(sessionmaker, vault, clock)
    # First rendered after the correction, so the file carries the head's id.
    content = vault.files[_path(head)]
    assert frontmatter.load(content)["anchor_id"] == head
    assert "Источник: example.com" in content
    assert "> Ложитесь спать в одно и то же время каждый день." in content


# --- the journal ----------------------------------------------------------------


async def test_today_is_rendered_and_a_hand_edited_day_is_never_rewritten(sessionmaker, vault, clock):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        session.add(Journal(local_date=TODAY, text="Поговорили про отчёт."))
        session.add(Checkin(local_date=TODAY, day_rating=4, due_result="done", note="устал"))
        await session.commit()
    await _pass(sessionmaker, vault, clock)
    path = f"Anchor/Journal/2026-09-25-{EPOCH}.md"
    content = vault.files[path]
    assert "- Оценка дня: 4/5\n- Главное действие: сделано\n- Заметка: устал\n" in content
    assert "## Журнал\n- Поговорили про отчёт.\n" in content

    vault.files[path] = content + "\nМои мысли.\n"
    async with sessionmaker() as session:
        session.add(Journal(local_date=TODAY, text="Вечером гуляли."))
        await session.commit()
    await _pass(sessionmaker, vault, clock)
    [row] = await _rows(sessionmaker, "journal")
    assert row.state == "diverged"
    assert vault.files[path].endswith("Мои мысли.\n")
    await _pass(sessionmaker, vault, clock)
    assert "Вечером гуляли" not in vault.files[path]


async def test_past_days_are_backfilled_and_empty_days_skipped(sessionmaker, vault, clock):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        session.add(Journal(local_date=datetime.date(2026, 9, 1), text="Давний день."))
        await session.commit()
    await _pass(sessionmaker, vault, clock)
    assert sorted(vault.files) == [f"Anchor/Journal/2026-09-01-{EPOCH}.md"]


async def test_nothing_from_a_welfare_exchange_is_ever_rendered(sessionmaker, vault, clock):
    """Plan section 10. A check-in note that tripped the welfare check
    keeps its text in checkin.note; the day file must not carry it."""
    await _seed(sessionmaker)
    distress = "мне очень плохо, не знаю зачем всё это"
    async with sessionmaker() as session:
        session.add(Checkin(local_date=TODAY, day_rating=1, due_result="no", note=distress))
        session.add(Message(role="user", content=distress, ooc=True, kind="welfare"))
        session.add(Journal(local_date=TODAY, text="Короткий разговор."))
        await session.commit()
        await session.execute(update(Message).values(created_at=NOW))
        await session.commit()
    await _pass(sessionmaker, vault, clock)
    for content in vault.files.values():
        assert distress not in content
    content = vault.files[f"Anchor/Journal/2026-09-25-{EPOCH}.md"]
    assert "- Оценка дня: 1/5" in content
    assert "Заметка" not in content


async def test_a_note_on_an_ordinary_day_is_rendered(sessionmaker, vault, clock):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        session.add(Checkin(local_date=TODAY - datetime.timedelta(days=3), day_rating=5, note="отлично"))
        session.add(Message(role="user", content="x", ooc=True, kind="welfare"))
        await session.commit()
        await session.execute(update(Message).values(created_at=NOW))
        await session.commit()
    await _pass(sessionmaker, vault, clock)
    [content] = vault.files.values()
    assert "- Заметка: отлично" in content


# --- writers that bypass write_memory (phase 6's idle consolidation) --------


async def test_a_consolidation_merge_keeps_one_file_on_the_new_head(sessionmaker, vault, clock):
    """Idle consolidation (app/core/idle/consolidate.py) inserts the merged
    fact and supersedes the originals directly, not through write_memory,
    so vault_file.memory_id is not moved for it. The pass follows the
    chain itself: the oldest file keeps the head, the other is deleted."""
    await _seed(sessionmaker)
    first = await _fact(sessionmaker, "Любит кофе по утрам")
    second = await _fact(sessionmaker, "Пьёт эспрессо после обеда")
    await _pass(sessionmaker, vault, clock)
    assert sorted(vault.files) == [_path(first), _path(second)]

    async with sessionmaker() as session:
        merged = Memory(kind="preference", text="Пьёт кофе утром и после обеда", source="consolidate")
        session.add(merged)
        await session.flush()
        for original in (first, second):
            (await session.get(Memory, original)).superseded_by = merged.id
        await session.commit()
        merged_id = merged.id

    result = await _pass(sessionmaker, vault, clock)
    assert result.deleted == 1 and result.updated == 1
    assert list(vault.files) == [_path(first)]
    meta = frontmatter.load(vault.files[_path(first)])
    assert (meta["anchor_id"], meta["fact"]) == (merged_id, "Пьёт кофе утром и после обеда")
    [row] = await _rows(sessionmaker)
    assert row.memory_id == merged_id

    # Undo (app/core/idle/undo.py) deletes the merged row and reactivates
    # the originals, again directly: the merged file goes, both come back.
    async with sessionmaker() as session:
        for original in (first, second):
            (await session.get(Memory, original)).superseded_by = None
        await session.flush()
        await session.delete(await session.get(Memory, merged_id))
        await session.commit()
    await _pass(sessionmaker, vault, clock)
    texts = sorted(frontmatter.load(content)["fact"] for content in vault.files.values())
    assert texts == ["Любит кофе по утрам", "Пьёт эспрессо после обеда"]
    assert len(await _rows(sessionmaker)) == 2

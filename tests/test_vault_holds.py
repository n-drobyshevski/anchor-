"""Vault holds (phase-8 plan section 8): confirm, revert, stale, expiry.

Mostly unit-level against `app/vault/holds.py` directly, plus a couple
of full-pass tests for expiry and for a rule-confirm whose head moved.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core import memory
from app.core.clock import FrozenClock
from app.db.models import Memory, UserState, VaultFile, VaultHold
from app.vault import holds, limits
from app.vault.sync import run_vault_sync
from vault_fake import FakeVault

EPOCH = "abcdef"
TOKEN = "vault-token-" + "v" * 32
NOW = datetime.datetime(2026, 9, 25, 10, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def vault() -> FakeVault:
    return FakeVault()


def _settings(cap: int = 50) -> Settings:
    return Settings(VAULT_MODE="sync", VAULT_API_TOKEN=TOKEN, VAULT_MAX_WRITES_PER_PASS=cap)


async def _seed(sessionmaker, epoch: str = EPOCH) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, timezone="Europe/Paris", vault_epoch=epoch))
        await session.commit()


async def _pass(sessionmaker, vault, clock):
    async with sessionmaker() as session:
        return await run_vault_sync(session, _settings(), clock, vault)


# --- confirm / revert / stale press / replay --------------------------------------


async def test_confirming_a_new_rule_writes_the_memory_and_unheld_the_row(sessionmaker, clock):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        row = VaultFile(path="Anchor/Memory/Правило.md", role="fact")
        session.add(row)
        await session.flush()
        hold = await holds.open_rule_hold(
            session, file_id=row.id, kind="rule", text="Не звонить после десяти.", supersedes_id=None, clock=clock
        )
        session.add(hold)
        await session.commit()
        hold_id, row_id = hold.id, row.id

    async with sessionmaker() as session:
        result = await holds.decide(session, hold_id, EPOCH, True, clock)
    assert result.outcome == holds.CONFIRMED_RESULT

    async with sessionmaker() as session:
        row = await session.get(VaultFile, row_id)
        assert row.state == "ok" and row.hold_id is None
        head = (await session.execute(select(Memory))).scalar_one()
    assert head.kind == "rule" and head.text == "Не звонить после десяти."


async def test_reverting_a_new_rule_leaves_no_memory_and_the_file_is_cleaned_up(
    sessionmaker, vault, clock
):
    await _seed(sessionmaker)
    vault.files["Anchor/Memory/Правило.md"] = (
        "---\nanchor: fact\nkind: rule\npinned: false\nfact: Не звонить после десяти.\n---\n"
    )
    result = await _pass(sessionmaker, vault, clock)
    [hold_id] = result.new_hold_ids

    async with sessionmaker() as session:
        decided = await holds.decide(session, hold_id, EPOCH, False, clock)
    assert decided.outcome == holds.REVERTED_RESULT
    await _pass(sessionmaker, vault, clock)
    assert "Anchor/Memory/Правило.md" not in vault.files
    async with sessionmaker() as session:
        assert (await session.execute(select(VaultFile))).first() is None
        assert (await session.execute(select(Memory))).first() is None


async def test_confirming_a_rule_edit_supersedes_the_head(sessionmaker, clock):
    async with sessionmaker() as session:
        old = await memory.write_memory(session, kind="rule", text="Не звонить после десяти.", source="user")
        old_id = old.id
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        row = VaultFile(path="Anchor/Memory/0001-abcdef.md", role="fact", memory_id=old_id)
        session.add(row)
        await session.flush()
        hold = await holds.open_rule_hold(
            session, file_id=row.id, kind="rule", text="Не звонить после девяти.", supersedes_id=old_id, clock=clock
        )
        session.add(hold)
        await session.commit()
        hold_id, row_id = hold.id, row.id

    async with sessionmaker() as session:
        result = await holds.decide(session, hold_id, EPOCH, True, clock)
    assert result.outcome == holds.CONFIRMED_RESULT
    async with sessionmaker() as session:
        head = (await session.execute(select(Memory).where(Memory.superseded_by.is_(None)))).scalar_one()
        row = await session.get(VaultFile, row_id)
    assert head.text == "Не звонить после девяти." and row.memory_id == head.id
    assert row.state == "ok" and row.hold_id is None


async def test_reverting_a_rule_edit_clears_render_digest(sessionmaker, clock):
    async with sessionmaker() as session:
        old = await memory.write_memory(session, kind="rule", text="Не звонить после десяти.", source="user")
        old_id = old.id
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        row = VaultFile(path="Anchor/Memory/0001-abcdef.md", role="fact", memory_id=old_id, render_digest="stale")
        session.add(row)
        await session.flush()
        hold = await holds.open_rule_hold(
            session, file_id=row.id, kind="rule", text="Не звонить после девяти.", supersedes_id=old_id, clock=clock
        )
        session.add(hold)
        await session.commit()
        hold_id, row_id = hold.id, row.id

    async with sessionmaker() as session:
        result = await holds.decide(session, hold_id, EPOCH, False, clock)
    assert result.outcome == holds.REVERTED_RESULT
    async with sessionmaker() as session:
        row = await session.get(VaultFile, row_id)
        assert (await session.get(Memory, old_id)).text == "Не звонить после десяти."
    assert row.render_digest is None and row.state == "ok"


async def test_a_stale_press_answers_unknown_id_not_pending_and_wrong_epoch(sessionmaker, clock):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        assert (await holds.decide(session, 999, EPOCH, True, clock)).outcome == holds.STALE_PRESS

    async with sessionmaker() as session:
        row = VaultFile(path="Anchor/Memory/Правило.md", role="fact")
        session.add(row)
        await session.flush()
        hold = await holds.open_rule_hold(
            session, file_id=row.id, kind="rule", text="x", supersedes_id=None, clock=clock
        )
        session.add(hold)
        await session.commit()
        hold_id = hold.id

    # Wrong epoch: a button left over from before /delete.
    async with sessionmaker() as session:
        assert (await holds.decide(session, hold_id, "zzzzzz", True, clock)).outcome == holds.STALE_PRESS
    async with sessionmaker() as session:
        assert (await session.get(VaultHold, hold_id)).status == "pending"

    # A genuine decision, then a replay of the same press.
    async with sessionmaker() as session:
        assert (await holds.decide(session, hold_id, EPOCH, True, clock)).outcome == holds.CONFIRMED_RESULT
    async with sessionmaker() as session:
        assert (await holds.decide(session, hold_id, EPOCH, True, clock)).outcome == holds.STALE_PRESS
    async with sessionmaker() as session:
        assert (await holds.decide(session, hold_id, EPOCH, False, clock)).outcome == holds.STALE_PRESS


async def test_a_rule_confirm_whose_head_moved_becomes_stale(sessionmaker, clock):
    async with sessionmaker() as session:
        old = await memory.write_memory(session, kind="rule", text="Не звонить после десяти.", source="user")
        old_id = old.id
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        row = VaultFile(path="Anchor/Memory/0001-abcdef.md", role="fact", memory_id=old_id)
        session.add(row)
        await session.flush()
        hold = await holds.open_rule_hold(
            session, file_id=row.id, kind="rule", text="Не звонить после девяти.", supersedes_id=old_id, clock=clock
        )
        row.state, row.hold_id = "held", hold.id
        await session.commit()
        hold_id, row_id = hold.id, row.id

    # Chat corrects the same rule while the hold is pending.
    async with sessionmaker() as session:
        await memory.write_memory(
            session, kind="rule", text="Не звонить вообще.", source="user", supersedes_id=old_id
        )

    async with sessionmaker() as session:
        result = await holds.decide(session, hold_id, EPOCH, True, clock)
    assert result.outcome == holds.STALE_APPLY
    async with sessionmaker() as session:
        hold = await session.get(VaultHold, hold_id)
        row = await session.get(VaultFile, row_id)
        head = (await session.execute(select(Memory).where(Memory.superseded_by.is_(None)))).scalar_one()
    assert hold.status == "stale"
    assert head.text == "Не звонить вообще."
    assert row.hold_id is None


# --- expiry -------------------------------------------------------------------


async def test_expiry_reverts_a_rule_edit(sessionmaker, clock):
    async with sessionmaker() as session:
        old = await memory.write_memory(session, kind="rule", text="Не звонить после десяти.", source="user")
        old_id = old.id
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        row = VaultFile(path="Anchor/Memory/0001-abcdef.md", role="fact", memory_id=old_id, render_digest="stale")
        session.add(row)
        await session.flush()
        hold = await holds.open_rule_hold(
            session, file_id=row.id, kind="rule", text="Не звонить после девяти.", supersedes_id=old_id, clock=clock
        )
        session.add(hold)
        await session.commit()
        hold_id, row_id = hold.id, row.id

    clock.advance(datetime.timedelta(days=limits.HOLD_TTL_DAYS + 1))
    async with sessionmaker() as session:
        expired = await holds.expire_holds(session, clock)
    assert expired == [hold_id]
    async with sessionmaker() as session:
        hold = await session.get(VaultHold, hold_id)
        row = await session.get(VaultFile, row_id)
        assert (await session.get(Memory, old_id)).text == "Не звонить после десяти."
    assert hold.status == "expired" and hold.decided_at == clock.now_utc()
    assert row.render_digest is None


async def test_a_hold_within_the_ttl_does_not_expire(sessionmaker, clock):
    async with sessionmaker() as session:
        row = VaultFile(path="Anchor/Memory/Правило.md", role="fact")
        session.add(row)
        await session.flush()
        hold = await holds.open_rule_hold(
            session, file_id=row.id, kind="rule", text="x", supersedes_id=None, clock=clock
        )
        session.add(hold)
        await session.commit()
        hold_id = hold.id
    clock.advance(datetime.timedelta(days=limits.HOLD_TTL_DAYS - 1))
    async with sessionmaker() as session:
        assert await holds.expire_holds(session, clock) == []
        assert (await session.get(VaultHold, hold_id)).status == "pending"


# --- pending_unsent / mark_sent -------------------------------------------------


async def test_pending_unsent_and_mark_sent(sessionmaker, clock):
    async with sessionmaker() as session:
        row = VaultFile(path="Anchor/Memory/Правило.md", role="fact")
        session.add(row)
        await session.flush()
        hold = await holds.open_rule_hold(
            session, file_id=row.id, kind="rule", text="x", supersedes_id=None, clock=clock
        )
        session.add(hold)
        await session.commit()
        hold_id = hold.id

    async with sessionmaker() as session:
        pending = await holds.pending_unsent(session)
    assert [h.id for h in pending] == [hold_id]

    async with sessionmaker() as session:
        await holds.mark_sent(session, hold_id, 4242)
    async with sessionmaker() as session:
        assert await holds.pending_unsent(session) == []
        assert (await session.get(VaultHold, hold_id)).tg_message_id == 4242


# --- bug fixes (review of phase B) ------------------------------------------


async def test_confirming_a_rule_edit_that_duplicates_another_fact_is_quarantined_not_lost(
    sessionmaker, clock
):
    """Bug 4: write_memory returning None (near-duplicate) used to leave
    the row `ok` with the old text silently kept. Nothing may vanish
    without a trace, so it must quarantine instead."""
    async with sessionmaker() as session:
        old = await memory.write_memory(session, kind="rule", text="Не звонить после десяти.", source="user")
        old_id = old.id
        await memory.write_memory(session, kind="rule", text="Совсем не звонить.", source="user")
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        row = VaultFile(path="Anchor/Memory/0001-abcdef.md", role="fact", memory_id=old_id)
        session.add(row)
        await session.flush()
        hold = await holds.open_rule_hold(
            session, file_id=row.id, kind="rule", text="Совсем не звонить.", supersedes_id=old_id, clock=clock
        )
        session.add(hold)
        await session.commit()
        hold_id, row_id = hold.id, row.id

    async with sessionmaker() as session:
        result = await holds.decide(session, hold_id, EPOCH, True, clock)
    assert result.outcome == holds.DUPLICATE_RESULT
    async with sessionmaker() as session:
        hold = await session.get(VaultHold, hold_id)
        row = await session.get(VaultFile, row_id)
        untouched = await session.get(Memory, old_id)
    assert hold.status == "confirmed"  # the yes was real; only the write was refused
    assert row.state == "quarantined" and row.reason == "duplicate_fact"
    assert untouched.text == "Не звонить после десяти."  # nothing deleted, nothing changed


async def test_confirming_a_new_rule_that_duplicates_another_fact_deletes_nothing(sessionmaker, clock):
    async with sessionmaker() as session:
        await memory.write_memory(session, kind="rule", text="Не звонить после десяти.", source="user")
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        row = VaultFile(path="Anchor/Memory/Правило.md", role="fact")
        session.add(row)
        await session.flush()
        hold = await holds.open_rule_hold(
            session, file_id=row.id, kind="rule", text="Не звонить после десяти.", supersedes_id=None, clock=clock
        )
        session.add(hold)
        await session.commit()
        hold_id, row_id = hold.id, row.id

    async with sessionmaker() as session:
        result = await holds.decide(session, hold_id, EPOCH, True, clock)
    assert result.outcome == holds.DUPLICATE_RESULT
    async with sessionmaker() as session:
        row = await session.get(VaultFile, row_id)
        count = (await session.execute(select(Memory))).scalars().all()
    assert row is not None and row.state == "quarantined" and row.reason == "duplicate_fact"
    assert len(count) == 1  # only the pre-existing rule; nothing new, nothing lost


async def test_expiry_does_not_depend_on_the_real_wall_clock(sessionmaker):
    """Bug 5: VaultHold.created_at used to come from the DB server's
    now(), which a FrozenClock far from real wall-clock time could never
    satisfy. open_rule_hold now stamps created_at from the same Clock
    expire_holds compares against."""
    far_past = FrozenClock(datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc))
    async with sessionmaker() as session:
        row = VaultFile(path="Anchor/Memory/Правило.md", role="fact")
        session.add(row)
        await session.flush()
        hold = await holds.open_rule_hold(
            session, file_id=row.id, kind="rule", text="x", supersedes_id=None, clock=far_past
        )
        session.add(hold)
        await session.commit()
        hold_id = hold.id

    far_past.advance(datetime.timedelta(days=limits.HOLD_TTL_DAYS + 1))
    async with sessionmaker() as session:
        expired = await holds.expire_holds(session, far_past)
    assert expired == [hold_id]
    async with sessionmaker() as session:
        hold = await session.get(VaultHold, hold_id)
    assert hold.status == "expired"
    assert hold.decided_at == far_past.now_utc()

"""The vault tables refuse, in SQL, every row plan section 6 says cannot exist.

Each CHECK gets a row that violates it and nothing else, so a failure
here names exactly which invariant stopped holding.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core import purge
from app.config import Settings
from app.core.clock import SystemClock
from app.db.models import Memory, UserState, VaultChunk, VaultFile, VaultHold, VaultStatus
from app.vault.epoch import EPOCH_RE, new_epoch

TODAY = datetime.date(2026, 9, 25)


async def _insert_fails(sessionmaker, row, constraint: str) -> None:
    async with sessionmaker() as session:
        session.add(row)
        with pytest.raises(IntegrityError, match=constraint):
            await session.commit()


async def _hold(sessionmaker) -> int:
    async with sessionmaker() as session:
        hold = VaultHold(kind="mass_delete", payload={"file_ids": []})
        session.add(hold)
        await session.commit()
        return hold.id


async def test_valid_rows_are_accepted(sessionmaker):
    hold_id = await _hold(sessionmaker)
    async with sessionmaker() as session:
        memory = Memory(kind="identity", text="факт", source="user")
        session.add(memory)
        await session.flush()
        session.add_all(
            [
                VaultFile(path="Anchor/Memory/0001-abcdef.md", role="fact", memory_id=memory.id),
                VaultFile(path="Anchor/Memory/Утро.md", role="fact", state="held", hold_id=hold_id),
                VaultFile(path="Anchor/Journal/2026-09-25-abcdef.md", role="journal", local_date=TODAY),
                VaultFile(path="Notes/Бег.md", role="note", state="quarantined", reason="too_long"),
                VaultHold(
                    kind="rule",
                    payload={"file_id": 1, "kind": "rule", "text": "x", "supersedes_id": None},
                ),
                VaultStatus(id=1, forgets_window=["2026-09-25T10:00:00+00:00"]),
            ]
        )
        await session.commit()


@pytest.mark.parametrize(
    "row,constraint",
    [
        (dict(role="fact", local_date=TODAY), "ck_vault_file_role_columns"),
        (dict(role="journal"), "ck_vault_file_role_columns"),
        (dict(role="note", local_date=TODAY), "ck_vault_file_role_columns"),
        (dict(role="picture"), "ck_vault_file_role"),
        (dict(role="note", state="broken"), "ck_vault_file_state"),
        (dict(role="note", state="held"), "ck_vault_file_held_has_hold"),
        (dict(role="note", reason="Слишком длинный факт"), "ck_vault_file_reason_code"),
        (dict(role="note", reason="too long"), "ck_vault_file_reason_code"),
    ],
)
async def test_vault_file_checks(sessionmaker, row, constraint):
    await _insert_fails(sessionmaker, VaultFile(path="Notes/x.md", **row), constraint)


async def test_a_journal_row_cannot_point_at_a_memory(sessionmaker):
    async with sessionmaker() as session:
        memory = Memory(kind="identity", text="факт", source="user")
        session.add(memory)
        await session.commit()
        memory_id = memory.id
    await _insert_fails(
        sessionmaker,
        VaultFile(path="Anchor/Journal/x.md", role="journal", local_date=TODAY, memory_id=memory_id),
        "ck_vault_file_role_columns",
    )


async def test_a_hold_without_held_state_is_refused(sessionmaker):
    hold_id = await _hold(sessionmaker)
    await _insert_fails(
        sessionmaker, VaultFile(path="Notes/x.md", role="note", hold_id=hold_id), "ck_vault_file_held_has_hold"
    )


@pytest.mark.parametrize("path", ["", "/etc/passwd", "Anchor\\Memory\\x.md"])
async def test_vault_file_paths_are_vault_relative(sessionmaker, path):
    await _insert_fails(sessionmaker, VaultFile(path=path, role="note"), "ck_vault_file_path_relative")


@pytest.mark.parametrize(
    "kind,payload,constraint",
    [
        # An array holding the rule's key names satisfies `?&`; only the
        # object check stops it.
        ("rule", ["file_id", "kind", "text", "supersedes_id"], "ck_vault_hold_payload_object"),
        ("mass_delete", {"file_id": 1}, "ck_vault_hold_mass_delete_payload"),
        ("rule", {"file_id": 1, "text": "x"}, "ck_vault_hold_rule_payload"),
        ("forget", {"file_ids": []}, "ck_vault_hold_kind"),
    ],
)
async def test_vault_hold_checks(sessionmaker, kind, payload, constraint):
    await _insert_fails(sessionmaker, VaultHold(kind=kind, payload=payload), constraint)


async def test_vault_hold_status_check(sessionmaker):
    await _insert_fails(
        sessionmaker,
        VaultHold(kind="mass_delete", payload={"file_ids": []}, status="maybe"),
        "ck_vault_hold_status",
    )


async def test_vault_status_is_a_singleton_holding_an_array(sessionmaker):
    await _insert_fails(sessionmaker, VaultStatus(id=2), "ck_vault_status_singleton")
    await _insert_fails(sessionmaker, VaultStatus(id=1, forgets_window={"a": 1}), "ck_vault_status_forgets_array")


async def test_vault_chunk_lengths_and_generated_tsvector(sessionmaker):
    async with sessionmaker() as session:
        note = VaultFile(path="Бег.md", role="note")
        session.add(note)
        await session.commit()
        note_id = note.id
    await _insert_fails(sessionmaker, VaultChunk(file_id=note_id, ord=0, text="x" * 1201), "ck_vault_chunk_text_length")
    await _insert_fails(
        sessionmaker, VaultChunk(file_id=note_id, ord=0, heading="x" * 201, text="x"), "ck_vault_chunk_heading_length"
    )
    async with sessionmaker() as session:
        session.add(VaultChunk(file_id=note_id, ord=0, heading="Бег", text="Бегаю по утрам в парке."))
        await session.commit()
        matched = (
            await session.execute(
                text("select count(*) from vault_chunk where tsv @@ to_tsquery('russian', 'парк')")
            )
        ).scalar_one()
    assert matched == 1


async def test_deleting_a_file_deletes_its_chunks(sessionmaker):
    async with sessionmaker() as session:
        note = VaultFile(path="Бег.md", role="note")
        session.add(note)
        await session.flush()
        session.add(VaultChunk(file_id=note.id, ord=0, text="x"))
        await session.commit()
        await session.delete(note)
        await session.commit()
        assert (await session.execute(text("select count(*) from vault_chunk"))).scalar_one() == 0


# --- the epoch ---


def test_new_epochs_have_the_right_shape_and_vary():
    epochs = {new_epoch() for _ in range(200)}
    assert all(EPOCH_RE.match(e) for e in epochs)
    assert len(epochs) > 190


async def test_user_state_gets_an_epoch_and_refuses_a_bad_one(sessionmaker):
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=1))
        await session.commit()
        state = await session.get(UserState, 1)
        assert EPOCH_RE.match(state.vault_epoch)
    async with sessionmaker() as session:
        with pytest.raises(IntegrityError, match="ck_user_state_vault_epoch"):
            await session.execute(text("update user_state set vault_epoch = 'ABC123'"))
            await session.commit()


def test_delete_resets_the_epoch():
    """reset_values draws a fresh epoch each time (plan section 6)."""
    values = purge.reset_values(Settings(), SystemClock())
    assert EPOCH_RE.match(values["vault_epoch"])
    again = {purge.reset_values(Settings(), SystemClock())["vault_epoch"] for _ in range(20)}
    assert len(again) > 15

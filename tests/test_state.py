"""core/state.py tests (plan section 5 / 16).

- reading the singleton row
- update_state() writes a state_change row with correct old/new/source
- the id=1 and intensity-between-1-and-5 constraints actually reject
  bad values
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.state import get_state, update_state
from app.db.models import StateChange, UserState

CHAT_ID = 4242


async def _seed_state(sessionmaker) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID))
        await session.commit()


async def test_get_state_reads_the_singleton_row(sessionmaker):
    await _seed_state(sessionmaker)

    async with sessionmaker() as session:
        state = await get_state(session)

    assert state.id == 1
    assert state.chat_id == CHAT_ID
    assert state.persona_active is True
    assert state.intensity == 3
    assert state.timezone == "Europe/Paris"


async def test_update_state_writes_audit_row_with_old_and_new(sessionmaker):
    await _seed_state(sessionmaker)

    async with sessionmaker() as session:
        updated = await update_state(session, "intensity", 2, "command")
    assert updated.intensity == 2

    async with sessionmaker() as session:
        result = await session.execute(select(StateChange))
        rows = result.scalars().all()

    assert len(rows) == 1
    row = rows[0]
    assert row.field == "intensity"
    assert row.old_value == "3"
    assert row.new_value == "2"
    assert row.source == "command"


async def test_update_state_persists_across_reads(sessionmaker):
    await _seed_state(sessionmaker)

    async with sessionmaker() as session:
        await update_state(session, "persona_active", False, "pause")

    async with sessionmaker() as session:
        state = await get_state(session)
    assert state.persona_active is False


async def test_user_state_rejects_non_singleton_id(sessionmaker):
    async with sessionmaker() as session:
        session.add(UserState(id=2, chat_id=CHAT_ID))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_user_state_rejects_intensity_out_of_range(sessionmaker):
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, intensity=6))
        with pytest.raises(IntegrityError):
            await session.commit()

    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, intensity=0))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_user_state_table_never_has_more_than_one_row(sessionmaker):
    await _seed_state(sessionmaker)

    async with sessionmaker() as session:
        result = await session.execute(select(func.count()).select_from(UserState))
        assert result.scalar_one() == 1

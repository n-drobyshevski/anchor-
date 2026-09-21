"""Queue tests (plan section 16 / 6.3).

- duplicate update_id inserts exactly once
- two concurrent claim() calls: exactly one wins, with attempts == 1
- a stuck `processing` row (locked_at 6 min old) returns to pending
- three failures land in `failed`; pending after the first two
"""

from __future__ import annotations

import asyncio
import datetime

from sqlalchemy import update as sql_update

from app.db.models import TelegramUpdate
from app.db.queue import claim, complete, enqueue, fail, recover_stuck


async def test_duplicate_update_id_inserts_once(sessionmaker):
    async with sessionmaker() as session:
        first = await enqueue(session, 100, {"update_id": 100})
        second = await enqueue(session, 100, {"update_id": 100})
    assert first is True
    assert second is False


async def test_concurrent_claim_only_one_wins(sessionmaker):
    async with sessionmaker() as session:
        await enqueue(session, 101, {"update_id": 101})

    async def do_claim():
        async with sessionmaker() as session:
            return await claim(session)

    results = await asyncio.gather(do_claim(), do_claim())
    winners = [r for r in results if r is not None]
    losers = [r for r in results if r is None]

    assert len(winners) == 1
    assert len(losers) == 1
    assert winners[0].attempts == 1
    assert winners[0].update_id == 101


async def test_stuck_processing_row_recovers_to_pending(sessionmaker):
    async with sessionmaker() as session:
        await enqueue(session, 102, {"update_id": 102})
        row = await claim(session)
        assert row.status == "processing"

        # Simulate a crash: push locked_at back beyond the 5-minute window.
        stale = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=6)
        await session.execute(
            sql_update(TelegramUpdate)
            .where(TelegramUpdate.update_id == 102)
            .values(locked_at=stale)
        )
        await session.commit()

    async with sessionmaker() as session:
        recovered = await recover_stuck(session)
        assert recovered == 1

    async with sessionmaker() as session:
        refreshed = await session.get(TelegramUpdate, 102)
        assert refreshed.status == "pending"


async def test_recover_stuck_leaves_fresh_processing_rows_alone(sessionmaker):
    async with sessionmaker() as session:
        await enqueue(session, 103, {"update_id": 103})
        await claim(session)

    async with sessionmaker() as session:
        recovered = await recover_stuck(session)
        assert recovered == 0

    async with sessionmaker() as session:
        refreshed = await session.get(TelegramUpdate, 103)
        assert refreshed.status == "processing"


async def test_three_failures_marks_failed(sessionmaker):
    async with sessionmaker() as session:
        await enqueue(session, 104, {"update_id": 104})

    for attempt in range(1, 4):
        async with sessionmaker() as session:
            row = await claim(session)
            assert row.attempts == attempt
            await fail(session, row.update_id, "SomeError")

        async with sessionmaker() as session:
            refreshed = await session.get(TelegramUpdate, 104)
            if attempt < 3:
                assert refreshed.status == "pending"
            else:
                assert refreshed.status == "failed"


async def test_complete_marks_done(sessionmaker):
    async with sessionmaker() as session:
        await enqueue(session, 105, {"update_id": 105})
        row = await claim(session)
        await complete(session, row.update_id)

    async with sessionmaker() as session:
        refreshed = await session.get(TelegramUpdate, 105)
        assert refreshed.status == "done"

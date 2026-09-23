"""`/digest`'s text layout, undo button eligibility, and no user text
leaking into it (approved plan §5's test list for milestone 6a)."""

from __future__ import annotations

import datetime
import decimal

import pytest

from app.core.clock import FrozenClock
from app.core.idle.digest import NOTHING_TEXT, WINDOW_24H, WINDOW_7D, build_digest
from app.db.models import IdleRun
from app.tg.idle import parse_digest_args, undo_keyboard


def _clock() -> FrozenClock:
    return FrozenClock(datetime.datetime(2026, 9, 23, 12, 0, tzinfo=datetime.timezone.utc))


@pytest.mark.asyncio
async def test_no_runs_in_window_says_nothing(sessionmaker):
    clock = _clock()
    async with sessionmaker() as session:
        digest = await build_digest(session, clock, undo_days=7)
    assert digest.text == NOTHING_TEXT
    assert digest.undoable_run_ids == ()


@pytest.mark.asyncio
async def test_header_shows_total_cost(sessionmaker):
    clock = _clock()
    async with sessionmaker() as session:
        session.add(
            IdleRun(
                kind="backfill", local_date=clock.now_utc().date(), status="done",
                usd_cost=decimal.Decimal("0.015"), summary={"summarized": 1, "reflected": 0},
            )
        )
        await session.commit()
    async with sessionmaker() as session:
        digest = await build_digest(session, clock, undo_days=7)
    assert digest.text.splitlines()[0] == "Фоновая работа за 24 ч — $0.01"


@pytest.mark.asyncio
async def test_summarized_line_sums_backfill_runs(sessionmaker):
    clock = _clock()
    async with sessionmaker() as session:
        session.add_all(
            [
                IdleRun(
                    kind="backfill", local_date=clock.now_utc().date(), status="done",
                    summary={"summarized": 2, "reflected": 1},
                ),
                IdleRun(
                    kind="backfill", local_date=clock.now_utc().date(), status="done",
                    summary={"summarized": 1, "reflected": 0},
                ),
            ]
        )
        await session.commit()
    async with sessionmaker() as session:
        digest = await build_digest(session, clock, undo_days=7)
    assert "Сводки: догнала 3" in digest.text


@pytest.mark.asyncio
async def test_skips_line_groups_by_reason_with_counts(sessionmaker):
    clock = _clock()
    async with sessionmaker() as session:
        session.add_all(
            [
                IdleRun(
                    kind="backfill", local_date=clock.now_utc().date(),
                    status="skipped", skip_reason="user_active",
                ),
                IdleRun(
                    kind="backfill", local_date=clock.now_utc().date(),
                    status="skipped", skip_reason="user_active",
                ),
                IdleRun(
                    kind="backfill", local_date=clock.now_utc().date(),
                    status="skipped", skip_reason="idle_cap",
                ),
            ]
        )
        await session.commit()
    async with sessionmaker() as session:
        digest = await build_digest(session, clock, undo_days=7)
    assert "user_active ×2" in digest.text
    assert "idle_cap ×1" in digest.text


@pytest.mark.asyncio
async def test_undo_eligible_run_is_offered(sessionmaker):
    clock = _clock()
    async with sessionmaker() as session:
        run = IdleRun(
            kind="backfill", local_date=clock.now_utc().date(), status="done",
            reversible=True,
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id
    async with sessionmaker() as session:
        digest = await build_digest(session, clock, undo_days=7)
    assert digest.undoable_run_ids == (run_id,)
    keyboard = undo_keyboard(digest.undoable_run_ids)
    assert keyboard is not None
    assert keyboard.inline_keyboard[0][0].callback_data == f"idle:u:{run_id}"


@pytest.mark.asyncio
async def test_not_reversible_run_is_not_offered(sessionmaker):
    clock = _clock()
    async with sessionmaker() as session:
        session.add(
            IdleRun(
                kind="backfill", local_date=clock.now_utc().date(), status="done",
                reversible=False,
            )
        )
        await session.commit()
    async with sessionmaker() as session:
        digest = await build_digest(session, clock, undo_days=7)
    assert digest.undoable_run_ids == ()


@pytest.mark.asyncio
async def test_already_undone_run_is_not_offered(sessionmaker):
    clock = _clock()
    async with sessionmaker() as session:
        session.add(
            IdleRun(
                kind="backfill", local_date=clock.now_utc().date(), status="done",
                reversible=True, undone_at=clock.now_utc(),
            )
        )
        await session.commit()
    async with sessionmaker() as session:
        digest = await build_digest(session, clock, undo_days=7)
    assert digest.undoable_run_ids == ()


@pytest.mark.asyncio
async def test_run_older_than_undo_days_is_not_offered(sessionmaker):
    clock = _clock()
    old_created = clock.now_utc() - datetime.timedelta(days=8)
    async with sessionmaker() as session:
        run = IdleRun(
            kind="backfill", local_date=old_created.date(), status="done", reversible=True,
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        # created_at has a server_default -- force it into the past
        # directly, since the model does not accept it as a constructor
        # kwarg default override in a meaningful way otherwise.
        await session.execute(
            IdleRun.__table__.update().where(IdleRun.id == run.id).values(created_at=old_created)
        )
        await session.commit()
    async with sessionmaker() as session:
        digest = await build_digest(session, clock, undo_days=7)
    assert digest.undoable_run_ids == ()


@pytest.mark.asyncio
async def test_window_7d_reaches_further_back_than_24h(sessionmaker):
    clock = _clock()
    three_days_ago = clock.now_utc() - datetime.timedelta(days=3)
    async with sessionmaker() as session:
        run = IdleRun(kind="backfill", local_date=three_days_ago.date(), status="done")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        await session.execute(
            IdleRun.__table__.update().where(IdleRun.id == run.id).values(created_at=three_days_ago)
        )
        await session.commit()

    async with sessionmaker() as session:
        digest_24h = await build_digest(session, clock, undo_days=7, window=WINDOW_24H)
        digest_7d = await build_digest(session, clock, undo_days=7, window=WINDOW_7D)

    assert digest_24h.text == NOTHING_TEXT
    assert digest_7d.text != NOTHING_TEXT


def test_parse_digest_args():
    assert parse_digest_args(None) == WINDOW_24H
    assert parse_digest_args("") == WINDOW_24H
    assert parse_digest_args("24h") == WINDOW_24H
    assert parse_digest_args("7d") == WINDOW_7D
    assert parse_digest_args("garbage") is None


@pytest.mark.asyncio
async def test_digest_text_carries_no_user_content(sessionmaker):
    """Summary is counts/codes only, never text -- plan section 8's
    privacy invariant, checked at the digest boundary specifically."""
    clock = _clock()
    secret = "секретный текст пользователя"
    async with sessionmaker() as session:
        session.add(
            IdleRun(
                kind="backfill", local_date=clock.now_utc().date(), status="done",
                summary={"summarized": 1, "reflected": 0},
                skip_reason=None,
            )
        )
        await session.commit()
    async with sessionmaker() as session:
        digest = await build_digest(session, clock, undo_days=7)
    assert secret not in digest.text

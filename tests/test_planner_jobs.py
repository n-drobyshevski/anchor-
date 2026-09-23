"""app/planner/jobs.py's run_planner_sync: the once-only reconnect notice."""

from __future__ import annotations

import datetime

import pytest
from aiogram import Bot

from app.config import Settings
from app.db.models import PlannerCredential, PlannerSnapshot, UserState
from app.planner import auth
from app.planner.jobs import RELINK_NOTICE, run_planner_sync
from conftest import make_bot

pytestmark = pytest.mark.asyncio

TZ = "Europe/Paris"


class _RevokedClient:
    async def get_agenda(self, settings, session, clock, **kwargs):
        raise auth.PlannerAuthError("revoked")


class _OkClient:
    async def get_agenda(self, settings, session, clock, **kwargs):
        return {"events": [], "tasks": []}


async def test_a_revoked_credential_sends_the_notice_exactly_once(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    settings = Settings(_env_file=None, PLANNER_ENABLED=True)
    bot, fake_session = make_bot()

    async with sessionmaker() as session:
        session.add(
            PlannerCredential(
                id=1, access_token="a", refresh_token="r",
                expires_at=clock.now_utc() + datetime.timedelta(hours=1), status="revoked",
            )
        )
        await session.commit()

        await run_planner_sync(
            session, settings, _RevokedClient(), clock, timezone=TZ, bot=bot, chat_id=4242,
        )
        await run_planner_sync(
            session, settings, _RevokedClient(), clock, timezone=TZ, bot=bot, chat_id=4242,
        )

    assert len(fake_session.sent) == 1
    assert fake_session.sent[0].text == RELINK_NOTICE


async def test_a_successful_sync_writes_the_snapshot_and_sends_nothing(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    settings = Settings(_env_file=None, PLANNER_ENABLED=True)
    bot, fake_session = make_bot()

    async with sessionmaker() as session:
        session.add(
            PlannerCredential(
                id=1, access_token="a", refresh_token="r",
                expires_at=clock.now_utc() + datetime.timedelta(hours=1), status="active",
            )
        )
        await session.commit()
        await run_planner_sync(
            session, settings, _OkClient(), clock, timezone=TZ, bot=bot, chat_id=4242,
        )

    assert fake_session.sent == []
    async with sessionmaker() as session:
        row = await session.get(PlannerSnapshot, 1)
    assert row is not None

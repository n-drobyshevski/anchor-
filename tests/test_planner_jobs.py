"""app/planner/jobs.py: run_planner_sync's once-only reconnect notice,
and run_planner_write's one-MCP-call-per-action shape (P3)."""

from __future__ import annotations

import datetime

import pytest
from aiogram import Bot

from app.config import Settings
from app.db.models import PlannerCredential, PlannerSnapshot, UserState
from app.planner import actions, auth
from app.planner.client import PlannerToolError, PlannerUnavailable
from app.planner.jobs import (
    RELINK_NOTICE,
    WRITE_AUTH_FAILED_NOTICE,
    WRITE_OK_DONE,
    WRITE_OK_TASK,
    run_planner_sync,
    run_planner_write,
)
from conftest import make_bot

pytestmark = pytest.mark.asyncio

TZ = "Europe/Paris"


class _RevokedClient:
    async def get_agenda(self, settings, session, clock, **kwargs):
        raise auth.PlannerAuthError("revoked")


class _OkClient:
    async def get_agenda(self, settings, session, clock, **kwargs):
        return {"events": [], "tasks": []}


class _WriteClient:
    """A fake covering the write methods run_planner_write calls, plus
    get_agenda for its post-write snapshot sync."""

    def __init__(self, *, raises: Exception | None = None):
        self.raises = raises
        self.create_task_calls: list[dict] = []
        self.create_event_calls: list[dict] = []
        self.complete_task_calls: list[dict] = []

    async def create_task(self, settings, session, clock, **kwargs):
        if self.raises:
            raise self.raises
        self.create_task_calls.append(kwargs)
        return {"id": "t1"}

    async def create_event(self, settings, session, clock, **kwargs):
        if self.raises:
            raise self.raises
        self.create_event_calls.append(kwargs)
        return {"id": "e1"}

    async def complete_task(self, settings, session, clock, **kwargs):
        if self.raises:
            raise self.raises
        self.complete_task_calls.append(kwargs)
        return {"id": kwargs.get("task_id")}

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


# --- P3: run_planner_write -------------------------------------------------


async def test_write_calls_create_task_with_the_anchor_client_request_id_and_confirms(
    sessionmaker, frozen_clock
):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    settings = Settings(_env_file=None, PLANNER_ENABLED=True, PLANNER_WRITE_PRIVATE=True)
    bot, fake_session = make_bot()
    client = _WriteClient()

    async with sessionmaker() as session:
        action = await actions.create(
            session, clock, kind=actions.CREATE_TASK,
            payload={"title": "Купить молоко", "due_date": "2026-09-24"},
        )
        await actions.accept(session, clock, action.id)

        await run_planner_write(
            session, settings, client, clock,
            planner_action_id=action.id, timezone=TZ, bot=bot, chat_id=4242,
        )

    assert client.create_task_calls == [
        {"title": "Купить молоко", "due_date": "2026-09-24", "client_request_id": f"anchor:{action.id}"}
    ]
    assert len(fake_session.sent) == 1
    assert fake_session.sent[0].text == WRITE_OK_TASK.format(title="Купить молоко")


async def test_write_calls_create_event_with_is_private_from_settings(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    settings = Settings(_env_file=None, PLANNER_ENABLED=True, PLANNER_WRITE_PRIVATE=True)
    bot, fake_session = make_bot()
    client = _WriteClient()

    payload = {
        "title": "Встреча", "start": "2026-09-24T18:00:00+02:00",
        "end": "2026-09-24T19:00:00+02:00", "all_day": False,
    }
    async with sessionmaker() as session:
        action = await actions.create(session, clock, kind=actions.CREATE_EVENT, payload=payload)
        await actions.accept(session, clock, action.id)
        await run_planner_write(
            session, settings, client, clock,
            planner_action_id=action.id, timezone=TZ, bot=bot, chat_id=4242,
        )

    assert len(client.create_event_calls) == 1
    assert client.create_event_calls[0]["is_private"] is True
    assert client.create_event_calls[0]["client_request_id"] == f"anchor:{action.id}"


async def test_write_calls_complete_task_with_the_task_id(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    settings = Settings(_env_file=None, PLANNER_ENABLED=True)
    bot, fake_session = make_bot()
    client = _WriteClient()

    async with sessionmaker() as session:
        action = await actions.create(
            session, clock, kind=actions.COMPLETE_TASK,
            payload={"task_id": "abc-123", "title": "Позвонить маме"},
        )
        await actions.accept(session, clock, action.id)
        await run_planner_write(
            session, settings, client, clock,
            planner_action_id=action.id, timezone=TZ, bot=bot, chat_id=4242,
        )

    assert client.complete_task_calls == [{"task_id": "abc-123"}]
    assert fake_session.sent[0].text == WRITE_OK_DONE.format(title="Позвонить маме")


async def test_write_on_a_non_accepted_action_does_nothing(sessionmaker, frozen_clock):
    """Not yet accepted, already rejected, or unknown -- none of these
    should ever reach the client. Mirrors the replay guard run_planner_write's
    docstring describes for a re-enqueued job."""
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    settings = Settings(_env_file=None, PLANNER_ENABLED=True)
    bot, fake_session = make_bot()
    client = _WriteClient()

    async with sessionmaker() as session:
        pending = await actions.create(
            session, clock, kind=actions.CREATE_TASK, payload={"title": "A", "due_date": None}
        )
        await run_planner_write(
            session, settings, client, clock,
            planner_action_id=pending.id, timezone=TZ, bot=bot, chat_id=4242,
        )
        await run_planner_write(
            session, settings, client, clock,
            planner_action_id=999999, timezone=TZ, bot=bot, chat_id=4242,
        )

    assert client.create_task_calls == []
    assert fake_session.sent == []


async def test_write_reraises_on_planner_unavailable_for_the_queue_to_retry(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    settings = Settings(_env_file=None, PLANNER_ENABLED=True)
    bot, fake_session = make_bot()
    client = _WriteClient(raises=PlannerUnavailable("timeout"))

    async with sessionmaker() as session:
        action = await actions.create(
            session, clock, kind=actions.CREATE_TASK, payload={"title": "A", "due_date": None}
        )
        await actions.accept(session, clock, action.id)
        with pytest.raises(PlannerUnavailable):
            await run_planner_write(
                session, settings, client, clock,
                planner_action_id=action.id, timezone=TZ, bot=bot, chat_id=4242,
            )
    assert fake_session.sent == []


async def test_write_reraises_on_a_generic_tool_error(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    settings = Settings(_env_file=None, PLANNER_ENABLED=True)
    bot, fake_session = make_bot()
    client = _WriteClient(raises=PlannerToolError("bad request"))

    async with sessionmaker() as session:
        action = await actions.create(
            session, clock, kind=actions.CREATE_TASK, payload={"title": "A", "due_date": None}
        )
        await actions.accept(session, clock, action.id)
        with pytest.raises(PlannerToolError):
            await run_planner_write(
                session, settings, client, clock,
                planner_action_id=action.id, timezone=TZ, bot=bot, chat_id=4242,
            )


async def test_write_sends_the_relink_notice_exactly_once_on_a_revoked_grant(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    settings = Settings(_env_file=None, PLANNER_ENABLED=True)
    bot, fake_session = make_bot()
    client = _WriteClient(raises=auth.PlannerAuthError("revoked"))

    async with sessionmaker() as session:
        session.add(
            PlannerCredential(
                id=1, access_token="a", refresh_token="r",
                expires_at=clock.now_utc() + datetime.timedelta(hours=1), status="revoked",
            )
        )
        action1 = await actions.create(
            session, clock, kind=actions.CREATE_TASK, payload={"title": "A", "due_date": None}
        )
        action2 = await actions.create(
            session, clock, kind=actions.CREATE_TASK, payload={"title": "B", "due_date": None}
        )
        await actions.accept(session, clock, action1.id)
        await actions.accept(session, clock, action2.id)

        await run_planner_write(
            session, settings, client, clock,
            planner_action_id=action1.id, timezone=TZ, bot=bot, chat_id=4242,
        )
        await run_planner_write(
            session, settings, client, clock,
            planner_action_id=action2.id, timezone=TZ, bot=bot, chat_id=4242,
        )

    assert len(fake_session.sent) == 1
    assert fake_session.sent[0].text == WRITE_AUTH_FAILED_NOTICE

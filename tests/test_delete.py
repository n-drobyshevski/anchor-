"""/delete (phase-2 plan sections 11 and 14, build rule 7).

Rule 7 is "/delete must really delete", and the two tests that enforce
it are not the behavioural ones -- they are the invariants at the bottom
of this file. A behavioural test proves today's tables get wiped; the
invariants fail the day someone adds a table or a column and forgets
this code exists, which is the only day it matters.
"""

from __future__ import annotations

import asyncio
import datetime
import decimal
import time

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import func, select, update as sql_update

from app.config import Settings
from app.core import purge
from app.db.models import (
    BackupLog,
    Base,
    BriefNote,
    Checkin,
    CheckinOrderResult,
    IdleChange,
    IdleRun,
    InterestTopic,
    Job,
    Journal,
    Memory,
    NotebookEntry,
    Outbound,
    Message,
    PendingMemory,
    PersonaAmendment,
    PersonaVersion,
    Proposal,
    ReviewProposal,
    SafetyEvent,
    Scene,
    SpendLedger,
    StandingOrder,
    StateChange,
    StudyCard,
    StudyClip,
    StudyJob,
    TelegramUpdate,
    UserState,
    WebSession,
    WeeklyReview,
)
from app.tg import data as data_ui
from app.tg.router import build_router
from conftest import FakeLLMProvider, FakeSession

pytestmark = pytest.mark.asyncio

CHAT_ID = 555
TIMEZONE = "Europe/Paris"


def _command_update(update_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Test"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(text)}],
        },
    }


def _callback_update(update_id: int, data: str, *, message_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Test"},
            "chat_instance": "ci",
            "data": data,
            "message": {
                "message_id": message_id,
                "date": 0,
                "chat": {"id": CHAT_ID, "type": "private"},
                "text": data_ui.CONFIRM_TEXT,
            },
        },
    }


def _build_dp(sessionmaker, hub=None):
    fake = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)
    dp = Dispatcher()
    dp.include_router(
        build_router(sessionmaker, Settings(), FakeLLMProvider(), FakeLLMProvider(), hub=hub)
    )
    return dp, bot, fake


async def _seed_everything(sessionmaker, *update_ids: int) -> None:
    """One row in every purgeable table, plus a dirtied user_state."""
    now = datetime.datetime.now(datetime.timezone.utc)
    today = datetime.date.today()
    async with sessionmaker() as session:
        session.add(
            UserState(
                id=1, chat_id=CHAT_ID, timezone="America/New_York",
                persona_active=False, intensity=5, focus_on=True, focus_since=now,
                due_action="сдать отчёт", due_set_at=now, streak=9,
                last_checkin_at=now, awaiting="checkin_note", awaiting_ref=1,
            )
        )
        session.add(PersonaVersion(sha256="abc123", body="# Anchor"))
        await session.commit()
        for update_id in (1, *update_ids):
            session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()
        scene = Scene(started_at=now, ended_at=now, summary="сводка")
        session.add(scene)
        await session.commit()
        await session.refresh(scene)
        # 5e: dirty callback_scene too, same targeted-update pattern
        # app/core/callbacks.py's own mark_delivered() uses.
        await session.execute(
            sql_update(UserState).where(UserState.id == 1).values(callback_scene=scene.id)
        )
        await session.commit()
        session.add_all(
            [
                Message(role="user", content="текст", ooc=False, kind="chat",
                        update_id=1, scene_id=scene.id),
                Memory(kind="identity", text="факт", source="user"),
                # 5b.
                NotebookEntry(kind="observation", text="заметка", source="anchor"),
                PendingMemory(text="незавершённая заметка"),
                Checkin(local_date=today, day_rating=4),
                Proposal(field="due_action", value="что-то"),
                Journal(local_date=today, text="запись"),
                StateChange(field="intensity", old_value="3", new_value="5", source="command"),
                SpendLedger(local_date=today, category="chat", usd_cost=decimal.Decimal("0.01")),
                Job(kind="extract", payload={}, dedup_key="extract:1"),
                # 3a: a proactive message is a record of what the bot
                # said to this user, so "delete all my data" has to take
                # it too (phase-3 plan section 4).
                Outbound(
                    kind="morning",
                    local_date=today,
                    bucket=0,
                    planned_for=now,
                    status="sent",
                    sent_at=now,
                ),
                # H2: no content, but still a record of when this user
                # was talked to and how the safety checks behaved while
                # they were.
                SafetyEvent(
                    local_date=today,
                    kind="welfare",
                    outcome="ok",
                    model="fake-safety",
                ),
                # Web-chat plan track 1: a live session cookie is a
                # credential, so "delete all my data" purges it too
                # (app/core/purge.py's PURGED_TABLES).
                WebSession(
                    token_hash=b"\x00" * 32,
                    expires_at=now + datetime.timedelta(days=1),
                ),
            ]
        )
        await session.commit()

        # 4a: the research loop's three tables (phase-4 plan section 4).
        # study_clip holds text fetched from the web and study_card
        # holds quotes from it -- the most content-bearing thing /delete
        # has had to wipe since `message`. Added last because the clip
        # and the card need the ids above them.
        job = StudyJob(kind="read", local_date=today, status="done")
        session.add(job)
        await session.flush()
        clip = StudyClip(
            job_id=job.id,
            url="https://example.com/sleep",
            domain="example.com",
            title="Как высыпаться",
            text="Ложитесь спать в одно и то же время каждый день.",
            http_status=200,
            fetched_at=now,
        )
        session.add(clip)
        await session.flush()
        session.add(
            StudyCard(
                job_id=job.id,
                clip_id=clip.id,
                kind="technique",
                text="Ложиться в одно и то же время.",
                quote="Ложитесь спать в одно и то же время каждый день.",
                source_url="https://example.com/sleep",
                risk_model="low",
                risk_rules="low",
                risk_final="low",
            )
        )
        await session.commit()

        # 5c: a standing order and its check-in result (phase-5 plan
        # section 3) -- checkin_order_result needs both ids, so it is
        # added last, same reasoning as study_clip/study_card above.
        checkin_row = (await session.execute(select(Checkin))).scalars().one()
        order = StandingOrder(
            text="пить воду по утрам", cadence="daily", status="active", source="user"
        )
        session.add(order)
        await session.flush()
        session.add(
            CheckinOrderResult(checkin_id=checkin_row.id, order_id=order.id, result="no")
        )
        await session.commit()

        # 5d: the weekly review and persona amendments (phase-5 plan
        # sections 3, 8 and 9) -- weekly_review needs its id before
        # review_proposal, which needs its own before persona_amendment,
        # same "added last, needs the ids above it" reasoning as
        # study_clip/study_card and checkin_order_result above.
        review = WeeklyReview(
            week_start=today, analysis={"wins": [], "misses": [], "patterns": [],
                                          "intentions": [], "proposals": []},
        )
        session.add(review)
        await session.flush()
        proposal = ReviewProposal(
            review_id=review.id, kind="persona_note", text="меньше вопросов утром"
        )
        session.add(proposal)
        await session.flush()
        session.add(
            PersonaAmendment(
                text="меньше вопросов утром",
                status="trial",
                proposal_id=proposal.id,
                persona_sha="deadbeef",
            )
        )
        await session.commit()

        # 6a: the idle framework's own five tables (Phase 6 plan section
        # 3). idle_change needs idle_run's id, so it is added in the same
        # flush right after.
        run = IdleRun(kind="backfill", local_date=today, status="done", reversible=True)
        session.add(run)
        await session.flush()
        session.add(
            IdleChange(
                run_id=run.id,
                table_name="memory",
                row_id=1,
                op="insert",
                after={"id": 1},
            )
        )
        session.add(BriefNote(local_date=today, notes=["сон", "вода"]))
        session.add(InterestTopic(text="сон", packet="ref"))
        session.add(
            BackupLog(started_at=now, finished_at=now, object_key="anchor/x", bytes=10, status="ok")
        )
        await session.commit()


async def _counts(sessionmaker) -> dict[str, int]:
    async with sessionmaker() as session:
        out = {}
        for name, table in Base.metadata.tables.items():
            out[name] = (
                await session.execute(select(func.count()).select_from(table))
            ).scalar_one()
        return out


# --- the wipe ---


async def test_delete_wipes_every_purged_table(sessionmaker, clock):
    await _seed_everything(sessionmaker)
    before = await _counts(sessionmaker)
    assert all(before[name] > 0 for name in purge.PURGED_TABLES), "fixture must seed everything"

    async with sessionmaker() as session:
        await purge.delete_everything(session, Settings(), clock)

    after = await _counts(sessionmaker)
    for name in purge.PURGED_TABLES:
        if name == "state_change":
            # One deliberate audit row records that the wipe happened.
            assert after[name] == 1, name
        else:
            assert after[name] == 0, f"{name} survived the wipe"


async def test_pending_memory_is_wiped_though_the_plan_omits_it(sessionmaker, clock):
    """It holds text typed at /remember and never classified. Leaving it
    behind after "delete all my data" is the bug rule 7 names."""
    await _seed_everything(sessionmaker)
    async with sessionmaker() as session:
        await purge.delete_everything(session, Settings(), clock)
        rows = (await session.execute(select(PendingMemory))).scalars().all()
    assert rows == []


async def test_persona_version_survives(sessionmaker, clock):
    """Plan section 11: "Keep persona_version". It is a hash of a file in
    the repo, not user data."""
    await _seed_everything(sessionmaker)
    async with sessionmaker() as session:
        await purge.delete_everything(session, Settings(), clock)
        rows = (await session.execute(select(PersonaVersion))).scalars().all()
    assert len(rows) == 1


async def test_user_state_is_reset_but_keeps_chat_id(sessionmaker, clock):
    await _seed_everything(sessionmaker)
    settings = Settings(TZ_DEFAULT=TIMEZONE)

    async with sessionmaker() as session:
        await purge.delete_everything(session, settings, clock)

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)

    assert state is not None, "the row must be reset in place, never dropped"
    assert state.chat_id == CHAT_ID
    assert state.persona_active is True
    assert state.intensity == 3
    assert state.timezone == TIMEZONE
    assert state.focus_on is False and state.focus_since is None
    assert state.due_action is None and state.due_set_at is None
    assert state.streak == 0 and state.last_checkin_at is None
    assert state.awaiting is None and state.awaiting_ref is None
    assert state.callback_scene is None


async def test_ids_restart_so_the_first_new_row_is_one(sessionmaker, clock):
    """After "delete everything", /memories showing #47 would be a lie."""
    await _seed_everything(sessionmaker)
    async with sessionmaker() as session:
        for i in range(5):
            session.add(Memory(kind="event", text=f"факт номер {i}", source="user"))
        await session.commit()
        await purge.delete_everything(session, Settings(), clock)

    async with sessionmaker() as session:
        row = Memory(kind="identity", text="первый новый факт", source="user")
        session.add(row)
        await session.commit()
        await session.refresh(row)
    assert row.id == 1


async def test_the_audit_row_records_the_fact_and_no_content(sessionmaker, clock):
    await _seed_everything(sessionmaker)
    async with sessionmaker() as session:
        await purge.delete_everything(session, Settings(), clock)
        rows = (await session.execute(select(StateChange))).scalars().all()

    assert len(rows) == 1
    audit = rows[0]
    assert audit.field == "data"
    assert audit.new_value == "deleted"
    assert audit.old_value is None
    for value in (audit.field, audit.old_value, audit.new_value, audit.source):
        assert value is None or "факт" not in value


async def test_the_bot_still_works_after_a_wipe(sessionmaker, clock):
    """get_state raises on a missing row, so a delete that dropped
    user_state would crash every later message."""
    from app.core.state import get_state

    await _seed_everything(sessionmaker)
    async with sessionmaker() as session:
        await purge.delete_everything(session, Settings(), clock)
    async with sessionmaker() as session:
        state = await get_state(session)
    assert state.chat_id == CHAT_ID


# --- cancelling research work before the wipe (4d, plan section 9) ---


async def test_cancel_research_jobs_marks_non_terminal_study_jobs_cancelled(sessionmaker):
    """ck_study_job_status's seven values, all at once: the four
    non-terminal ones must flip, the three terminal ones must not."""
    today = datetime.date.today()
    statuses = ("queued", "searching", "fetching", "distilling", "done", "failed", "cancelled")
    async with sessionmaker() as session:
        jobs = {status: StudyJob(kind="read", local_date=today, status=status) for status in statuses}
        session.add_all(jobs.values())
        await session.commit()

        await purge.cancel_research_jobs(session)
        await session.commit()

        for job in jobs.values():
            await session.refresh(job)

    for status in ("queued", "searching", "fetching", "distilling"):
        assert jobs[status].status == "cancelled", status
    for status in ("done", "failed", "cancelled"):
        assert jobs[status].status == status, status


async def test_cancel_research_jobs_removes_only_pending_research_queue_rows(sessionmaker):
    """A claimed (processing) research job row is left alone -- it is
    the worker still running it that owns its fate, not this function
    (see cancel_research_jobs' docstring on why that is an honest
    limitation, not a bug). A pending job of any other kind is untouched
    regardless of status."""
    async with sessionmaker() as session:
        pending_research = Job(kind="research", payload={}, dedup_key="research:pending")
        processing_research = Job(
            kind="research", payload={}, dedup_key="research:processing", status="processing"
        )
        pending_other = Job(kind="extract", payload={}, dedup_key="extract:pending")
        session.add_all([pending_research, processing_research, pending_other])
        await session.commit()

        await purge.cancel_research_jobs(session)
        await session.commit()

        remaining = (await session.execute(select(Job))).scalars().all()

    assert {row.dedup_key for row in remaining} == {"research:processing", "extract:pending"}


async def test_delete_wipes_in_flight_research_work_without_erroring(sessionmaker, clock):
    """A non-terminal study_job and a still-pending research job row must
    not make delete_everything raise (e.g. a CHECK constraint tripped by
    the cancel step) -- they must simply end up gone like everything
    else, same as test_delete_wipes_every_purged_table already checks
    for the ordinary case."""
    today = datetime.date.today()
    async with sessionmaker() as session:
        session.add(StudyJob(kind="study", local_date=today, status="fetching"))
        session.add(Job(kind="research", payload={"job_id": 1}, dedup_key="research:inflight"))
        await session.commit()

    async with sessionmaker() as session:
        await purge.delete_everything(session, Settings(), clock)

    async with sessionmaker() as session:
        assert (await session.execute(select(StudyJob))).scalars().all() == []
        assert (await session.execute(select(Job))).scalars().all() == []


async def test_the_research_job_kind_literal_matches_the_research_package():
    """purge.py hardcodes 'research' rather than importing app.research.
    jobs.RESEARCH (see the comment by _RESEARCH_JOB_KIND); this is what
    keeps the two pinned together instead of silently drifting apart."""
    from app.research.jobs import RESEARCH

    assert purge._RESEARCH_JOB_KIND == RESEARCH


# --- the two-step confirmation ---


async def test_the_command_alone_deletes_nothing(sessionmaker):
    """Plan section 14: "the second step needs the button"."""
    await _seed_everything(sessionmaker, 2)
    dp, bot, fake = _build_dp(sessionmaker)

    await dp.feed_update(
        bot, Update.model_validate(_command_update(2, "/delete"), context={"bot": bot})
    )

    assert fake.sent[-1].text == data_ui.CONFIRM_TEXT
    labels = [b.text for r in fake.sent[-1].reply_markup.inline_keyboard for b in r]
    assert labels == [data_ui.CONFIRM_YES, data_ui.CONFIRM_NO]

    after = await _counts(sessionmaker)
    assert after["memory"] == 1, "nothing may be deleted before the button"


async def test_confirming_wipes_everything(sessionmaker):
    await _seed_everything(sessionmaker, 2, 3)
    dp, bot, fake = _build_dp(sessionmaker)

    await dp.feed_update(
        bot, Update.model_validate(_command_update(2, "/delete"), context={"bot": bot})
    )
    issued = int(time.time())
    await dp.feed_update(
        bot,
        Update.model_validate(_callback_update(3, f"d:yes:{issued}"), context={"bot": bot}),
    )

    after = await _counts(sessionmaker)
    assert after["memory"] == 0
    assert after["message"] == 0
    assert fake.edits[-1].text == data_ui.DELETED_TEXT
    assert fake.edits[-1].reply_markup is None


async def test_confirming_also_closes_the_web_hub(sessionmaker):
    """Medium-severity finding: a Telegram-issued /delete used to leave
    the WebHub entirely untouched -- purge.py truncates `message` and
    `web_session`, but the hub's own 200-event ring buffer (and any
    still-open SSE stream) kept the supposedly deleted conversation
    replayable to anyone who opened GET /api/events afterwards. /delete
    now closes the same hub /weblogout does, once the wipe commits."""
    from app.web.hub import WebHub

    await _seed_everything(sessionmaker, 2, 3)
    hub = WebHub()
    hub.publish_message(
        id=1, role="user", text="секрет до удаления", kind="chat", keyboard=None,
        ts=datetime.datetime.now(datetime.timezone.utc),
    )
    sub = hub.subscribe()
    dp, bot, fake = _build_dp(sessionmaker, hub=hub)

    await dp.feed_update(
        bot, Update.model_validate(_command_update(2, "/delete"), context={"bot": bot})
    )
    issued = int(time.time())
    await dp.feed_update(
        bot,
        Update.model_validate(_callback_update(3, f"d:yes:{issued}"), context={"bot": bot}),
    )

    # The stream got the poison pill...
    remaining = [event async for event in sub.events()]
    assert remaining == []
    # ...and the ring buffer no longer replays the pre-delete text to a
    # brand new subscriber either.
    fresh = hub.subscribe(last_event_id=0)
    fresh.close()
    assert fresh.backlog == []


async def test_cancelling_does_not_touch_the_web_hub(sessionmaker):
    """Only a *successful* wipe closes the hub -- "Отмена" must not end
    a session's live view for no reason."""
    from app.web.hub import WebHub

    await _seed_everything(sessionmaker, 2, 3)
    hub = WebHub()
    sub = hub.subscribe()
    dp, bot, fake = _build_dp(sessionmaker, hub=hub)

    await dp.feed_update(
        bot, Update.model_validate(_command_update(2, "/delete"), context={"bot": bot})
    )
    await dp.feed_update(
        bot, Update.model_validate(_callback_update(3, "d:no"), context={"bot": bot})
    )

    # Still open: nothing closed it.
    hub.publish_toast("живой")
    event = await asyncio.wait_for(sub.events().__anext__(), timeout=1)
    assert event.event == "toast"
    sub.close()


async def test_the_confirmation_names_the_real_provider(sessionmaker):
    """6e (plan section 9.3) replaces the 1e-era wording with the
    plan's own exact final-reply text, which now also covers backups
    rather than naming a specific model vendor."""
    assert "xAI" not in data_ui.DELETED_TEXT
    assert data_ui.DELETED_TEXT == (
        "Удалено, включая бэкапы. Копии у провайдеров моделей удаляются по их "
        "правилам хранения."
    )


async def test_the_confirm_text_mentions_backups(sessionmaker):
    """6e (plan section 9.3)'s exact confirm text."""
    assert data_ui.CONFIRM_TEXT == "Удалить все данные и все резервные копии? Это необратимо."


class _FakeS3Client:
    """An in-memory stand-in for boto3's S3 client -- just enough of
    list_objects_v2/delete_objects for app/ops/backup.py's purge path
    (plan section 12: "delete: purges the backup objects in a fake
    S3"). No network, no moto -- per the approved decision."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None):
        matching = sorted(k for k in self.objects if k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in matching], "IsTruncated": False}

    def delete_objects(self, *, Bucket, Delete):
        for entry in Delete["Objects"]:
            self.objects.pop(entry["Key"], None)
        return {}


async def test_delete_purges_backup_objects_in_a_fake_s3(sessionmaker, monkeypatch):
    """6e (plan section 9.3 and 12): /delete purges every object under
    the `anchor/` prefix, using a fake in-memory S3 client."""
    from app.ops import backup as backup_module

    fake_s3 = _FakeS3Client(
        {
            "anchor/2026/01/01/anchor-20260101T040000Z.dump.age": b"ciphertext-1",
            "anchor/2026/01/02/anchor-20260102T040000Z.dump.age": b"ciphertext-2",
            "unrelated/other-app/file": b"not ours",
        }
    )
    monkeypatch.setattr(backup_module, "build_s3_client", lambda settings: fake_s3)

    await _seed_everything(sessionmaker, 2, 3)
    settings = Settings(
        BACKUP_S3_ENDPOINT="https://fake.example",
        BACKUP_S3_BUCKET="anchor-backups",
        BACKUP_S3_ACCESS_KEY_ID="fake-key",
        BACKUP_S3_SECRET_ACCESS_KEY="fake-secret",
    )
    fake = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, settings, FakeLLMProvider(), FakeLLMProvider()))

    await dp.feed_update(
        bot, Update.model_validate(_command_update(2, "/delete"), context={"bot": bot})
    )
    issued = int(time.time())
    await dp.feed_update(
        bot,
        Update.model_validate(_callback_update(3, f"d:yes:{issued}"), context={"bot": bot}),
    )

    assert "anchor/2026/01/01/anchor-20260101T040000Z.dump.age" not in fake_s3.objects
    assert "anchor/2026/01/02/anchor-20260102T040000Z.dump.age" not in fake_s3.objects
    assert "unrelated/other-app/file" in fake_s3.objects
    assert fake.edits[-1].text == data_ui.DELETED_TEXT


async def test_delete_proceeds_when_s3_is_not_configured(sessionmaker):
    """Plan section 9.3: "if S3 isn't configured, the wipe still
    proceeds" -- the database side of /delete must not be blocked by a
    missing backup configuration."""
    await _seed_everything(sessionmaker, 2, 3)
    dp, bot, fake = _build_dp(sessionmaker)  # default Settings(): no S3 configured

    await dp.feed_update(
        bot, Update.model_validate(_command_update(2, "/delete"), context={"bot": bot})
    )
    issued = int(time.time())
    await dp.feed_update(
        bot,
        Update.model_validate(_callback_update(3, f"d:yes:{issued}"), context={"bot": bot}),
    )

    after = await _counts(sessionmaker)
    assert after["memory"] == 0
    assert fake.edits[-1].text == data_ui.DELETED_TEXT


async def test_cancelling_deletes_nothing(sessionmaker):
    await _seed_everything(sessionmaker, 2, 3)
    dp, bot, fake = _build_dp(sessionmaker)

    await dp.feed_update(
        bot, Update.model_validate(_command_update(2, "/delete"), context={"bot": bot})
    )
    await dp.feed_update(
        bot, Update.model_validate(_callback_update(3, "d:no"), context={"bot": bot})
    )

    after = await _counts(sessionmaker)
    assert after["memory"] == 1
    assert fake.edits[-1].text == data_ui.CANCELLED_TEXT
    assert fake.edits[-1].reply_markup is None


async def test_a_stale_button_deletes_nothing(sessionmaker):
    """A [Да, удалить] left in scrollback must not wipe everything when
    tapped by accident weeks later."""
    await _seed_everything(sessionmaker, 2)
    dp, bot, fake = _build_dp(sessionmaker)

    old = int(time.time()) - data_ui.CONFIRM_TTL - 1
    await dp.feed_update(
        bot, Update.model_validate(_callback_update(2, f"d:yes:{old}"), context={"bot": bot})
    )

    after = await _counts(sessionmaker)
    assert after["memory"] == 1
    assert fake.edits[-1].text == data_ui.STALE_TEXT
    assert len(fake.answered) == 1, "even a stale press is answered"


async def test_a_malformed_confirmation_deletes_nothing(sessionmaker):
    await _seed_everything(sessionmaker, 2)
    dp, bot, fake = _build_dp(sessionmaker)

    await dp.feed_update(
        bot, Update.model_validate(_callback_update(2, "d:yes:notanumber"), context={"bot": bot})
    )

    assert (await _counts(sessionmaker))["memory"] == 1
    assert fake.edits[-1].text == data_ui.STALE_TEXT


async def test_a_replayed_confirmation_is_harmless(sessionmaker):
    """The wipe destroys the message rows the usual replay gate reads,
    so idempotency comes from the wipe being repeatable instead."""
    await _seed_everything(sessionmaker, 2, 3)
    dp, bot, fake = _build_dp(sessionmaker)

    issued = int(time.time())
    for _ in range(2):
        await dp.feed_update(
            bot,
            Update.model_validate(_callback_update(3, f"d:yes:{issued}"), context={"bot": bot}),
        )

    after = await _counts(sessionmaker)
    assert after["memory"] == 0
    # Exactly one, not two: the second wipe truncates the first wipe's
    # audit row before writing its own. However many times it runs, the
    # log afterwards says "everything was deleted" once.
    assert after["state_change"] == 1


async def test_is_fresh_window():
    now = 1_000_000
    assert data_ui.is_fresh(now, now=now) is True
    assert data_ui.is_fresh(now - data_ui.CONFIRM_TTL + 1, now=now) is True
    assert data_ui.is_fresh(now - data_ui.CONFIRM_TTL - 1, now=now) is False
    # A button from the future is not fresh either -- that is a clock
    # problem, not a licence to wipe.
    assert data_ui.is_fresh(now + 60, now=now) is False


# --- THE invariants (build rule 7) ---


async def test_every_table_is_either_purged_or_deliberately_kept():
    """The one that matters. A table added in phase 3 must force a
    decision rather than silently surviving a delete."""
    covered = set(purge.PURGED_TABLES) | set(purge.KEPT_TABLES)
    assert set(Base.metadata.tables) == covered, (
        "a table is neither purged nor kept: "
        f"{set(Base.metadata.tables) ^ covered}"
    )


async def test_every_user_state_column_is_preserved_or_reset(clock):
    """A column added later must not silently keep its value through a
    wipe."""
    columns = {c.name for c in UserState.__table__.columns}
    handled = set(purge.PRESERVED_STATE_COLUMNS) | set(purge.reset_values(Settings(), clock))
    assert columns == handled, f"unhandled user_state columns: {columns ^ handled}"

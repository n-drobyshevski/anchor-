"""Scenes and the summarize_scene job (phase-2 plan sections 5 and 14).

Covers the plan's required cases: a 6h gap opens a new scene and
enqueues a summary; scenes under 3 messages are not summarized; welfare
and OOC rows are excluded from the summary input. Plus the cap-defer
path from section 12 and the message_count/replay behaviour that the
turn integration depends on.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core.scene import (
    Deferred,
    MIN_MESSAGES_FOR_SUMMARY,
    SUMMARIZE_SCENE,
    bump_message_count,
    ensure_open_scene,
    get_open_scene,
    next_local_midnight,
    render_dialogue,
    run_summarize_scene,
    summarizable_messages,
)
from app.db.jobs import enqueue_job
from app.db.models import Job, Message, Scene, SpendLedger
from conftest import FakeLLMProvider

pytestmark = pytest.mark.asyncio

IDLE_HOURS = 6
TIMEZONE = "Europe/Paris"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _settings(**overrides) -> Settings:
    base = {
        "LLM_MODEL": "thedrummer/cydonia-24b-v4.1",
        "LLM_MODEL_CHEAP": "thedrummer/cydonia-24b-v4.1",
        "DAILY_USD_CAP": 1.00,
        "SCENE_IDLE_HOURS": IDLE_HOURS,
    }
    base.update(overrides)
    return Settings(**base)


async def _add_message(
    session,
    scene_id: int,
    *,
    role: str = "user",
    content: str = "текст",
    ooc: bool = False,
    kind: str = "chat",
    created_at: datetime.datetime | None = None,
) -> Message:
    row = Message(role=role, content=content, ooc=ooc, kind=kind, scene_id=scene_id)
    if created_at is not None:
        row.created_at = created_at
    session.add(row)
    await bump_message_count(session, scene_id)
    await session.commit()
    await session.refresh(row)
    return row


# --- lifecycle ---


async def test_first_message_opens_a_scene(sessionmaker):
    async with sessionmaker() as session:
        scene_id = await ensure_open_scene(session, idle_hours=IDLE_HOURS)
        scene = await session.get(Scene, scene_id)

    assert scene.ended_at is None
    assert scene.message_count == 0


async def test_recent_message_keeps_the_same_scene(sessionmaker):
    async with sessionmaker() as session:
        first = await ensure_open_scene(session, idle_hours=IDLE_HOURS)
        await _add_message(session, first, created_at=_utcnow() - datetime.timedelta(hours=1))

    async with sessionmaker() as session:
        second = await ensure_open_scene(session, idle_hours=IDLE_HOURS)

    assert second == first


async def test_six_hour_gap_opens_a_new_scene_and_enqueues_a_summary(sessionmaker):
    """The plan's headline case (section 5 / section 16's last checkbox)."""
    stale_at = _utcnow() - datetime.timedelta(hours=IDLE_HOURS, minutes=1)
    async with sessionmaker() as session:
        first = await ensure_open_scene(session, idle_hours=IDLE_HOURS)
        await _add_message(session, first, created_at=stale_at)

    async with sessionmaker() as session:
        second = await ensure_open_scene(session, idle_hours=IDLE_HOURS)
        closed = await session.get(Scene, first)
        jobs = (await session.execute(select(Job))).scalars().all()

    assert second != first
    assert closed.ended_at is not None
    # ended_at is the last message's time, not now: the scene ended when
    # the conversation stopped, not when the user came back.
    assert abs((closed.ended_at - stale_at).total_seconds()) < 1

    assert len(jobs) == 1
    assert jobs[0].kind == SUMMARIZE_SCENE
    assert jobs[0].payload == {"scene_id": first}
    assert jobs[0].dedup_key == f"scene:{first}"


async def test_a_replayed_close_queues_one_summary(sessionmaker):
    """The close commits the scene and the enqueue together, but a worker
    crash can still replay the enqueue. dedup_key makes that free."""
    async with sessionmaker() as session:
        first = await ensure_open_scene(session, idle_hours=IDLE_HOURS)
        await _add_message(
            session, first, created_at=_utcnow() - datetime.timedelta(hours=IDLE_HOURS, minutes=1)
        )

    async with sessionmaker() as session:
        await ensure_open_scene(session, idle_hours=IDLE_HOURS)

    # Replay the enqueue the close performed, exactly as it performed it.
    async with sessionmaker() as session:
        await enqueue_job(
            session, SUMMARIZE_SCENE, {"scene_id": first}, dedup_key=f"scene:{first}"
        )
        jobs = (await session.execute(select(Job))).scalars().all()

    assert len(jobs) == 1


async def test_empty_open_scene_is_reused_not_duplicated(sessionmaker):
    """A scene opened but never written to (worker restart mid-turn) must
    not leak a fresh empty scene on the next message."""
    async with sessionmaker() as session:
        first = await ensure_open_scene(session, idle_hours=IDLE_HOURS)
    async with sessionmaker() as session:
        second = await ensure_open_scene(session, idle_hours=IDLE_HOURS)
        scenes = (await session.execute(select(Scene))).scalars().all()

    assert second == first
    assert len(scenes) == 1


async def test_get_open_scene_returns_none_when_all_closed(sessionmaker):
    async with sessionmaker() as session:
        scene = Scene(started_at=_utcnow(), ended_at=_utcnow())
        session.add(scene)
        await session.commit()
        assert await get_open_scene(session) is None


async def test_bump_message_count_accumulates(sessionmaker):
    async with sessionmaker() as session:
        scene_id = await ensure_open_scene(session, idle_hours=IDLE_HOURS)
        await _add_message(session, scene_id)
        await _add_message(session, scene_id, role="assistant")
        scene = await session.get(Scene, scene_id)
        await session.refresh(scene)

    assert scene.message_count == 2


# --- what the summarizer is allowed to see ---


async def test_welfare_and_ooc_and_canned_rows_are_excluded_from_summary_input(sessionmaker):
    """Plan sections 10 and 13: welfare content never reaches a summary.
    Both filters (ooc and kind) must exclude it independently, so that
    one of them failing is not enough to leak it."""
    async with sessionmaker() as session:
        scene_id = await ensure_open_scene(session, idle_hours=IDLE_HOURS)
        await _add_message(session, scene_id, content="обычная реплика")
        await _add_message(session, scene_id, content="чек-ин", kind="checkin")
        # Welfare: excluded by kind AND by ooc.
        await _add_message(session, scene_id, content="кризис", kind="welfare", ooc=True)
        # Welfare with the ooc flag wrongly unset: kind alone must still exclude it.
        await _add_message(session, scene_id, content="кризис2", kind="welfare", ooc=False)
        # Canned reply: excluded by kind.
        await _add_message(session, scene_id, content="канон", kind="canned", role="assistant")
        # Neutral-mode chat: excluded by ooc.
        await _add_message(session, scene_id, content="вне роли", kind="chat", ooc=True)

        visible = await summarizable_messages(session, scene_id)

    contents = [row.content for row in visible]
    assert contents == ["обычная реплика", "чек-ин"]
    assert not any("кризис" in c for c in contents)


async def test_render_dialogue_labels_roles_in_russian(sessionmaker):
    async with sessionmaker() as session:
        scene_id = await ensure_open_scene(session, idle_hours=IDLE_HOURS)
        await _add_message(session, scene_id, role="user", content="привет")
        await _add_message(session, scene_id, role="assistant", content="и тебе")
        rows = await summarizable_messages(session, scene_id)

    assert render_dialogue(rows) == "Пользователь: привет\nAnchor: и тебе"


# --- the job body ---


async def _scene_with(session, n: int, **kwargs) -> int:
    scene_id = await ensure_open_scene(session, idle_hours=IDLE_HOURS)
    for i in range(n):
        await _add_message(
            session,
            scene_id,
            role="user" if i % 2 == 0 else "assistant",
            content=f"реплика {i}",
            **kwargs,
        )
    return scene_id


async def test_scene_under_three_messages_is_not_summarized(sessionmaker):
    provider = FakeLLMProvider(text="сводка")
    async with sessionmaker() as session:
        scene_id = await _scene_with(session, MIN_MESSAGES_FOR_SUMMARY - 1)

    async with sessionmaker() as session:
        await run_summarize_scene(
            session, _settings(), provider, scene_id=scene_id, timezone=TIMEZONE
        )

    async with sessionmaker() as session:
        scene = await session.get(Scene, scene_id)

    assert provider.calls == 0
    assert scene.summary is None


async def test_scene_with_three_messages_is_summarized_and_ledgered(sessionmaker):
    provider = FakeLLMProvider(text="  Говорили об отчёте.  ", model="cydonia-fake")
    async with sessionmaker() as session:
        scene_id = await _scene_with(session, MIN_MESSAGES_FOR_SUMMARY)

    async with sessionmaker() as session:
        await run_summarize_scene(
            session, _settings(), provider, scene_id=scene_id, timezone=TIMEZONE
        )

    async with sessionmaker() as session:
        scene = await session.get(Scene, scene_id)
        ledger = (await session.execute(select(SpendLedger))).scalars().all()

    assert provider.calls == 1
    assert scene.summary == "Говорили об отчёте."
    assert len(ledger) == 1
    assert ledger[0].category == "summary"
    assert ledger[0].model == "cydonia-fake"


async def test_summary_prompt_is_a_system_message_and_dialogue_is_plain_text(sessionmaker):
    """A roleplay model handed a real transcript continues the roleplay
    instead of describing it, so the dialogue goes in as one plain-text
    user message, not as user/assistant turns."""
    provider = FakeLLMProvider(text="сводка")
    async with sessionmaker() as session:
        scene_id = await _scene_with(session, MIN_MESSAGES_FOR_SUMMARY)

    async with sessionmaker() as session:
        await run_summarize_scene(
            session, _settings(), provider, scene_id=scene_id, timezone=TIMEZONE
        )

    sent = provider.received_messages[0]
    assert [m.role for m in sent] == ["system", "user"]
    assert sent[0].content.startswith("Кратко (до 5 предложений)")
    assert "Пользователь: реплика 0" in sent[1].content


async def test_already_summarized_scene_is_a_no_op(sessionmaker):
    """A job can be re-claimed after a crash; re-running it must not pay
    for a second model call."""
    provider = FakeLLMProvider(text="сводка")
    async with sessionmaker() as session:
        scene_id = await _scene_with(session, MIN_MESSAGES_FOR_SUMMARY)

    for _ in range(2):
        async with sessionmaker() as session:
            await run_summarize_scene(
                session, _settings(), provider, scene_id=scene_id, timezone=TIMEZONE
            )

    async with sessionmaker() as session:
        ledger = (await session.execute(select(SpendLedger))).scalars().all()

    assert provider.calls == 1
    assert len(ledger) == 1


async def test_missing_scene_is_a_no_op(sessionmaker):
    provider = FakeLLMProvider(text="сводка")
    async with sessionmaker() as session:
        await run_summarize_scene(
            session, _settings(), provider, scene_id=999_999, timezone=TIMEZONE
        )
    assert provider.calls == 0


async def test_at_cap_the_summary_is_deferred_to_next_local_midnight(sessionmaker):
    """Plan section 12: at the cap, summaries are re-queued rather than
    run or dropped."""
    provider = FakeLLMProvider(text="сводка")
    settings = _settings(DAILY_USD_CAP=0.0)
    async with sessionmaker() as session:
        scene_id = await _scene_with(session, MIN_MESSAGES_FOR_SUMMARY)

    async with sessionmaker() as session:
        with pytest.raises(Deferred) as excinfo:
            await run_summarize_scene(
                session, settings, provider, scene_id=scene_id, timezone=TIMEZONE
            )

    assert provider.calls == 0
    assert excinfo.value.run_after == next_local_midnight(TIMEZONE)

    async with sessionmaker() as session:
        scene = await session.get(Scene, scene_id)
    assert scene.summary is None


async def test_next_local_midnight_is_the_next_local_day_boundary():
    import zoneinfo

    tz = zoneinfo.ZoneInfo(TIMEZONE)
    midnight = next_local_midnight(TIMEZONE)
    now_local = datetime.datetime.now(tz)

    assert midnight > now_local
    assert midnight.hour == 0 and midnight.minute == 0
    assert (midnight.date() - now_local.date()).days == 1


# --- integration with core/turn.py (plan section 5: "every message row
# gets scene_id") ---


async def _seed_state(sessionmaker, update_id: int, *, chat_id: int = 4242) -> None:
    from app.db.models import TelegramUpdate, UserState

    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=chat_id, timezone=TIMEZONE))
        await session.commit()
        session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()


async def test_a_turn_stamps_both_rows_with_the_scene_and_kind_chat(sessionmaker):
    from aiogram import Bot

    from app.core import turn
    from conftest import FakeSession

    update_id = 5001
    await _seed_state(sessionmaker, update_id)
    bot = Bot(token="123456:TESTTOKEN", session=FakeSession())
    provider = FakeLLMProvider(text="Принято.")

    await turn.run(
        sessionmaker,
        bot,
        _settings(),
        provider,
        chat_id=4242,
        update_id=update_id,
        user_text="привет",
    )

    async with sessionmaker() as session:
        rows = (await session.execute(select(Message).order_by(Message.id))).scalars().all()
        scenes = (await session.execute(select(Scene))).scalars().all()

    assert len(rows) == 2
    assert {row.role for row in rows} == {"user", "assistant"}
    assert all(row.scene_id == scenes[0].id for row in rows)
    assert all(row.kind == "chat" for row in rows)
    assert scenes[0].message_count == 2


async def test_a_replayed_turn_does_not_inflate_message_count(sessionmaker):
    """Both inserts are idempotent, so the count must follow the insert,
    not the attempt."""
    from aiogram import Bot

    from app.core import turn
    from conftest import FakeSession

    update_id = 5002
    await _seed_state(sessionmaker, update_id)
    provider = FakeLLMProvider(text="Принято.")

    for _ in range(2):
        bot = Bot(token="123456:TESTTOKEN", session=FakeSession())
        await turn.run(
            sessionmaker,
            bot,
            _settings(),
            provider,
            chat_id=4242,
            update_id=update_id,
            user_text="привет",
        )

    async with sessionmaker() as session:
        scenes = (await session.execute(select(Scene))).scalars().all()

    assert scenes[0].message_count == 2


async def test_a_pause_word_writes_a_canned_row_excluded_from_summaries(sessionmaker):
    """The canned pause acknowledgement must never reach a summary."""
    from aiogram import Bot

    from app.core import turn
    from conftest import FakeSession

    update_id = 5003
    await _seed_state(sessionmaker, update_id)
    bot = Bot(token="123456:TESTTOKEN", session=FakeSession())
    provider = FakeLLMProvider(text="не должно быть вызвано")

    await turn.run(
        sessionmaker,
        bot,
        _settings(),
        provider,
        chat_id=4242,
        update_id=update_id,
        user_text="пурпурный",
    )

    async with sessionmaker() as session:
        scenes = (await session.execute(select(Scene))).scalars().all()
        assistant = (
            await session.execute(select(Message).where(Message.role == "assistant"))
        ).scalar_one()
        visible = await summarizable_messages(session, scenes[0].id)

    assert provider.calls == 0
    assert assistant.kind == "canned"
    assert assistant.scene_id == scenes[0].id
    # The safeword itself is ooc, the acknowledgement is canned: neither
    # is summarizable, so the scene has nothing to summarize.
    assert visible == []

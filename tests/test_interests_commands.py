"""`/interests`, `/interests add <forums|guides|ref> <тема>`, and the
`it:x:<id>` callback (Phase 6 plan section 7, milestone 6d). Router-level
tests, same Dispatcher pattern as tests/test_orders_commands.py.
"""

from __future__ import annotations

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import select

from app.config import Settings
from app.core import interests
from app.research import jobs as research_jobs
from app.db.models import InterestTopic, Job, StudyJob, TelegramUpdate, UserState
from app.tg import research as research_ui
from app.tg import interests as interests_ui
from app.tg.router import BOT_COMMANDS, build_router
from conftest import FakeLLMProvider, FakeSession

pytestmark = pytest.mark.asyncio

TEST_CHAT_ID = 555
TIMEZONE = "Europe/Paris"
# `/interests run` needs research on; PACKET_GUIDES is empty by default,
# which is what makes a `guides` topic the unrunnable one.
RESEARCH_ON = {"RESEARCH_ENABLED": True, "DAILY_USD_CAP": 10.0}


def _command_update(update_id: int, text: str) -> dict:
    command_len = len(text.split(" ", 1)[0])
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": TEST_CHAT_ID, "type": "private"},
            "from": {"id": TEST_CHAT_ID, "is_bot": False, "first_name": "Test"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": command_len}],
        },
    }


def _callback_update(update_id: int, data: str, *, message_id: int = 900) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": TEST_CHAT_ID, "is_bot": False, "first_name": "Test"},
            "chat_instance": "ci",
            "data": data,
            "message": {
                "message_id": message_id,
                "date": 0,
                "chat": {"id": TEST_CHAT_ID, "type": "private"},
                "text": "…",
            },
        },
    }


def _build_dp(sessionmaker, settings: Settings | None = None):
    fake_session = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake_session)
    dp = Dispatcher()
    dp.include_router(
        build_router(sessionmaker, settings or Settings(), FakeLLMProvider(text="Принято."))
    )
    return dp, bot, fake_session


async def _seed(sessionmaker, *update_ids: int) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=TEST_CHAT_ID, timezone=TIMEZONE))
        await session.commit()
        for update_id in update_ids:
            session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()


async def _feed(dp, bot, payload: dict) -> None:
    await dp.feed_update(bot, Update.model_validate(payload, context={"bot": bot}))


async def test_interests_is_registered():
    assert any(c.command == "interests" for c in BOT_COMMANDS)


# --- /interests (list) -----------------------------------------------------


async def test_interests_list_is_empty(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/interests"))

    assert fake.sent[0].text == interests_ui.TOPICS_EMPTY


async def test_interests_list_shows_topics_with_remove_buttons(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    async with sessionmaker() as session:
        await interests.add_topic(session, Settings(), packet="forums", text="бессонница")
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(2, "/interests"))

    text = fake.sent[-1].text
    assert "[forums] бессонница" in text
    buttons = [b.text for r in fake.sent[-1].reply_markup.inline_keyboard for b in r]
    assert len(buttons) == 1
    assert buttons[0].startswith(interests_ui.REMOVE)


async def test_interests_list_shows_the_run_hint_only_when_research_is_on(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    async with sessionmaker() as session:
        await interests.add_topic(session, Settings(), packet="forums", text="бессонница")

    dp, bot, fake = _build_dp(sessionmaker, Settings(**RESEARCH_ON))
    await _feed(dp, bot, _command_update(1, "/interests"))
    assert fake.sent[-1].text.endswith(interests_ui.LIST_HINT)

    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=False))
    await _feed(dp, bot, _command_update(2, "/interests"))
    assert interests_ui.LIST_HINT not in fake.sent[-1].text


async def test_an_empty_list_has_no_run_hint(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(**RESEARCH_ON))

    await _feed(dp, bot, _command_update(1, "/interests"))

    assert fake.sent[0].text == interests_ui.TOPICS_EMPTY


# --- /interests add ---------------------------------------------------------


async def test_interests_add_creates_a_topic(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/interests add forums бессонница"))

    async with sessionmaker() as session:
        rows = (await session.execute(select(InterestTopic))).scalars().all()
    assert len(rows) == 1
    assert rows[0].text == "бессонница"
    assert rows[0].packet == "forums"
    assert "бессонница" in fake.sent[0].text


async def test_interests_add_with_no_args_explains_usage(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/interests add"))

    assert fake.sent[0].text == interests_ui.ADD_USAGE
    async with sessionmaker() as session:
        assert (await session.execute(select(InterestTopic))).scalars().all() == []


async def test_interests_add_with_only_a_packet_explains_usage(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/interests add forums"))

    assert fake.sent[0].text == interests_ui.ADD_USAGE


async def test_interests_add_refuses_an_unknown_packet(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/interests add whatever бессонница"))

    assert fake.sent[0].text == interests_ui.UNKNOWN_PACKET_REPLY
    async with sessionmaker() as session:
        assert (await session.execute(select(InterestTopic))).scalars().all() == []


async def test_interests_add_refuses_a_high_risk_topic(sessionmaker):
    """Plan section 7's own acceptance line: a `high` hit gets «Такое не ищу.»."""
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/interests add forums дозировка мелатонина"))

    assert fake.sent[0].text == interests.REFUSAL_TEXT
    async with sessionmaker() as session:
        assert (await session.execute(select(InterestTopic))).scalars().all() == []


async def test_interests_add_reports_the_cap(sessionmaker):
    await _seed(sessionmaker, *range(1, interests.MAX_ACTIVE_TOPICS + 2))
    dp, bot, fake = _build_dp(sessionmaker)
    for i, update_id in enumerate(range(1, interests.MAX_ACTIVE_TOPICS + 1)):
        await _feed(dp, bot, _command_update(update_id, f"/interests add forums тема{i}"))

    await _feed(
        dp, bot, _command_update(interests.MAX_ACTIVE_TOPICS + 1, "/interests add forums лишняя")
    )

    assert fake.sent[-1].text == interests.CAP_TEXT


# --- it:x: (the [✖] button) -------------------------------------------------


async def test_it_x_removes_and_rerenders_the_list(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/interests add forums бессонница"))
    await _feed(dp, bot, _command_update(2, "/interests"))
    async with sessionmaker() as session:
        row = (await session.execute(select(InterestTopic))).scalars().one()

    await _feed(dp, bot, _callback_update(3, f"it:x:{row.id}", message_id=len(fake.sent)))

    assert fake.edits[-1].text == interests_ui.TOPICS_EMPTY
    async with sessionmaker() as session:
        updated = await session.get(InterestTopic, row.id)
    assert updated.active is False


async def test_it_x_on_a_stale_id_is_answered(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, "it:x:999999"))

    assert fake.edits[-1].text == interests_ui.STALE


async def test_it_x_on_an_already_removed_topic_is_answered(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/interests add forums бессонница"))
    async with sessionmaker() as session:
        row = (await session.execute(select(InterestTopic))).scalars().one()

    await _feed(dp, bot, _callback_update(2, f"it:x:{row.id}", message_id=1))
    await _feed(dp, bot, _callback_update(3, f"it:x:{row.id}", message_id=2))

    assert fake.edits[-1].text == interests_ui.STALE


# --- /interests run ---------------------------------------------------------


async def _add(sessionmaker, text: str, packet: str = "forums") -> int:
    async with sessionmaker() as session:
        assert await interests.add_topic(session, Settings(), packet=packet, text=text) == "ok"
        rows = await interests.active_topics(session)
    return max(r.id for r in rows if r.text == text)


async def _jobs(sessionmaker) -> list[StudyJob]:
    async with sessionmaker() as session:
        return list((await session.execute(select(StudyJob).order_by(StudyJob.id))).scalars())


async def _queue_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        rows = await session.execute(select(Job).where(Job.kind == research_jobs.RESEARCH))
        return len(rows.scalars().all())


async def test_interests_run_is_answered_when_research_is_off(sessionmaker):
    await _seed(sessionmaker, 1)
    await _add(sessionmaker, "бессонница")
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=False))

    await _feed(dp, bot, _command_update(1, "/interests run"))

    assert fake.sent[0].text == research_ui.DISABLED
    assert await _jobs(sessionmaker) == []


@pytest.mark.parametrize(
    "tail",
    [
        "0",
        "abc",
        "1 2",
        "#",
        "-1",
        "²",
        "#0",
        "3 бессонница",
        # Past BIGINT: a pasted phone or card number gets the usage line,
        # not a DataError from the primary-key lookup.
        "99999999999999999999",
        str(2**63),
    ],
)
async def test_interests_run_with_a_bad_argument_explains_usage(sessionmaker, tail):
    await _seed(sessionmaker, 1)
    await _add(sessionmaker, "бессонница")
    dp, bot, fake = _build_dp(sessionmaker, Settings(**RESEARCH_ON))

    await _feed(dp, bot, _command_update(1, f"/interests run {tail}"))

    assert fake.sent[0].text == interests_ui.RUN_USAGE
    assert await _jobs(sessionmaker) == []


async def test_interests_run_with_no_topics_says_so(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(**RESEARCH_ON))

    await _feed(dp, bot, _command_update(1, "/interests run"))

    assert fake.sent[0].text == interests_ui.RUN_NO_TOPICS
    assert await _jobs(sessionmaker) == []


async def test_interests_run_queues_the_next_topic_and_names_it(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    await _add(sessionmaker, "первая")
    await _add(sessionmaker, "вторая", packet="ref")
    dp, bot, fake = _build_dp(sessionmaker, Settings(**RESEARCH_ON))

    await _feed(dp, bot, _command_update(1, "/interests run"))
    await _feed(dp, bot, _command_update(2, "/interests run"))

    assert fake.sent[0].text == interests_ui.RUN_ACCEPTED.format(text="первая", packet="forums")
    assert fake.sent[1].text == interests_ui.RUN_ACCEPTED.format(text="вторая", packet="ref")
    rows = await _jobs(sessionmaker)
    assert [(r.kind, r.query, r.packet, r.manual, r.status) for r in rows] == [
        ("study", "первая", "forums", True, "queued"),
        ("study", "вторая", "ref", True, "queued"),
    ]
    assert await _queue_count(sessionmaker) == 2


@pytest.mark.parametrize("form", ["{id}", "#{id}"])
async def test_interests_run_takes_a_number_as_the_list_prints_it(sessionmaker, form):
    await _seed(sessionmaker, 1)
    await _add(sessionmaker, "первая")
    wanted = await _add(sessionmaker, "вторая")
    dp, bot, fake = _build_dp(sessionmaker, Settings(**RESEARCH_ON))

    await _feed(dp, bot, _command_update(1, f"/interests run {form.format(id=wanted)}"))

    assert fake.sent[0].text == interests_ui.RUN_ACCEPTED.format(text="вторая", packet="forums")
    assert [r.query for r in await _jobs(sessionmaker)] == ["вторая"]


async def test_interests_run_on_an_unknown_or_retired_number_says_there_is_no_such_topic(
    sessionmaker,
):
    await _seed(sessionmaker, 1, 2)
    retired = await _add(sessionmaker, "снятая")
    async with sessionmaker() as session:
        assert await interests.retire(session, retired) == "ok"
    dp, bot, fake = _build_dp(sessionmaker, Settings(**RESEARCH_ON))

    await _feed(dp, bot, _command_update(1, f"/interests run {retired}"))
    await _feed(dp, bot, _command_update(2, "/interests run #99999"))

    assert [m.text for m in fake.sent] == [interests_ui.RUN_TOPIC_MISSING] * 2
    assert await _jobs(sessionmaker) == []


async def test_interests_run_on_a_packet_without_domains_names_the_packet(sessionmaker):
    await _seed(sessionmaker, 1)
    guides = await _add(sessionmaker, "тема с гидами", packet="guides")
    dp, bot, fake = _build_dp(sessionmaker, Settings(**RESEARCH_ON))

    await _feed(dp, bot, _command_update(1, f"/interests run {guides}"))

    assert fake.sent[0].text == research_ui.EMPTY_PACKET_TEMPLATE.format(packet="guides")
    assert await _jobs(sessionmaker) == []


async def test_interests_run_reports_its_own_exhausted_quota(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    await _add(sessionmaker, "бессонница")
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_MANUAL_PER_DAY=1, **RESEARCH_ON))

    await _feed(dp, bot, _command_update(1, "/interests run"))
    await _feed(dp, bot, _command_update(2, "/interests run"))

    assert fake.sent[1].text == interests_ui.RUN_QUOTA_EXHAUSTED
    assert len(await _jobs(sessionmaker)) == 1


async def test_interests_run_reports_the_spend_cap(sessionmaker):
    await _seed(sessionmaker, 1)
    await _add(sessionmaker, "бессонница")
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True, DAILY_USD_CAP=0.0))

    await _feed(dp, bot, _command_update(1, "/interests run"))

    assert fake.sent[0].text == research_ui.STUDY_REFUSALS[research_jobs.CAP]
    assert await _jobs(sessionmaker) == []


async def test_a_replayed_interests_run_queues_one_job_and_replies_once(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    await _add(sessionmaker, "первая")
    await _add(sessionmaker, "вторая")
    dp, bot, fake = _build_dp(sessionmaker, Settings(**RESEARCH_ON))

    await _feed(dp, bot, _command_update(1, "/interests run"))
    await _feed(dp, bot, _command_update(1, "/interests run"))

    assert len(fake.sent) == 1
    assert [r.query for r in await _jobs(sessionmaker)] == ["первая"]
    assert await _queue_count(sessionmaker) == 1
    # The replay did not stamp the second topic either: the next run still takes it.
    await _feed(dp, bot, _command_update(2, "/interests run"))
    assert [r.query for r in await _jobs(sessionmaker)] == ["первая", "вторая"]


async def test_it_x_keeps_the_run_hint_when_research_is_on(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker, Settings(**RESEARCH_ON))
    first = await _add(sessionmaker, "первая")
    await _add(sessionmaker, "вторая")
    await _feed(dp, bot, _command_update(1, "/interests"))

    await _feed(dp, bot, _callback_update(3, f"it:x:{first}", message_id=len(fake.sent)))

    assert fake.edits[-1].text.endswith(interests_ui.LIST_HINT)
    assert "первая" not in fake.edits[-1].text


async def test_it_x_leaves_the_hint_out_when_research_is_off(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=False))
    first = await _add(sessionmaker, "первая")
    await _add(sessionmaker, "вторая")

    await _feed(dp, bot, _callback_update(3, f"it:x:{first}", message_id=1))

    assert "вторая" in fake.edits[-1].text
    assert interests_ui.LIST_HINT not in fake.edits[-1].text

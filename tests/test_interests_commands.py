"""`/interests`, `/interests add <forums|guides|ref> <тема>`, and the
`it:x:<id>` callback (Phase 6 plan section 7, milestone 6d). Router-level
tests, same Dispatcher pattern as tests/test_orders_commands.py.

`/interests suggest` and its `is:` buttons (anchor-interest-suggestions-
plan.md section 8): the web chat and the off switch, the reply, the
re-send of an unsent batch, each tap, a replay, a stale epoch, a cap
refusal, `r` with the manual quota used; then the send hook's hold,
expiry and failure line. Every suggestion and reason is synthetic.
"""

from __future__ import annotations

import datetime
import functools

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import select

from app.config import Settings
from app.core import interests
from app.core.clock import FrozenClock
from app.research import jobs as research_jobs
from app.research import suggest
from app.db.models import (
    InterestSuggestBatch,
    InterestSuggestion,
    InterestTopic,
    Job,
    StudyJob,
    TelegramUpdate,
    UserState,
)
from app.tg import research as research_ui
from app.tg import interests as interests_ui
from app.tg.router import BOT_COMMANDS, WEB_ONLY_REPLY, build_router
from app.web import ingress
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


# --- /interests suggest (anchor-interest-suggestions-plan.md section 5) ------


SUGGEST_EPOCH = "k3f7qa"


def _now() -> datetime.datetime:
    """The router runs on the system clock, so a batch the command or a
    tap should see is made "now" by that clock."""
    return datetime.datetime.now(datetime.timezone.utc)


async def _suggest_batch(
    sessionmaker,
    *texts: str,
    created_at: datetime.datetime | None = None,
    origin: str = "manual",
    status: str = "done",
    error_code: str | None = None,
    sent: bool = False,
) -> tuple[int, list[int]]:
    """A finished batch with one open `forums` suggestion per text, in
    order. Synthetic texts only."""
    created_at = created_at or _now()
    async with sessionmaker() as session:
        batch = InterestSuggestBatch(
            origin=origin,
            status=status,
            error_code=error_code,
            local_date=created_at.date(),
            created_at=created_at,
            finished_at=created_at if status != "queued" else None,
            epoch=SUGGEST_EPOCH,
            sent_at=created_at if sent else None,
            message_id=900 if sent else None,
        )
        session.add(batch)
        await session.flush()
        rows = [
            InterestSuggestion(
                batch_id=batch.id, position=n, text=text, packet="forums",
                why=f"Синтетическая причина {n}.", status="open",
            )
            for n, text in enumerate(texts, start=1)
        ]
        session.add_all(rows)
        await session.commit()
        return batch.id, [row.id for row in rows]


async def _suggestion_row(sessionmaker, suggestion_id: int) -> InterestSuggestion:
    async with sessionmaker() as session:
        return await session.get(InterestSuggestion, suggestion_id)


async def _topics(sessionmaker) -> list[InterestTopic]:
    async with sessionmaker() as session:
        return list((await session.execute(select(InterestTopic).order_by(InterestTopic.id))).scalars())


async def _batches(sessionmaker) -> list[InterestSuggestBatch]:
    async with sessionmaker() as session:
        return list(
            (await session.execute(select(InterestSuggestBatch).order_by(InterestSuggestBatch.id))).scalars()
        )


def _buttons(markup) -> list[list[tuple[str, str]]]:
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


async def test_interests_suggest_on_the_web_chat_is_refused(sessionmaker):
    await _seed(sessionmaker, 1)
    await _add(sessionmaker, "бессонница")
    dp, bot, fake = _build_dp(sessionmaker, Settings(**RESEARCH_ON))
    bot.is_web_sink = True

    await _feed(dp, bot, _command_update(1, "/interests suggest"))

    assert fake.sent[-1].text == WEB_ONLY_REPLY
    assert await _batches(sessionmaker) == []


async def test_interests_suggest_is_answered_when_research_is_off(sessionmaker):
    await _seed(sessionmaker, 1)
    await _add(sessionmaker, "бессонница")
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=False))

    await _feed(dp, bot, _command_update(1, "/interests suggest"))

    assert fake.sent[-1].text == research_ui.DISABLED
    assert await _batches(sessionmaker) == []


async def test_interests_suggest_queues_a_batch_and_says_it_is_coming(sessionmaker):
    await _seed(sessionmaker, 1)
    await _add(sessionmaker, "бессонница")
    dp, bot, fake = _build_dp(sessionmaker, Settings(**RESEARCH_ON))

    await _feed(dp, bot, _command_update(1, "/interests suggest"))

    assert fake.sent[-1].text == interests_ui.SUGGEST_ACCEPTED
    (batch,) = await _batches(sessionmaker)
    assert (batch.origin, batch.status) == ("manual", "queued")
    async with sessionmaker() as session:
        jobs = (await session.execute(select(Job).where(Job.kind == suggest.INTEREST_SUGGEST))).scalars().all()
    assert [job.payload for job in jobs] == [{"batch_id": batch.id}]


async def test_a_replayed_interests_suggest_queues_one_batch_and_replies_once(sessionmaker):
    await _seed(sessionmaker, 1)
    await _add(sessionmaker, "бессонница")
    dp, bot, fake = _build_dp(sessionmaker, Settings(**RESEARCH_ON))

    await _feed(dp, bot, _command_update(1, "/interests suggest"))
    await _feed(dp, bot, _command_update(1, "/interests suggest"))

    assert len(await _batches(sessionmaker)) == 1
    assert [m.text for m in fake.sent].count(interests_ui.SUGGEST_ACCEPTED) == 1


@pytest.mark.parametrize(
    ("setup", "reply"),
    [
        ("nothing", interests_ui.SUGGEST_NOTHING),
        ("busy", interests_ui.SUGGEST_BUSY),
        ("quota", interests_ui.SUGGEST_QUOTA),
    ],
)
async def test_interests_suggest_refusals(sessionmaker, setup, reply):
    await _seed(sessionmaker, 1)
    if setup != "nothing":
        await _add(sessionmaker, "бессонница")
    if setup == "busy":
        await _suggest_batch(sessionmaker, status="queued")
    settings = Settings(**RESEARCH_ON, INTEREST_SUGGEST_PER_DAY=0 if setup == "quota" else 2)
    dp, bot, fake = _build_dp(sessionmaker, settings)

    await _feed(dp, bot, _command_update(1, "/interests suggest"))

    assert fake.sent[-1].text == reply


async def test_interests_suggest_resends_an_unsent_batch_instead_of_making_one(sessionmaker):
    await _seed(sessionmaker, 1)
    await _add(sessionmaker, "бессонница")
    batch_id, _ids = await _suggest_batch(sessionmaker, "гигиена сна", "режим дня")
    dp, bot, fake = _build_dp(sessionmaker, Settings(**RESEARCH_ON))

    await _feed(dp, bot, _command_update(1, "/interests suggest"))

    (message,) = fake.sent
    assert message.text.startswith(interests_ui.SUGGESTIONS_HEADER)
    assert "1. «гигиена сна» [forums] — Синтетическая причина 1." in message.text
    assert len(message.reply_markup.inline_keyboard) == 2
    (batch,) = await _batches(sessionmaker)
    assert batch.id == batch_id and batch.sent_at is not None
    assert batch.message_id == 1


async def test_the_suggestions_message_and_its_keyboard():
    view = suggest.BatchView(
        id=7, origin="manual", status="done", epoch=SUGGEST_EPOCH, message_id=None, sent_at=None,
        created_at=_now(),
        suggestions=tuple(
            suggest.SuggestionView(
                id=10 + n, position=n, text=f"тема {n}", packet="ref", why="почему", status=status
            )
            for n, status in enumerate(("open", "added", "dismissed", "expired"), start=1)
        ),
    )

    text = interests_ui.render_suggestions(view)
    assert text.splitlines() == [
        "Можно поискать:",
        "1. «тема 1» [ref] — почему",
        "2. «тема 2» [ref] — почему",
        "3. «тема 3» [ref] — почему",
        "4. «тема 4» [ref] — почему",
    ]
    assert _buttons(interests_ui.suggestions_keyboard(view)) == [
        [
            ("1 · добавить", f"is:a:11:{SUGGEST_EPOCH}"),
            ("1 · добавить и искать", f"is:r:11:{SUGGEST_EPOCH}"),
            ("1 · ✖", f"is:x:11:{SUGGEST_EPOCH}"),
        ],
        [("2 · ✓", f"is:a:12:{SUGGEST_EPOCH}")],
        [("3 · ✖", f"is:x:13:{SUGGEST_EPOCH}")],
    ]
    for row in interests_ui.suggestions_keyboard(view).inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= 64
            assert interests_ui.parse_suggestion_callback(button.callback_data) is not None


async def test_an_empty_batch_says_so_without_buttons():
    view = suggest.BatchView(
        id=7, origin="weekly", status="done", epoch=SUGGEST_EPOCH, message_id=None, sent_at=None,
        created_at=_now(), suggestions=(),
    )
    assert interests_ui.render_suggestions(view) == interests_ui.SUGGESTIONS_NONE
    assert interests_ui.suggestions_keyboard(view) is None


@pytest.mark.parametrize(
    "data",
    ["is:a:1", "is:q:1:k3f7qa", "is:a:0:k3f7qa", "is:a:1:K3F7QA", "is:a:1:k3f7qa\n", "is:a:99999999999:k3f7qa"],
)
async def test_a_malformed_suggestion_press_does_not_parse(data):
    assert interests_ui.parse_suggestion_callback(data) is None


async def test_tap_a_adds_the_topic_and_marks_the_row(sessionmaker):
    await _seed(sessionmaker)
    _batch, (first, second) = await _suggest_batch(sessionmaker, "гигиена сна", "режим дня")
    dp, bot, fake = _build_dp(sessionmaker, Settings(**RESEARCH_ON))

    await _feed(dp, bot, _callback_update(1, f"is:a:{first}:{SUGGEST_EPOCH}"))

    assert fake.answered[-1].text == "Добавлено: «гигиена сна»."
    (topic,) = await _topics(sessionmaker)
    assert (topic.text, topic.packet, topic.active) == ("гигиена сна", "forums", True)
    row = await _suggestion_row(sessionmaker, first)
    assert (row.status, row.topic_id) == ("added", topic.id)
    rows = _buttons(fake.edits[-1].reply_markup)
    assert rows[0] == [("1 · ✓", f"is:a:{first}:{SUGGEST_EPOCH}")]
    assert [text for text, _ in rows[1]] == ["2 · добавить", "2 · добавить и искать", "2 · ✖"]
    assert fake.sent == []  # edit only


async def test_tap_r_adds_the_topic_and_runs_it(sessionmaker):
    await _seed(sessionmaker)
    _batch, (first,) = await _suggest_batch(sessionmaker, "гигиена сна")
    dp, bot, fake = _build_dp(sessionmaker, Settings(**RESEARCH_ON))

    await _feed(dp, bot, _callback_update(1, f"is:r:{first}:{SUGGEST_EPOCH}"))

    assert fake.answered[-1].text == interests_ui.ADDED_AND_RUN_TOAST.format(text="гигиена сна")
    (topic,) = await _topics(sessionmaker)
    (job,) = await _jobs(sessionmaker)
    assert (job.query, job.packet, job.manual) == ("гигиена сна", "forums", True)
    assert topic.last_run_at is not None
    assert _buttons(fake.edits[-1].reply_markup) == [[("1 · ✓", f"is:a:{first}:{SUGGEST_EPOCH}")]]


async def test_tap_r_with_the_manual_quota_used_still_adds_the_topic(sessionmaker):
    await _seed(sessionmaker)
    _batch, (first,) = await _suggest_batch(sessionmaker, "гигиена сна")
    dp, bot, fake = _build_dp(sessionmaker, Settings(**RESEARCH_ON, RESEARCH_MANUAL_PER_DAY=0))

    await _feed(dp, bot, _callback_update(1, f"is:r:{first}:{SUGGEST_EPOCH}"))

    assert fake.answered[-1].text == (
        f"Добавлено: «гигиена сна». {interests_ui.RUN_QUOTA_EXHAUSTED}"
    )
    assert len(fake.answered[-1].text) <= 200
    assert [t.text for t in await _topics(sessionmaker)] == ["гигиена сна"]
    assert await _jobs(sessionmaker) == []
    assert (await _suggestion_row(sessionmaker, first)).status == "added"


async def test_tap_x_dismisses_the_suggestion(sessionmaker):
    await _seed(sessionmaker)
    _batch, (first,) = await _suggest_batch(sessionmaker, "гигиена сна")
    dp, bot, fake = _build_dp(sessionmaker, Settings(**RESEARCH_ON))

    await _feed(dp, bot, _callback_update(1, f"is:x:{first}:{SUGGEST_EPOCH}"))

    assert fake.answered[-1].text == interests_ui.DISMISSED_TOAST
    assert await _topics(sessionmaker) == []
    assert (await _suggestion_row(sessionmaker, first)).status == "dismissed"
    assert _buttons(fake.edits[-1].reply_markup) == [[("1 · ✖", f"is:x:{first}:{SUGGEST_EPOCH}")]]


async def test_a_replayed_tap_is_stale_and_leaves_one_topic(sessionmaker):
    await _seed(sessionmaker)
    _batch, (first,) = await _suggest_batch(sessionmaker, "гигиена сна")
    dp, bot, fake = _build_dp(sessionmaker, Settings(**RESEARCH_ON))

    await _feed(dp, bot, _callback_update(1, f"is:a:{first}:{SUGGEST_EPOCH}"))
    await _feed(dp, bot, _callback_update(2, f"is:r:{first}:{SUGGEST_EPOCH}"))

    assert fake.answered[-1].text == interests_ui.STALE
    assert len(await _topics(sessionmaker)) == 1
    assert await _jobs(sessionmaker) == []
    # The stale tap re-renders the truth: still the one ✓ row.
    assert _buttons(fake.edits[-1].reply_markup) == [[("1 · ✓", f"is:a:{first}:{SUGGEST_EPOCH}")]]


async def test_a_tap_with_a_stale_epoch_changes_nothing_and_drops_the_keyboard(sessionmaker):
    await _seed(sessionmaker)
    _batch, (first,) = await _suggest_batch(sessionmaker, "гигиена сна")
    dp, bot, fake = _build_dp(sessionmaker, Settings(**RESEARCH_ON))

    await _feed(dp, bot, _callback_update(1, f"is:a:{first}:abcdef"))

    assert fake.answered[-1].text == interests_ui.STALE
    assert await _topics(sessionmaker) == []
    assert (await _suggestion_row(sessionmaker, first)).status == "open"
    assert fake.edits == []
    assert fake.markup_edits[-1].reply_markup is None


async def test_a_tap_on_an_expired_suggestion_is_stale_and_its_row_goes(sessionmaker):
    await _seed(sessionmaker)
    _batch, (first, second) = await _suggest_batch(sessionmaker, "гигиена сна", "режим дня")
    async with sessionmaker() as session:
        await suggest.expire_open(session, _now())
        await session.commit()
    dp, bot, fake = _build_dp(sessionmaker, Settings(**RESEARCH_ON))

    await _feed(dp, bot, _callback_update(1, f"is:a:{first}:{SUGGEST_EPOCH}"))

    assert fake.answered[-1].text == interests_ui.STALE
    assert fake.edits[-1].reply_markup is None
    assert await _topics(sessionmaker) == []


async def test_a_malformed_tap_is_stale(sessionmaker):
    await _seed(sessionmaker)
    dp, bot, fake = _build_dp(sessionmaker, Settings(**RESEARCH_ON))

    await _feed(dp, bot, _callback_update(1, "is:a:nope"))

    assert fake.answered[-1].text == interests_ui.STALE
    assert fake.edits == []


async def test_a_cap_refusal_keeps_the_suggestion_open(sessionmaker):
    await _seed(sessionmaker)
    for n in range(interests.MAX_ACTIVE_TOPICS):
        await _add(sessionmaker, f"тема номер {n}")
    _batch, (first,) = await _suggest_batch(sessionmaker, "гигиена сна")
    dp, bot, fake = _build_dp(sessionmaker, Settings(**RESEARCH_ON))

    await _feed(dp, bot, _callback_update(1, f"is:r:{first}:{SUGGEST_EPOCH}"))

    assert fake.answered[-1].text == interests.CAP_TEXT
    row = await _suggestion_row(sessionmaker, first)
    assert (row.status, row.decided_at, row.topic_id) == ("open", None, None)
    assert len(await _topics(sessionmaker)) == interests.MAX_ACTIVE_TOPICS
    assert await _jobs(sessionmaker) == []
    assert fake.edits == []


async def test_a_suggestion_press_through_the_web_sink_is_refused(sessionmaker):
    await _seed(sessionmaker)
    _batch, (first,) = await _suggest_batch(sessionmaker, "гигиена сна")
    dp, bot, fake = _build_dp(sessionmaker, Settings(**RESEARCH_ON))
    bot.is_web_sink = True

    await _feed(dp, bot, _callback_update(1, f"is:a:{first}:{SUGGEST_EPOCH}"))

    assert fake.answered[-1].text == WEB_ONLY_REPLY
    assert await _topics(sessionmaker) == []
    assert (await _suggestion_row(sessionmaker, first)).status == "open"


async def test_ingress_blocks_the_suggestion_buttons():
    for data in ("is:a:1:k3f7qa", "is:r:1:k3f7qa", "is:x:1:k3f7qa"):
        assert data.startswith(ingress.BLOCKED_CALLBACK_PREFIX)


# --- the send hook --------------------------------------------------------------


# 14:00 in Paris, outside the default quiet hours; and 02:00, inside.
DAY = datetime.datetime(2026, 10, 24, 12, 0, tzinfo=datetime.timezone.utc)
NIGHT = datetime.datetime(2026, 10, 24, 0, 0, tzinfo=datetime.timezone.utc)


async def test_the_send_hook_holds_a_batch_in_quiet_hours_and_sends_it_after(sessionmaker):
    await _seed(sessionmaker)
    batch_id, _ids = await _suggest_batch(sessionmaker, "гигиена сна", created_at=NIGHT)
    settings = Settings(**RESEARCH_ON)
    bot, fake = _build_dp(sessionmaker, settings)[1:]

    assert not await interests_ui.send_suggestions(sessionmaker, bot, settings, FrozenClock(NIGHT))
    assert fake.sent == []

    assert await interests_ui.send_suggestions(sessionmaker, bot, settings, FrozenClock(DAY))
    (message,) = fake.sent
    assert message.chat_id == TEST_CHAT_ID
    assert "«гигиена сна»" in message.text
    (batch,) = await _batches(sessionmaker)
    assert (batch.message_id, batch.sent_at) == (1, DAY)

    # Sent once: the next tick finds nothing waiting.
    assert not await interests_ui.send_suggestions(sessionmaker, bot, settings, FrozenClock(DAY))
    assert len(fake.sent) == 1


async def test_the_send_hook_sends_nothing_while_research_is_off(sessionmaker):
    await _seed(sessionmaker)
    await _suggest_batch(sessionmaker, "гигиена сна", created_at=DAY)
    settings = Settings(RESEARCH_ENABLED=False)
    bot, fake = _build_dp(sessionmaker, settings)[1:]

    assert not await interests_ui.send_suggestions(sessionmaker, bot, settings, FrozenClock(DAY))
    assert fake.sent == []


async def test_an_empty_batch_is_sent_as_one_line(sessionmaker):
    await _seed(sessionmaker)
    await _suggest_batch(sessionmaker, created_at=DAY)
    settings = Settings(**RESEARCH_ON)
    bot, fake = _build_dp(sessionmaker, settings)[1:]

    assert await interests_ui.send_suggestions(sessionmaker, bot, settings, FrozenClock(DAY))
    (message,) = fake.sent
    assert message.text == interests_ui.SUGGESTIONS_NONE
    assert message.reply_markup is None


async def test_a_batch_unsent_for_48_hours_expires(sessionmaker):
    await _seed(sessionmaker)
    _batch, (first,) = await _suggest_batch(sessionmaker, "гигиена сна", created_at=DAY)
    settings = Settings(**RESEARCH_ON)
    bot, fake = _build_dp(sessionmaker, settings)[1:]
    later = DAY + suggest.UNSENT_TTL + datetime.timedelta(minutes=1)

    assert not await interests_ui.send_suggestions(sessionmaker, bot, settings, FrozenClock(later))
    assert fake.sent == []
    row = await _suggestion_row(sessionmaker, first)
    assert row.status == "expired"


async def test_a_failed_manual_batch_sends_one_line_and_a_weekly_one_nothing(sessionmaker):
    await _seed(sessionmaker)
    manual, _ = await _suggest_batch(
        sessionmaker, created_at=DAY, status="failed", error_code=suggest.PROVIDER_ERROR
    )
    weekly, _ = await _suggest_batch(
        sessionmaker, created_at=DAY, origin="weekly", status="failed",
        error_code=suggest.PROVIDER_ERROR,
    )
    interrupted, _ = await _suggest_batch(
        sessionmaker, created_at=DAY, status="failed", error_code=suggest.INTERRUPTED
    )
    settings = Settings(**RESEARCH_ON)
    bot, fake = _build_dp(sessionmaker, settings)[1:]
    clock = FrozenClock(DAY)

    assert await interests_ui.send_batch_failed(sessionmaker, bot, settings, clock, batch_id=manual)
    assert not await interests_ui.send_batch_failed(sessionmaker, bot, settings, clock, batch_id=weekly)
    assert not await interests_ui.send_batch_failed(
        sessionmaker, bot, settings, clock, batch_id=interrupted
    )
    assert [m.text for m in fake.sent] == [interests_ui.SUGGEST_FAILED]


async def test_the_worker_sends_the_failure_line_after_a_failed_manual_batch(
    sessionmaker, monkeypatch
):
    from app import worker as worker_module
    from app.llm.provider import LLMError

    await _seed(sessionmaker)
    await _add(sessionmaker, "бессонница")
    settings = Settings(**RESEARCH_ON)
    async with sessionmaker() as session:
        queued = await suggest.enqueue_suggest(
            session, settings, FrozenClock(DAY), timezone=TIMEZONE
        )
    monkeypatch.setattr(
        worker_module,
        "run_suggest_batch",
        functools.partial(
            suggest.run_batch, provider=FakeLLMProvider(raises=[LLMError("down")])
        ),
    )
    bot, fake = _build_dp(sessionmaker, settings)[1:]

    assert await worker_module.process_one_job(
        sessionmaker, settings, FakeLLMProvider(), FrozenClock(DAY), bot
    )

    (batch,) = await _batches(sessionmaker)
    assert (batch.id, batch.status) == (queued.batch_id, "failed")
    assert [m.text for m in fake.sent] == [interests_ui.SUGGEST_FAILED]


async def test_the_menu_offers_suggestions_only_with_research_on_and_off_the_web():
    from app.tg import menu

    assert menu.action_available("interests_suggest", Settings(**RESEARCH_ON), web=False)
    assert not menu.action_available("interests_suggest", Settings(**RESEARCH_ON), web=True)
    assert not menu.action_available(
        "interests_suggest", Settings(RESEARCH_ENABLED=False), web=False
    )

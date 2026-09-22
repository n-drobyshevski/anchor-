"""The research commands and callbacks (phase-4 plan sections 9, 12).

Router-level tests, following tests/test_memory_commands.py's shape:
real Update payloads through build_router()'s dispatcher, so the
command filters, callback filters and refusal checks all run as they
actually do in production. app/core/cards.py's own behaviour (adopt/
reject logic, the dedupe-linking fallback, the FORBIDDEN/GONE split) is
covered in depth in tests/test_cards.py; this file is about the
Telegram surface: exact refusal wording, idempotency, and that the
buttons produce what the commands produce.

RESEARCH_ENABLED defaults to false (plan section 3), so every test that
exercises a real path builds `Settings(RESEARCH_ENABLED=True)`
explicitly -- the disabled-by-default tests are the one place that
default is left alone.
"""

from __future__ import annotations

import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import select

from app.config import Settings
from app.core.clock import FrozenClock
from app.db.models import Job, Memory, StudyCard, StudyClip, StudyJob, TelegramUpdate, UserState
from app.research import jobs as research_jobs
from app.tg import research as research_ui
from app.tg.router import BOT_COMMANDS, build_router
from conftest import FakeLLMProvider, FakeSession

pytestmark = pytest.mark.asyncio

TEST_CHAT_ID = 555
TIMEZONE = "Europe/Paris"
READ_URL = "https://example.com/sleep-article"


def _clock() -> FrozenClock:
    return FrozenClock(datetime.datetime(2026, 9, 22, 12, 0, tzinfo=datetime.timezone.utc))


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


def _build_dp(sessionmaker, settings: Settings, clock=None) -> tuple[Dispatcher, Bot, FakeSession]:
    fake_session = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake_session)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, settings, FakeLLMProvider(), clock=clock))
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


async def _seed_card(sessionmaker, **overrides) -> StudyCard:
    async with sessionmaker() as session:
        job = StudyJob(kind="read", local_date=datetime.date(2026, 9, 22), status="done")
        session.add(job)
        await session.flush()
        clip = StudyClip(job_id=job.id, url=READ_URL, domain="example.com", text="текст")
        session.add(clip)
        await session.flush()
        fields = dict(
            job_id=job.id,
            clip_id=clip.id,
            kind="technique",
            text="Ложиться спать в одно и то же время.",
            quote="Ложитесь спать в одно и то же время каждый день, даже по выходным.",
            source_url=clip.url,
            risk_model="low",
            risk_rules="low",
            risk_final="low",
            status="pending",
        )
        fields.update(overrides)
        card = StudyCard(**fields)
        session.add(card)
        await session.commit()
        await session.refresh(card)
        return card


# --- disabled everywhere ---------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["/study forums бессонница", "/read " + READ_URL, "/notes", "/card 1", "/adopt 1", "/reject 1"],
    ids=["study", "read", "notes", "card", "adopt", "reject"],
)
async def test_every_research_command_refuses_when_disabled(sessionmaker, text):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=False))

    await _feed(dp, bot, _command_update(1, text))

    assert fake.sent[0].text == research_ui.DISABLED


async def test_the_research_commands_are_registered_for_telegram():
    names = {command.command for command in BOT_COMMANDS}
    assert {"study", "read", "notes", "card", "adopt", "reject"} <= names


# --- /study ----------------------------------------------------------------


async def test_study_without_any_args_explains_usage(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True))

    await _feed(dp, bot, _command_update(1, "/study"))

    assert fake.sent[0].text == research_ui.STUDY_USAGE
    async with sessionmaker() as session:
        assert (await session.execute(select(StudyJob))).scalars().all() == []


async def test_study_with_a_packet_but_no_topic_explains_usage(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True))

    await _feed(dp, bot, _command_update(1, "/study forums"))

    assert fake.sent[0].text == research_ui.STUDY_USAGE
    async with sessionmaker() as session:
        assert (await session.execute(select(StudyJob))).scalars().all() == []


@pytest.mark.parametrize("packet", ["forums", "ref"])
async def test_study_queues_a_job_for_each_shipped_packet(sessionmaker, packet):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True))

    await _feed(dp, bot, _command_update(1, f"/study {packet} бессонница"))

    assert fake.sent[0].text == research_ui.STUDY_ACCEPTED
    async with sessionmaker() as session:
        jobs = (await session.execute(select(StudyJob))).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].kind == "study"
    assert jobs[0].packet == packet
    assert jobs[0].query == "бессонница"


async def test_study_queues_a_job_for_guides_once_configured(sessionmaker):
    await _seed(sessionmaker, 1)
    settings = Settings(RESEARCH_ENABLED=True, PACKET_GUIDES="example.com")
    dp, bot, fake = _build_dp(sessionmaker, settings)

    await _feed(dp, bot, _command_update(1, "/study guides сон"))

    assert fake.sent[0].text == research_ui.STUDY_ACCEPTED
    async with sessionmaker() as session:
        jobs = (await session.execute(select(StudyJob))).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].kind == "study"
    assert jobs[0].packet == "guides"
    assert jobs[0].query == "сон"


async def test_study_with_an_unknown_packet_is_refused(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True))

    await _feed(dp, bot, _command_update(1, "/study музыка джаз"))

    assert fake.sent[0].text == "Пакеты: forums, guides, ref."
    async with sessionmaker() as session:
        assert (await session.execute(select(StudyJob))).scalars().all() == []


async def test_study_guides_is_refused_while_unconfigured(sessionmaker):
    """PACKET_GUIDES defaults to empty (app/config.py); the default
    Settings() used everywhere else in this file is the unconfigured case."""
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True))

    await _feed(dp, bot, _command_update(1, "/study guides сон"))

    assert fake.sent[0].text == "Пакет guides пока не настроен."
    async with sessionmaker() as session:
        assert (await session.execute(select(StudyJob))).scalars().all() == []


async def test_study_refuses_a_topic_over_two_hundred_characters(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True))
    topic = "а" * 201

    await _feed(dp, bot, _command_update(1, f"/study forums {topic}"))

    assert fake.sent[0].text == research_ui.STUDY_TOPIC_TOO_LONG
    async with sessionmaker() as session:
        assert (await session.execute(select(StudyJob))).scalars().all() == []


async def test_study_refuses_once_the_daily_job_quota_is_used(sessionmaker):
    settings = Settings(RESEARCH_ENABLED=True, RESEARCH_JOBS_PER_DAY=1)
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker, settings, clock=_clock())

    await _feed(dp, bot, _command_update(1, "/study forums бессонница"))
    await _feed(dp, bot, _command_update(2, "/study ref сон"))

    assert fake.sent[0].text == research_ui.STUDY_ACCEPTED
    assert fake.sent[1].text == research_ui.QUOTA_EXHAUSTED
    async with sessionmaker() as session:
        jobs = (await session.execute(select(StudyJob))).scalars().all()
    assert len(jobs) == 1


async def test_a_replayed_study_queues_exactly_one_job(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True))

    for _ in range(2):
        await _feed(dp, bot, _command_update(1, "/study forums бессонница"))

    assert len(fake.sent) == 1
    async with sessionmaker() as session:
        jobs = (await session.execute(select(StudyJob))).scalars().all()
    assert len(jobs) == 1


async def test_using_up_the_read_quota_does_not_block_study(sessionmaker):
    """RESEARCH_JOBS_PER_DAY and RESEARCH_READS_PER_DAY are separate
    quotas (app/research/jobs.enqueue_study's docstring); exhausting one
    kind must not refuse the other."""
    settings = Settings(RESEARCH_ENABLED=True, RESEARCH_READS_PER_DAY=1)
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker, settings, clock=_clock())

    await _feed(dp, bot, _command_update(1, f"/read {READ_URL}"))
    await _feed(dp, bot, _command_update(2, "/study forums бессонница"))

    assert fake.sent[0].text == research_ui.READ_ACCEPTED
    assert fake.sent[1].text == research_ui.STUDY_ACCEPTED
    async with sessionmaker() as session:
        jobs = (await session.execute(select(StudyJob))).scalars().all()
    assert {job.kind for job in jobs} == {"read", "study"}


async def test_using_up_the_study_quota_does_not_block_read(sessionmaker):
    settings = Settings(RESEARCH_ENABLED=True, RESEARCH_JOBS_PER_DAY=1)
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker, settings, clock=_clock())

    await _feed(dp, bot, _command_update(1, "/study forums бессонница"))
    await _feed(dp, bot, _command_update(2, f"/read {READ_URL}"))

    assert fake.sent[0].text == research_ui.STUDY_ACCEPTED
    assert fake.sent[1].text == research_ui.READ_ACCEPTED
    async with sessionmaker() as session:
        jobs = (await session.execute(select(StudyJob))).scalars().all()
    assert {job.kind for job in jobs} == {"study", "read"}


# --- /read -------------------------------------------------------------


async def test_read_without_a_url_explains_usage(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True))

    await _feed(dp, bot, _command_update(1, "/read"))

    assert fake.sent[0].text == research_ui.READ_USAGE
    async with sessionmaker() as session:
        assert (await session.execute(select(StudyJob))).scalars().all() == []


async def test_read_queues_a_job_and_replies_reading(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True))

    await _feed(dp, bot, _command_update(1, f"/read {READ_URL}"))

    assert fake.sent[0].text == research_ui.READ_ACCEPTED
    async with sessionmaker() as session:
        jobs = (await session.execute(select(StudyJob))).scalars().all()
        queued = (await session.execute(select(Job).where(Job.kind == research_jobs.RESEARCH))).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].kind == "read"
    assert jobs[0].status == "queued"
    # study_job.query is left null for a /read job -- the URL travels in
    # the queue row's JSONB payload instead (app/research/jobs.py's own
    # docstring on why: a 200-char column is too small for a real link).
    assert len(queued) == 1
    assert queued[0].payload == {"job_id": jobs[0].id, "url": READ_URL}


async def test_read_with_a_malformed_url_is_refused(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True))

    await _feed(dp, bot, _command_update(1, "/read not-a-url"))

    assert fake.sent[0].text == research_ui.READ_REFUSALS[research_jobs.BAD_URL]
    async with sessionmaker() as session:
        assert (await session.execute(select(StudyJob))).scalars().all() == []


async def test_read_refuses_once_the_daily_quota_is_used(sessionmaker):
    settings = Settings(RESEARCH_ENABLED=True, RESEARCH_READS_PER_DAY=1)
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker, settings, clock=_clock())

    await _feed(dp, bot, _command_update(1, f"/read {READ_URL}"))
    await _feed(dp, bot, _command_update(2, "/read https://example.com/other"))

    assert fake.sent[0].text == research_ui.READ_ACCEPTED
    assert fake.sent[1].text == research_ui.QUOTA_EXHAUSTED
    async with sessionmaker() as session:
        jobs = (await session.execute(select(StudyJob))).scalars().all()
    assert len(jobs) == 1


async def test_a_replayed_read_queues_exactly_one_job(sessionmaker):
    """The worker re-runs an update after a crash or the stuck sweep
    (app/tg/memory.py's docstring); enqueue_read is not idempotent on its
    own, so the router's _once gate has to be what prevents a second
    study_job row."""
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True))

    for _ in range(2):
        await _feed(dp, bot, _command_update(1, f"/read {READ_URL}"))

    assert len(fake.sent) == 1
    async with sessionmaker() as session:
        jobs = (await session.execute(select(StudyJob))).scalars().all()
    assert len(jobs) == 1


# --- /notes --------------------------------------------------------------


async def test_notes_empty(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True))

    await _feed(dp, bot, _command_update(1, "/notes"))

    assert fake.sent[0].text == research_ui.NOTES_EMPTY
    assert fake.sent[0].reply_markup is None


async def test_notes_lists_a_card_and_never_a_hidden_one(sessionmaker):
    await _seed_card(sessionmaker, text="Видимая техника.")
    await _seed_card(
        sessionmaker,
        text="Спрятанная техника.",
        risk_model="high",
        risk_rules="low",
        risk_final="high",
        status="hidden",
    )
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True))

    await _feed(dp, bot, _command_update(1, "/notes"))

    text = fake.sent[0].text
    assert "Видимая техника." in text
    assert "Спрятанная техника." not in text
    assert "Источник: example.com" in text
    assert "«Ложитесь спать" in text

    labels = [b.text for row in fake.sent[0].reply_markup.inline_keyboard for b in row]
    assert labels == [research_ui.ACCEPT, research_ui.REJECT_LABEL]


async def test_notes_pages_and_a_decision_resets_to_the_first_page(sessionmaker):
    for i in range(7):
        await _seed_card(sessionmaker, text=f"Карточка {i}.")
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True))

    await _feed(dp, bot, _command_update(1, "/notes"))
    arrows = [b.text for row in fake.sent[0].reply_markup.inline_keyboard[-1:] for b in row]
    assert arrows == ["›"], "no back arrow on the first page"

    # FakeSession assigns message ids sequentially from 1; /notes sent
    # exactly one message above, so this is it.
    await _feed(dp, bot, _callback_update(2, "r:p:1", message_id=1))
    assert len(fake.edits) == 1
    arrows = [b.text for row in fake.edits[0].reply_markup.inline_keyboard[-1:] for b in row]
    assert arrows == ["‹"], "no forward arrow on the last page"


# --- /card -----------------------------------------------------------------


async def test_card_shows_the_full_source_url(sessionmaker):
    card = await _seed_card(sessionmaker)
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True))

    await _feed(dp, bot, _command_update(1, f"/card {card.id}"))

    assert fake.sent[0].text == research_ui.render_card_detail(card)
    assert READ_URL in fake.sent[0].text


async def test_card_on_a_hidden_id_behaves_as_if_it_does_not_exist(sessionmaker):
    card = await _seed_card(
        sessionmaker, risk_model="high", risk_rules="low", risk_final="high", status="hidden"
    )
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True))

    await _feed(dp, bot, _command_update(1, f"/card {card.id}"))

    assert fake.sent[0].text == research_ui.CARD_MISSING


async def test_card_on_a_missing_id_says_so(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True))

    await _feed(dp, bot, _command_update(1, "/card 999999"))

    assert fake.sent[0].text == research_ui.CARD_MISSING


async def test_card_without_an_id_explains_usage(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True))

    await _feed(dp, bot, _command_update(1, "/card"))

    assert fake.sent[0].text == research_ui.CARD_USAGE


# --- /adopt and /reject ------------------------------------------------


async def test_adopt_writes_a_memory_and_replies(sessionmaker):
    card = await _seed_card(sessionmaker)
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True), clock=_clock())

    await _feed(dp, bot, _command_update(1, f"/adopt {card.id}"))

    assert fake.sent[0].text == research_ui.ADOPT_REPLY
    async with sessionmaker() as session:
        memories = (await session.execute(select(Memory))).scalars().all()
    assert len(memories) == 1
    assert memories[0].kind == "technique"


async def test_adopting_twice_is_idempotent_and_writes_one_memory(sessionmaker):
    card = await _seed_card(sessionmaker)
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True), clock=_clock())

    await _feed(dp, bot, _command_update(1, f"/adopt {card.id}"))
    await _feed(dp, bot, _command_update(2, f"/adopt {card.id}"))

    assert fake.sent[0].text == research_ui.ADOPT_REPLY
    assert fake.sent[1].text == research_ui.ADOPT_REPLY
    async with sessionmaker() as session:
        memories = (await session.execute(select(Memory))).scalars().all()
    assert len(memories) == 1


async def test_adopting_a_hidden_card_is_refused_via_the_command(sessionmaker):
    card = await _seed_card(
        sessionmaker, risk_model="high", risk_rules="low", risk_final="high", status="hidden"
    )
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True))

    await _feed(dp, bot, _command_update(1, f"/adopt {card.id}"))

    assert fake.sent[0].text == research_ui.CARD_MISSING
    async with sessionmaker() as session:
        assert (await session.execute(select(Memory))).scalars().all() == []


async def test_adopt_without_an_id_explains_usage(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True))

    await _feed(dp, bot, _command_update(1, "/adopt"))

    assert fake.sent[0].text == research_ui.ADOPT_USAGE


async def test_reject_replies_and_writes_no_memory(sessionmaker):
    card = await _seed_card(sessionmaker)
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True), clock=_clock())

    await _feed(dp, bot, _command_update(1, f"/reject {card.id}"))

    assert fake.sent[0].text == research_ui.REJECT_REPLY
    async with sessionmaker() as session:
        assert (await session.execute(select(Memory))).scalars().all() == []
        refreshed = await session.get(StudyCard, card.id)
    assert refreshed.status == "rejected"


async def test_rejecting_twice_is_idempotent(sessionmaker):
    card = await _seed_card(sessionmaker)
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True), clock=_clock())

    await _feed(dp, bot, _command_update(1, f"/reject {card.id}"))
    await _feed(dp, bot, _command_update(2, f"/reject {card.id}"))

    assert fake.sent[0].text == research_ui.REJECT_REPLY
    assert fake.sent[1].text == research_ui.REJECT_REPLY


# --- the buttons match the commands -------------------------------------


async def test_the_accept_button_writes_the_same_memory_the_command_would(sessionmaker):
    card = await _seed_card(sessionmaker)
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True), clock=_clock())

    await _feed(dp, bot, _command_update(1, "/notes"))
    await _feed(dp, bot, _callback_update(2, f"r:a:{card.id}", message_id=1))

    assert len(fake.answered) == 1
    assert fake.answered[0].text == research_ui.ADOPT_REPLY
    async with sessionmaker() as session:
        memories = (await session.execute(select(Memory))).scalars().all()
        refreshed = await session.get(StudyCard, card.id)
    assert len(memories) == 1
    assert refreshed.status == "adopted"
    # The list message is refreshed in place, not replaced by a new send.
    assert len(fake.sent) == 1
    assert len(fake.edits) == 1
    assert fake.edits[0].text == research_ui.NOTES_EMPTY


async def test_the_reject_button_matches_the_reject_command(sessionmaker):
    a = await _seed_card(sessionmaker, text="Карточка A.")
    b = await _seed_card(sessionmaker, text="Карточка B.")
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True), clock=_clock())

    await _feed(dp, bot, _command_update(1, "/notes"))
    await _feed(dp, bot, _callback_update(2, f"r:r:{a.id}", message_id=1))

    assert fake.answered[0].text == research_ui.REJECT_REPLY
    async with sessionmaker() as session:
        refreshed_a = await session.get(StudyCard, a.id)
    assert refreshed_a.status == "rejected"
    # b is still pending and still shown after the refresh.
    assert b.text in fake.edits[0].text


async def test_a_button_on_a_hidden_card_is_forbidden_like_the_command(sessionmaker):
    card = await _seed_card(
        sessionmaker, risk_model="high", risk_rules="low", risk_final="high", status="hidden"
    )
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True))

    await _feed(dp, bot, _callback_update(1, f"r:a:{card.id}", message_id=1))

    assert fake.answered[0].text == research_ui.CARD_MISSING
    async with sessionmaker() as session:
        refreshed = await session.get(StudyCard, card.id)
        assert (await session.execute(select(Memory))).scalars().all() == []
    assert refreshed.status == "hidden"


async def test_an_unparseable_decision_callback_is_answered_stale(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings(RESEARCH_ENABLED=True))

    await _feed(dp, bot, _callback_update(1, "r:a:not-a-number", message_id=1))

    assert fake.answered[0].text == research_ui.STALE


# --- completion text (used by app/worker.py, not sent from here) -----------


async def test_completion_text_for_cards_found():
    assert research_ui.completion_text(
        status="done", error_code=None, visible_cards=3
    ) == "Готово: 3 карточки. /notes"


async def test_completion_text_for_nothing_found():
    assert (
        research_ui.completion_text(status="done", error_code=None, visible_cards=0)
        == research_ui.NOTHING_FOUND_TEXT
    )


async def test_completion_text_for_a_known_failure_code():
    from app.research import errors

    text = research_ui.completion_text(
        status="failed", error_code=errors.ROBOTS_DISALLOW, visible_cards=0
    )
    assert text == "Не получилось: сайт запрещает чтение этой страницы."


async def test_completion_text_for_the_cap_code():
    text = research_ui.completion_text(
        status="failed", error_code=research_jobs.CAP, visible_cards=0
    )
    assert text.startswith("Не получилось: ")


async def test_completion_text_falls_back_safely_for_an_unmapped_code():
    text = research_ui.completion_text(
        status="failed", error_code="some_future_code_nobody_mapped_yet", visible_cards=0
    )
    assert text == f"Не получилось: {research_ui.ERROR_RU_FALLBACK}."


async def test_every_declared_fetch_error_code_has_a_russian_mapping():
    from app.research import errors

    missing = errors.FETCH_ERROR_CODES - set(research_ui.ERROR_RU)
    assert not missing, f"no Russian text for: {sorted(missing)}"


# --- the completion line's plural agreement ---


@pytest.mark.parametrize(
    "n,expected",
    [
        (1, "Готово: 1 карточка. /notes"),
        (2, "Готово: 2 карточки. /notes"),
        (4, "Готово: 4 карточки. /notes"),
        (5, "Готово: 5 карточек. /notes"),
        (11, "Готово: 11 карточек. /notes"),
        (12, "Готово: 12 карточек. /notes"),
        (21, "Готово: 21 карточка. /notes"),
        (22, "Готово: 22 карточки. /notes"),
    ],
)
async def test_the_completion_line_agrees_with_the_card_count(n, expected):
    """Plan section 9 writes «Готово: N карточек. /notes» with N as a
    placeholder, not as a spec for the three Russian plural forms.
    «Готово: 1 карточек» is not a sentence this bot should send."""
    assert research_ui.completion_text(status="done", error_code=None, visible_cards=n) == expected


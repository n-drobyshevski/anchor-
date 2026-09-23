"""`/review`, `/amendments`, and every `am:*` callback (phase-5 plan
sections 3, 8 and 9, milestone 5d).

Router-level tests, same Dispatcher pattern as tests/test_orders_
commands.py.
"""

from __future__ import annotations

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import select

from app.config import Settings
from app.core import amendments, review
from app.db.models import (
    Job,
    PersonaAmendment,
    ReviewProposal,
    SpendLedger,
    StandingOrder,
    TelegramUpdate,
    UserState,
    WeeklyReview,
)
from app.tg.router import build_router
from conftest import FakeLLMProvider, FakeSession

pytestmark = pytest.mark.asyncio

TEST_CHAT_ID = 555
TIMEZONE = "Europe/Paris"

REVIEW_ANALYSIS_JSON = (
    '{"wins": ["сдал отчёт вовремя"], "misses": [], "patterns": [], '
    '"intentions": [], "proposals": [{"kind": "persona_note", "text": "меньше вопросов", "reason": null}]}'
)


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


def _build_dp(sessionmaker, settings=None, provider=None, safety_provider=None):
    fake_session = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake_session)
    dp = Dispatcher()
    dp.include_router(
        build_router(
            sessionmaker,
            settings or Settings(),
            provider or FakeLLMProvider(text="Хорошая неделя."),
            safety_provider or FakeLLMProvider(text=REVIEW_ANALYSIS_JSON),
        )
    )
    return dp, bot, fake_session


async def _seed(sessionmaker, *update_ids: int, **state) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=TEST_CHAT_ID, timezone=TIMEZONE, **state))
        await session.commit()
        for update_id in update_ids:
            session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()


async def _feed(dp, bot, payload: dict) -> None:
    await dp.feed_update(bot, Update.model_validate(payload, context={"bot": bot}))


# --- /review: on demand, cap only, gate-free --------------------------------


async def test_review_generates_stores_and_sends_the_message(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/review"))

    assert fake.sent[0].text == "Хорошая неделя."

    async with sessionmaker() as session:
        review_row = (await session.execute(select(WeeklyReview))).scalars().one()
    assert review_row.analysis["wins"] == ["сдал отчёт вовремя"]
    assert review_row.message_id is not None


async def test_review_works_even_when_the_gate_would_refuse(sessionmaker):
    """persona_active=False refuses every gated outbound -- /review is
    on-demand, not unsolicited, and bypasses the gate entirely."""
    await _seed(sessionmaker, 1, persona_active=False)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/review"))

    assert fake.sent[0].text == "Хорошая неделя."


async def test_review_respects_only_the_cap(sessionmaker):
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        session.add(SpendLedger(local_date=__import__("datetime").date.today(), category="chat", usd_cost=1.00))
        await session.commit()

    settings = Settings(DAILY_USD_CAP=1.00)
    dp, bot, fake = _build_dp(sessionmaker, settings=settings)

    from app.core.turn import CAP_REPLY_TEXT

    await _feed(dp, bot, _command_update(1, "/review"))
    assert fake.sent[0].text == CAP_REPLY_TEXT

    async with sessionmaker() as session:
        assert await session.scalar(select(WeeklyReview)) is None


async def test_review_sends_a_persona_note_card_with_am_buttons(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/review"))

    assert len(fake.sent) == 2
    card = fake.sent[1]
    assert "меньше вопросов" in card.text
    callbacks = [b.callback_data for b in card.reply_markup.inline_keyboard[0]]
    assert callbacks[0].startswith("am:a:")
    assert callbacks[1].startswith("am:r:")


async def test_a_second_review_the_same_week_regenerates(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/review"))
    async with sessionmaker() as session:
        first = (await session.execute(select(WeeklyReview))).scalars().one()
        first_proposal = (await session.execute(select(ReviewProposal))).scalars().one()

    await _feed(dp, bot, _command_update(2, "/review"))
    async with sessionmaker() as session:
        rows = (await session.execute(select(WeeklyReview))).scalars().all()
        assert len(rows) == 1  # upsert, not a second row
        assert rows[0].id == first.id

        stale = await session.get(ReviewProposal, first_proposal.id)
        assert stale.status == review.EXPIRED


# --- am:a / am:r ------------------------------------------------------------


async def _propose_persona_note(sessionmaker, *, text: str = "меньше вопросов") -> int:
    async with sessionmaker() as session:
        review_row = WeeklyReview(
            week_start=__import__("datetime").date(2026, 9, 21),
            analysis={"wins": [], "misses": [], "patterns": [], "intentions": [], "proposals": []},
        )
        session.add(review_row)
        await session.commit()
        await session.refresh(review_row)
        proposal = ReviewProposal(review_id=review_row.id, kind="persona_note", text=text)
        session.add(proposal)
        await session.commit()
        return proposal.id


async def test_am_a_adopts_and_enqueues_the_trial(sessionmaker):
    await _seed(sessionmaker, 1)
    proposal_id = await _propose_persona_note(sessionmaker)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, f"am:a:{proposal_id}"))

    assert fake.edits[-1].text == amendments.CHECKING_TEXT
    async with sessionmaker() as session:
        row = (await session.execute(select(PersonaAmendment))).scalars().one()
        assert row.status == amendments.TRIAL
        jobs = (await session.execute(select(Job))).scalars().all()
    assert [j.kind for j in jobs] == [amendments.AMENDMENT_TRIAL]
    assert jobs[0].dedup_key == f"am:{row.id}"


async def test_am_a_over_the_cap_shows_the_cap_text(sessionmaker):
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        session.add(PersonaAmendment(text="уже активна", status=amendments.ACTIVE, persona_sha="x"))
        await session.commit()
    proposal_id = await _propose_persona_note(sessionmaker)
    settings = Settings(AMENDMENTS_MAX_ACTIVE=1)
    dp, bot, fake = _build_dp(sessionmaker, settings=settings)

    await _feed(dp, bot, _callback_update(1, f"am:a:{proposal_id}"))

    assert fake.edits[-1].text == amendments.CAP_TEXT


async def test_am_r_declines(sessionmaker):
    await _seed(sessionmaker, 1)
    proposal_id = await _propose_persona_note(sessionmaker)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, f"am:r:{proposal_id}"))

    async with sessionmaker() as session:
        proposal = await session.get(ReviewProposal, proposal_id)
    assert proposal.status == review.REJECTED


async def test_am_a_stale_on_an_already_decided_proposal(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    proposal_id = await _propose_persona_note(sessionmaker)
    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _callback_update(1, f"am:r:{proposal_id}"))

    await _feed(dp, bot, _callback_update(2, f"am:a:{proposal_id}"))
    assert fake.edits[-1].text == "Устарело."


# --- /amendments and am:x ---------------------------------------------------


async def test_amendments_empty_list(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/amendments"))
    assert fake.sent[0].text == amendments.EMPTY_LIST_TEXT


async def test_amendments_lists_active_and_flags_a_stale_one(sessionmaker):
    from app.core.prompt import load_persona

    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        session.add(
            PersonaAmendment(text="актуальная", status=amendments.ACTIVE, persona_sha=load_persona()[1])
        )
        session.add(
            PersonaAmendment(text="устаревшая", status=amendments.ACTIVE, persona_sha="not-the-real-sha")
        )
        await session.commit()

    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/amendments"))

    text = fake.sent[0].text
    assert "актуальная" in text
    assert "устаревшая" in text
    assert amendments.STALE_CHANGED in text


async def test_am_x_revokes_and_rerenders(sessionmaker):
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        row = PersonaAmendment(text="актуальная", status=amendments.ACTIVE, persona_sha="x")
        session.add(row)
        await session.commit()
        await session.refresh(row)
        amendment_id = row.id

    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _callback_update(1, f"am:x:{amendment_id}"))

    async with sessionmaker() as session:
        row = await session.get(PersonaAmendment, amendment_id)
    assert row.status == amendments.REVOKED
    assert fake.edits[-1].text == amendments.EMPTY_LIST_TEXT


# --- so:a on a review-authored standing_order calls review.mark_proposal ---


async def test_accepting_a_review_proposed_order_marks_the_review_proposal_adopted(sessionmaker):
    order_json = (
        '{"wins": [], "misses": [], "patterns": [], "intentions": [], '
        '"proposals": [{"kind": "standing_order", "text": "пить воду по утрам", "reason": null}]}'
    )
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker, safety_provider=FakeLLMProvider(text=order_json))
    await _feed(dp, bot, _command_update(1, "/review"))

    async with sessionmaker() as session:
        order = (await session.execute(select(StandingOrder))).scalars().one()
        proposal = (await session.execute(select(ReviewProposal))).scalars().one()

    await _feed(dp, bot, _callback_update(2, f"so:a:{order.id}"))

    async with sessionmaker() as session:
        order = await session.get(StandingOrder, order.id)
        proposal = await session.get(ReviewProposal, proposal.id)
    assert order.status == "active"
    assert proposal.status == review.ADOPTED

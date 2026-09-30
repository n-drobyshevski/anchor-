"""The grounded review card, its [почему эти заметки?] button, and
/lens's last-round line (anchor-lens-plan.md sections 7 and 11;
milestone L2, spec item G).

Cards are rendered for real (app/tg/review.py's
`send_review_proposal_cards` against tests' FakeSession bot), the
`lr:w:<id>` callback goes through the real router, and the lens rows
are synthetic -- invented titles, never a real note.
"""

from __future__ import annotations

import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import delete, func, select, text, update

from app.config import Settings
from app.core import review
from app.core.clock import FrozenClock
from app.db.models import LensNote, LensRound, ReviewProposal, UserState, VaultFile, WeeklyReview
from app.tg import lens as lens_ui
from app.tg import review as review_ui
from app.tg.router import build_router
from app.vault import lens
from conftest import FakeLLMProvider, FakeSession

TEST_CHAT_ID = 555
NOW = datetime.datetime(2026, 9, 29, 12, 0, tzinfo=datetime.timezone.utc)
RATIONALE = "Неделя однообразных ответов — нужна разнообразность."


def _bot() -> tuple[Bot, FakeSession]:
    fake = FakeSession()
    return Bot(token="123456:TESTTOKEN", session=fake), fake


async def _seed_state(sessionmaker, timezone: str = "UTC") -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=TEST_CHAT_ID, timezone=timezone))
        await session.commit()


async def _note(session, title: str, kind: str = "concept") -> int:
    row = VaultFile(path=f"Lens/{title}.md", role="note", note_class="knowledge")
    session.add(row)
    await session.flush()
    note = LensNote(
        vault_file_id=row.id, kind=kind, title=title, body="x", body_hash=title.ljust(64, "0")[:64],
        chars=1,
    )
    session.add(note)
    await session.flush()
    return note.id


async def _round(session, *, note_ids, rationale=RATIONALE, outcome="grounded", created_at=None) -> int:
    row = LensRound(
        consumer="review", selected_note_ids=list(note_ids), rationale=rationale, outcome=outcome
    )
    if created_at is not None:
        row.created_at = created_at
    session.add(row)
    await session.flush()
    return row.id


async def _proposal(
    session, *, kind="persona_note", text="разнообразнее отвечать", lens_round_id=None, lens_note_ids=None
) -> ReviewProposal:
    # One review per proposal; week_start is unique, so step back a week each time.
    taken = (await session.execute(select(func.count()).select_from(WeeklyReview))).scalar_one()
    review_row = WeeklyReview(
        week_start=datetime.date(2026, 9, 21) - datetime.timedelta(weeks=taken),
        analysis={"wins": [], "misses": [], "patterns": [], "intentions": [], "proposals": []},
    )
    session.add(review_row)
    await session.flush()
    proposal = ReviewProposal(
        review_id=review_row.id,
        kind=kind,
        text=text,
        lens_round_id=lens_round_id,
        lens_note_ids=lens_note_ids,
    )
    session.add(proposal)
    await session.commit()
    await session.refresh(proposal)
    return proposal


def _callbacks(message) -> list[list[str]]:
    return [[b.callback_data for b in row] for row in message.reply_markup.inline_keyboard]


# --- the card -----------------------------------------------------------------------


async def test_card_shows_the_current_titles_in_order(sessionmaker):
    """The titles are read when the card is sent, so a note renamed since
    the round reads by its new name; the order is the proposal's."""
    bot, fake = _bot()
    async with sessionmaker() as session:
        ashby = await _note(session, "Эшби")
        variety = await _note(session, "Необходимое разнообразие")
        round_id = await _round(session, note_ids=[variety, ashby])
        proposal = await _proposal(session, lens_round_id=round_id, lens_note_ids=[variety, ashby])
        await session.execute(
            update(LensNote).where(LensNote.id == ashby).values(title="Росс Эшби")
        )
        await session.commit()
        await review_ui.send_review_proposal_cards(
            bot, TEST_CHAT_ID, [review.CreatedProposal(proposal=proposal)], session=session
        )

    (card,) = fake.sent
    assert card.text.splitlines() == [
        review_ui.PERSONA_NOTE_TEXT.format(text="разнообразнее отвечать"),
        "основание: Необходимое разнообразие, Росс Эшби",
    ]
    assert _callbacks(card) == [
        [f"am:a:{proposal.id}", f"am:r:{proposal.id}"],
        [f"lr:w:{round_id}"],
    ]
    assert card.reply_markup.inline_keyboard[1][0].text == review_ui.WHY_LABEL


async def test_card_skips_a_note_that_left_the_lens(sessionmaker):
    bot, fake = _bot()
    async with sessionmaker() as session:
        kept = await _note(session, "Эшби")
        gone = await _note(session, "Ушедшая")
        round_id = await _round(session, note_ids=[gone, kept])
        proposal = await _proposal(session, lens_round_id=round_id, lens_note_ids=[gone, kept, 999999])
        await session.execute(delete(LensNote).where(LensNote.id == gone))
        await session.commit()
        await review_ui.send_review_proposal_cards(
            bot, TEST_CHAT_ID, [review.CreatedProposal(proposal=proposal)], session=session
        )

    assert fake.sent[0].text.splitlines()[-1] == "основание: Эшби"
    assert "Ушедшая" not in fake.sent[0].text


async def test_card_with_every_note_gone_has_no_grounds_line(sessionmaker):
    bot, fake = _bot()
    async with sessionmaker() as session:
        gone = await _note(session, "Ушедшая")
        round_id = await _round(session, note_ids=[gone])
        proposal = await _proposal(session, lens_round_id=round_id, lens_note_ids=[gone])
        await session.execute(delete(LensNote))
        await session.commit()
        await review_ui.send_review_proposal_cards(
            bot, TEST_CHAT_ID, [review.CreatedProposal(proposal=proposal)], session=session
        )

    assert "основание" not in fake.sent[0].text
    # The round is still there, so its `why` can still be asked for.
    assert _callbacks(fake.sent[0])[-1] == [f"lr:w:{round_id}"]


async def test_card_without_lens_note_ids_is_the_pre_l2_card(sessionmaker):
    """No `lens_note_ids` (the lens inactive, empty or fallen back): the
    card is exactly what it was before L2 -- no line, no button."""
    bot, fake = _bot()
    async with sessionmaker() as session:
        await _note(session, "Эшби")
        round_id = await _round(session, note_ids=[], outcome="empty")
        plain = await _proposal(session)
        empty_ids = await _proposal(session, lens_round_id=round_id, lens_note_ids=[])
        await review_ui.send_review_proposal_cards(
            bot,
            TEST_CHAT_ID,
            [review.CreatedProposal(proposal=plain), review.CreatedProposal(proposal=empty_ids)],
            session=session,
        )

    for card, proposal in zip(fake.sent, (plain, empty_ids), strict=True):
        assert card.text == review_ui.PERSONA_NOTE_TEXT.format(text="разнообразнее отвечать")
        assert card.reply_markup == review_ui.amendment_proposal_keyboard(proposal.id)


async def test_ids_without_a_round_show_the_line_but_no_button(sessionmaker):
    """The round deleted under the proposal (ON DELETE SET NULL): the
    grounds still read, but there is no `why` left to ask for."""
    bot, fake = _bot()
    async with sessionmaker() as session:
        ashby = await _note(session, "Эшби")
        proposal = await _proposal(session, lens_round_id=None, lens_note_ids=[ashby])
        await review_ui.send_review_proposal_cards(
            bot, TEST_CHAT_ID, [review.CreatedProposal(proposal=proposal)], session=session
        )

    assert fake.sent[0].text.endswith("\nоснование: Эшби")
    assert _callbacks(fake.sent[0]) == [[f"am:a:{proposal.id}", f"am:r:{proposal.id}"]]


async def test_standing_order_card_gets_the_line_and_button(sessionmaker):
    bot, fake = _bot()
    async with sessionmaker() as session:
        ashby = await _note(session, "Эшби")
        round_id = await _round(session, note_ids=[ashby])
        proposal = await _proposal(
            session,
            kind=review.STANDING_ORDER,
            text="раз в день спрашивать по-разному",
            lens_round_id=round_id,
            lens_note_ids=[ashby],
        )
        await review_ui.send_review_proposal_cards(
            bot, TEST_CHAT_ID, [review.CreatedProposal(proposal=proposal, order_id=77)], session=session
        )

    card = fake.sent[0]
    assert card.text.splitlines()[-1] == "основание: Эшби"
    assert _callbacks(card) == [["so:a:77", "so:c:77", "so:r:77"], [f"lr:w:{round_id}"]]


async def test_card_reads_titles_on_the_proposals_own_session(sessionmaker):
    """The scheduled path (app/core/outbound_send.py) passes no session:
    the titles are read on the one the proposal rows are attached to."""
    bot, fake = _bot()
    async with sessionmaker() as session:
        ashby = await _note(session, "Эшби")
        round_id = await _round(session, note_ids=[ashby])
        proposal = await _proposal(session, lens_round_id=round_id, lens_note_ids=[ashby])
        await review_ui.send_review_proposal_cards(
            bot, TEST_CHAT_ID, [review.CreatedProposal(proposal=proposal)]
        )

    assert fake.sent[0].text.endswith("\nоснование: Эшби")


async def test_a_detached_proposal_still_gets_its_card(sessionmaker):
    """No session to read titles on: the line is left off, the card sent."""
    bot, fake = _bot()
    async with sessionmaker() as session:
        ashby = await _note(session, "Эшби")
        round_id = await _round(session, note_ids=[ashby])
        proposal = await _proposal(session, lens_round_id=round_id, lens_note_ids=[ashby])
    await review_ui.send_review_proposal_cards(
        bot, TEST_CHAT_ID, [review.CreatedProposal(proposal=proposal)]
    )

    assert "основание" not in fake.sent[0].text
    assert _callbacks(fake.sent[0])[-1] == [f"lr:w:{round_id}"]


async def test_a_failed_titles_lookup_still_sends_the_card_and_leaves_the_session_usable(
    sessionmaker, monkeypatch
):
    """A real database error in the lookup (division by zero, raised by
    Postgres) rolls back the lookup's SAVEPOINT alone: the card goes out
    without its line, the next card's lookup and the caller's own next
    query run on the same session, and the proposal rows are not expired."""
    real_lookup = review_ui.lens_review.grounds_titles
    calls = 0

    async def failing_once(session, lens_note_ids):
        nonlocal calls
        calls += 1
        if calls == 1:
            await session.execute(text("select 1/0"))
        return await real_lookup(session, lens_note_ids)

    monkeypatch.setattr(review_ui.lens_review, "grounds_titles", failing_once)
    bot, fake = _bot()
    async with sessionmaker() as session:
        ashby = await _note(session, "Эшби")
        round_id = await _round(session, note_ids=[ashby])
        first = await _proposal(session, lens_round_id=round_id, lens_note_ids=[ashby])
        second = await _proposal(session, lens_round_id=round_id, lens_note_ids=[ashby])
        await review_ui.send_review_proposal_cards(
            bot,
            TEST_CHAT_ID,
            [review.CreatedProposal(proposal=first), review.CreatedProposal(proposal=second)],
            session=session,
        )
        # The caller's transaction is not aborted: a following query runs.
        count = (await session.execute(select(func.count()).select_from(ReviewProposal))).scalar_one()
        assert count == 2
        assert first.text == "разнообразнее отвечать"
        await session.commit()

    assert calls == 2
    assert len(fake.sent) == 2
    assert "основание" not in fake.sent[0].text
    assert _callbacks(fake.sent[0])[-1] == [f"lr:w:{round_id}"]
    assert fake.sent[1].text.endswith("\nоснование: Эшби")


# --- lr:w:<round id>-----------------------------------------------------------------


def test_why_callback_data_round_trips_and_fits_telegram():
    data = review_ui.why_callback_data(2**31 - 1)
    assert data == "lr:w:2147483647"
    assert len(data.encode()) <= 64
    assert review_ui.parse_why_callback(data) == 2**31 - 1


@pytest.mark.parametrize(
    "data",
    [None, "", "lr:", "lr:w:", "lr:w:abc", "lr:w:-1", "lr:w:0", "lr:w:1:2", "lr:x:1", "lr:w: 1",
     "lr:w:١", "am:a:1"],
)
def test_malformed_why_data_parses_to_none(data):
    assert review_ui.parse_why_callback(data) is None


def _callback_update(update_id: int, data: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": TEST_CHAT_ID, "is_bot": False, "first_name": "Test"},
            "chat_instance": "ci",
            "data": data,
            "message": {
                "message_id": 900,
                "date": 0,
                "chat": {"id": TEST_CHAT_ID, "type": "private"},
                "text": "…",
            },
        },
    }


async def _press(sessionmaker, data: str) -> FakeSession:
    bot, fake = _bot()
    dp = Dispatcher()
    dp.include_router(
        build_router(sessionmaker, Settings(), FakeLLMProvider(text="x"), FakeLLMProvider(text="{}"))
    )
    await dp.feed_update(bot, Update.model_validate(_callback_update(1, data), context={"bot": bot}))
    return fake


async def test_why_button_replies_with_the_rounds_rationale(sessionmaker):
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        round_id = await _round(session, note_ids=[])
        await session.commit()

    fake = await _press(sessionmaker, f"lr:w:{round_id}")

    assert len(fake.answered) == 1
    assert [m.text for m in fake.sent] == [RATIONALE]
    assert fake.edits == []  # the card and its own buttons are left alone


async def test_why_button_without_a_rationale_says_so(sessionmaker):
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        round_id = await _round(session, note_ids=[], rationale=None)
        await session.commit()

    fake = await _press(sessionmaker, f"lr:w:{round_id}")

    assert [m.text for m in fake.sent] == [review_ui.NO_WHY_TEXT]


async def test_why_button_for_a_round_that_is_gone_says_so(sessionmaker):
    await _seed_state(sessionmaker)

    fake = await _press(sessionmaker, "lr:w:424242")

    assert len(fake.answered) == 1
    assert [m.text for m in fake.sent] == [review_ui.NO_WHY_TEXT]


@pytest.mark.parametrize("data", ["lr:w:abc", "lr:w:", "lr:x:1", "lr:w:1:2", "lr:"])
async def test_malformed_why_press_is_answered_and_ignored(sessionmaker, data):
    await _seed_state(sessionmaker)

    fake = await _press(sessionmaker, data)

    assert len(fake.answered) == 1
    assert fake.sent == [] and fake.edits == []


# --- /lens: the last round ----------------------------------------------------------


async def test_status_shows_the_last_round_in_local_date_and_russian(sessionmaker):
    """23:30 UTC on the 27th is already the 28th in Moscow; the newest
    round is the one shown."""
    await _seed_state(sessionmaker, timezone="Europe/Moscow")
    async with sessionmaker() as session:
        await _round(
            session, note_ids=[], outcome="grounded",
            created_at=datetime.datetime(2026, 9, 20, 10, 0, tzinfo=datetime.timezone.utc),
        )
        await _round(
            session, note_ids=[], outcome="fallback",
            created_at=datetime.datetime(2026, 9, 27, 23, 30, tzinfo=datetime.timezone.utc),
        )
        await session.commit()

    reply = await lens_ui.command(sessionmaker, Settings(LENS_ENABLED=True), FrozenClock(NOW), None)

    assert (
        "Последний разбор: 28.09.2026, линза не сработала, предложения без неё." in reply.splitlines()
    )
    assert RATIONALE not in reply


async def test_status_without_a_round_has_no_line(sessionmaker):
    await _seed_state(sessionmaker)
    reply = await lens_ui.command(sessionmaker, Settings(LENS_ENABLED=True), FrozenClock(NOW), None)
    assert "Последний разбор" not in reply


def test_every_round_outcome_has_russian_text():
    assert set(lens_ui.ROUND_OUTCOME_TEXT) == set(lens.ROUND_OUTCOMES)


@pytest.mark.parametrize("outcome", ["grounded", "empty", "fallback"])
async def test_status_names_each_outcome(sessionmaker, outcome):
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        await _round(session, note_ids=[], outcome=outcome, created_at=NOW)
        await session.commit()

    reply = await lens_ui.command(sessionmaker, Settings(LENS_ENABLED=True), FrozenClock(NOW), None)

    assert f"Последний разбор: 29.09.2026, {lens_ui.ROUND_OUTCOME_TEXT[outcome]}." in reply

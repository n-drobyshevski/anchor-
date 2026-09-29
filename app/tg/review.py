"""`/review`, the weekly review's proposal cards, and the `am:a`/`am:r`
callbacks (phase-5 plan sections 3, 8 and 9; milestone 5d).

Everything Telegram-shaped about the weekly review lives here;
app/core/review.py is the domain layer and imports no aiogram types,
nothing under app.tg, and none of the state writers or gates -- the
same split app/tg/orders.py and app/tg/notebook.py already keep with
their own core modules.

`/review` (`run_review_command`) does the full analysis-message-cards
flow app/core/outbound_send.py's `WEEKLY_REVIEW` branch does for the
scheduled path, but with **no Outbound row and no gate** -- the cap is
the only check (implementation plan's "/review"). The two paths cannot
share the send/store code directly (that lives in outbound_send.py,
keyed on an `outbound_id` this command never has), so it is
deliberately re-derived here, at the Telegram layer, which is exactly
where the plan says the duplication belongs ("imported locally the same
way core already imports send_outbound_message").

L2 (anchor-lens-plan.md sections 7 and 11): a proposal the lens round
grounded (`lens_note_ids` set, app/core/lens_review.py) shows
«основание: A, B» -- the *current* titles of those notes, so a note
renamed since reads by its new name and a note gone from the lens is
simply left out -- and a [почему эти заметки?] button, `lr:w:<lens_round
id>`, that answers with the selector's `why` for that round. The card
reaches the lens only through app/core/lens_review.py, never
app/vault/lens.py itself (tests/test_vault_notes_isolation.py). A title
lookup that fails leaves the line off rather than the card unsent: the
review never fails because of the lens. Neither a title nor the `why`
is ever logged (app/log.py).
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_object_session

from app.config import Settings
from app.core import amendments as amendments_module
from app.core import clock as clock_module
from app.core import lens_review
from app.core import orders as orders_module
from app.core import persona_context as persona_context_module
from app.core import review as review_module
from app.core import review_actions
from app.core.clock import Clock
from app.core.outbound_gate import WEEKLY_REVIEW
from app.core.outbound_send import build_outbound_messages
from app.core.scene import bump_message_count, ensure_open_scene
from app.core.spend import check_cap, priced
from app.core.state import get_state
from app.core.turn import CAP_REPLY_TEXT, NICKNAME_RNG, _complete_with_retries
from app.core.voice import remember_nickname
from app.db.models import Message, SpendLedger
from app.llm.provider import LLMProvider
from app.tg.send import answer_callback, edit_keyboard, send_keyboard, send_reply

logger = logging.getLogger(__name__)

ADOPT_LABEL = "Принять"
REJECT_LABEL = "Отклонить"

PERSONA_NOTE_TEXT = "Поправка к стилю: «{text}»"
STALE = "Устарело."
DECLINED_TEXT = "✖️ Отклонено"
UNAVAILABLE_TEXT = "Не получилось подготовить обзор недели. Попробуй позже."

# L2: the grounded card's line and button, and the button's answer when
# the round kept no `why` (or is gone: /delete, ON DELETE SET NULL).
GROUNDS_LINE = "основание: {titles}"
WHY_LABEL = "почему эти заметки?"
NO_WHY_TEXT = "Объяснения нет."
WHY_CALLBACK_PREFIX = "lr:w:"


def amendment_proposal_keyboard(review_proposal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=ADOPT_LABEL, callback_data=f"am:a:{review_proposal_id}"),
                InlineKeyboardButton(
                    text=REJECT_LABEL, callback_data=f"am:r:{review_proposal_id}"
                ),
            ]
        ]
    )


def why_callback_data(lens_round_id: int) -> str:
    """`lr:w:<lens_round id>` -- the grounded card's [почему эти заметки?]."""
    return f"{WHY_CALLBACK_PREFIX}{lens_round_id}"


def parse_why_callback(data: str | None) -> int | None:
    """The round id in `lr:w:<id>`, or None for anything else: another
    shape, a non-decimal or non-positive id."""
    if not data or not data.startswith(WHY_CALLBACK_PREFIX):
        return None
    raw = data[len(WHY_CALLBACK_PREFIX) :]
    if not raw.isascii() or not raw.isdigit():
        return None
    round_id = int(raw)
    return round_id if round_id > 0 else None


def _why_row(proposal) -> list[InlineKeyboardButton] | None:
    """The [почему эти заметки?] row, only on a grounded proposal whose
    round still exists."""
    if proposal.lens_round_id is None or not proposal.lens_note_ids:
        return None
    return [
        InlineKeyboardButton(text=WHY_LABEL, callback_data=why_callback_data(proposal.lens_round_id))
    ]


async def _grounds_line(session: AsyncSession | None, proposal) -> str | None:
    """«основание: A, B» from the current titles of the proposal's
    `lens_note_ids`; None without any, or when every one has left the
    lens, or when the lookup fails (logged without a title).

    The lookup runs in its own SAVEPOINT (`begin_nested`, as
    app/core/lens_review.py does): a database error rolls back that
    SAVEPOINT alone, so the caller's transaction stays usable for the
    next card and whatever the caller does after."""
    if session is None or not proposal.lens_note_ids:
        return None
    try:
        async with session.begin_nested():
            titles = await lens_review.grounds_titles(session, proposal.lens_note_ids)
    except SQLAlchemyError as exc:
        logger.warning(
            "lens grounds lookup failed",
            extra={"proposal_id": proposal.id, "event": type(exc).__name__},
        )
        return None
    return GROUNDS_LINE.format(titles=", ".join(titles)) if titles else None


async def send_review_proposal_cards(
    bot: Bot,
    chat_id: int,
    proposals: list[review_module.CreatedProposal],
    *,
    session: AsyncSession | None = None,
) -> None:
    """Implementation plan's "Send path" step 5: each proposal, its own
    card. `standing_order` proposals reuse §7's flow -- the exact same
    card app/core/extract.py's own proposals get, on the StandingOrder
    row `review.create_proposals` already made -- and `persona_note`
    proposals get this module's own [Принять]/[Отклонить] card.

    L2: a grounded proposal's card also carries «основание» and the
    [почему эти заметки?] row. The titles are read on `session`, or,
    when the caller passes none (app/core/outbound_send.py's scheduled
    path), on the session the proposal rows are still attached to.
    """
    for item in proposals:
        proposal = item.proposal
        grounds = await _grounds_line(
            session if session is not None else async_object_session(proposal), proposal
        )
        why_row = _why_row(proposal)
        if proposal.kind == review_module.STANDING_ORDER and item.order_id is not None:
            text = orders_module.PROPOSAL_TEXT.format(
                text=proposal.text,
                cadence=orders_module.cadence_label(review_module.DEFAULT_ORDER_CADENCE, None),
            )
            if grounds is not None:
                text = f"{text}\n{grounds}"
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Принять", callback_data=f"so:a:{item.order_id}"
                        ),
                        InlineKeyboardButton(
                            text="Изменить", callback_data=f"so:c:{item.order_id}"
                        ),
                        InlineKeyboardButton(
                            text="Отклонить", callback_data=f"so:r:{item.order_id}"
                        ),
                    ]
                ]
            )
            if why_row is not None:
                markup.inline_keyboard.append(why_row)
            await send_keyboard(bot, chat_id, text, markup)
        elif proposal.kind == review_module.PERSONA_NOTE:
            text = PERSONA_NOTE_TEXT.format(text=proposal.text)
            if grounds is not None:
                text = f"{text}\n{grounds}"
            markup = amendment_proposal_keyboard(proposal.id)
            if why_row is not None:
                markup.inline_keyboard.append(why_row)
            await send_keyboard(bot, chat_id, text, markup)


# --- /review -------------------------------------------------------------


async def run_review_command(
    sessionmaker,
    settings: Settings,
    provider: LLMProvider,
    safety_provider: LLMProvider | None,
    bot: Bot,
    clock: Clock,
    *,
    chat_id: int,
) -> None:
    """`/review`: on demand, gate-free, cap-only (implementation plan's
    "/review")."""
    async with sessionmaker() as session:
        state = await get_state(session)
        over_cap = await check_cap(session, settings, clock, state.timezone)
    if over_cap:
        await send_reply(bot, chat_id, CAP_REPLY_TEXT)
        return
    if safety_provider is None:
        await send_reply(bot, chat_id, UNAVAILABLE_TEXT)
        return

    async with sessionmaker() as session:
        state = await get_state(session)
        outcome = await review_module.run_review(
            session, settings, safety_provider, clock=clock, timezone=state.timezone, on_demand=True
        )
        if not outcome.available:
            await send_reply(bot, chat_id, UNAVAILABLE_TEXT)
            return

        scene_id = await ensure_open_scene(session, clock, idle_hours=settings.SCENE_IDLE_HOURS)
        persona_ctx = await persona_context_module.gather(
            session,
            settings,
            state,
            clock,
            scene_id=scene_id,
            exclude_update_id=None,
            rng=NICKNAME_RNG,
        )
        messages = await build_outbound_messages(
            session,
            settings,
            state,
            clock=clock,
            kind=WEEKLY_REVIEW,
            review_note=outcome.note,
            persona_context=persona_ctx,
        )
        response = await _complete_with_retries(
            provider, messages, update_id=outcome.review_id or 0
        )
        text = (response.text.strip() if response is not None else "")
        if not text:
            await send_reply(bot, chat_id, UNAVAILABLE_TEXT)
            return

        cost = priced(response.usage, settings, model=response.model)
        message = Message(
            role="assistant",
            content=text,
            ooc=False,
            kind="outbound",
            scene_id=scene_id,
            model=response.model,
            tokens_in=response.usage.input_tokens,
            tokens_cached=response.usage.cached_tokens,
            tokens_out=response.usage.output_tokens,
            usd_cost=cost.usd,
        )
        session.add(message)
        await session.commit()
        await session.refresh(message)
        if scene_id is not None:
            await bump_message_count(session, scene_id)
        session.add(
            SpendLedger(
                local_date=clock_module.local_date(clock, state.timezone),
                category=review_module.REVIEW_MSG_CATEGORY,
                model=response.model,
                tokens_in=response.usage.input_tokens,
                tokens_cached=response.usage.cached_tokens,
                tokens_out=response.usage.output_tokens,
                usd_cost=cost.usd,
                cost_source=cost.source,
            )
        )
        await session.commit()

        await send_reply(bot, chat_id, text)
        message.sent_at = clock.now_utc()
        await session.commit()
        if persona_ctx.nickname is not None:
            await remember_nickname(session, persona_ctx.nickname)

        await review_module.set_message_id(session, outcome.review_id, message.id)

        logger.info("review sent on demand", extra={"review_id": outcome.review_id})
        await send_review_proposal_cards(bot, chat_id, list(outcome.proposals), session=session)


# --- am:a / am:r callbacks -------------------------------------------------


async def handle_decision_callback(
    sessionmaker,
    bot: Bot,
    settings: Settings,
    clock: Clock,
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    data: str,
) -> None:
    """`am:a:<review_proposal_id>` / `am:r:<review_proposal_id>` -- adopt
    or decline a `persona_note` proposal card."""
    _, action, raw_id = data.split(":", 2)
    await answer_callback(bot, callback_id)

    try:
        proposal_id = int(raw_id)
    except ValueError:
        await edit_keyboard(bot, chat_id, message_id, STALE, None)
        return

    if action == "a":
        # Adopts and queues the trial (app/core/review_actions.py, shared
        # with the web's Дневник).
        async with sessionmaker() as session:
            result = await review_actions.adopt_amendment(session, settings, proposal_id, clock=clock)
        if result.status == "cap":
            await edit_keyboard(bot, chat_id, message_id, amendments_module.CAP_TEXT, None)
            return
        if result.status != "ok" or result.amendment is None:
            await edit_keyboard(bot, chat_id, message_id, STALE, None)
            return
        await edit_keyboard(bot, chat_id, message_id, amendments_module.CHECKING_TEXT, None)
        return

    if action == "r":
        async with sessionmaker() as session:
            ok = await amendments_module.reject(session, proposal_id, clock=clock)
        await edit_keyboard(
            bot, chat_id, message_id, DECLINED_TEXT if ok else STALE, None
        )
        return

    await edit_keyboard(bot, chat_id, message_id, STALE, None)


# --- lr:w callback (L2) -------------------------------------------------------


async def handle_why_callback(
    sessionmaker,
    bot: Bot,
    *,
    callback_id: str,
    chat_id: int,
    data: str,
) -> None:
    """`lr:w:<lens_round id>` -- [почему эти заметки?] on a grounded
    card: a new message with the selector's `why` for that round, or
    NO_WHY_TEXT when it kept none or the round is gone. The card itself
    is left as it is, so its own buttons still work. Malformed data is
    answered and otherwise ignored, like the paging callbacks'."""
    await answer_callback(bot, callback_id)
    round_id = parse_why_callback(data)
    if round_id is None:
        return
    async with sessionmaker() as session:
        why = await lens_review.round_why(session, round_id)
    await send_reply(bot, chat_id, why or NO_WHY_TEXT)

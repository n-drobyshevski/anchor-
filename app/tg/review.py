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
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.core import amendments as amendments_module
from app.core import clock as clock_module
from app.core import orders as orders_module
from app.core import persona_context as persona_context_module
from app.core import review as review_module
from app.core.clock import Clock
from app.core.outbound_gate import WEEKLY_REVIEW
from app.core.outbound_send import build_outbound_messages
from app.core.scene import bump_message_count, ensure_open_scene
from app.core.spend import check_cap, priced
from app.core.state import get_state
from app.core.turn import CAP_REPLY_TEXT, NICKNAME_RNG, _complete_with_retries
from app.core.voice import remember_nickname
from app.db.jobs import enqueue_job
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


async def send_review_proposal_cards(
    bot: Bot, chat_id: int, proposals: list[review_module.CreatedProposal]
) -> None:
    """Implementation plan's "Send path" step 5: each proposal, its own
    card. `standing_order` proposals reuse §7's flow -- the exact same
    card app/core/extract.py's own proposals get, on the StandingOrder
    row `review.create_proposals` already made -- and `persona_note`
    proposals get this module's own [Принять]/[Отклонить] card.
    """
    for item in proposals:
        proposal = item.proposal
        if proposal.kind == review_module.STANDING_ORDER and item.order_id is not None:
            text = orders_module.PROPOSAL_TEXT.format(
                text=proposal.text,
                cadence=orders_module.cadence_label(review_module.DEFAULT_ORDER_CADENCE, None),
            )
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
            await send_keyboard(bot, chat_id, text, markup)
        elif proposal.kind == review_module.PERSONA_NOTE:
            text = PERSONA_NOTE_TEXT.format(text=proposal.text)
            await send_keyboard(bot, chat_id, text, amendment_proposal_keyboard(proposal.id))


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
    await send_review_proposal_cards(bot, chat_id, list(outcome.proposals))


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
        async with sessionmaker() as session:
            result = await amendments_module.adopt(session, settings, proposal_id, clock=clock)
        if result.status == "cap":
            await edit_keyboard(bot, chat_id, message_id, amendments_module.CAP_TEXT, None)
            return
        if result.status != "ok" or result.amendment is None:
            await edit_keyboard(bot, chat_id, message_id, STALE, None)
            return
        await edit_keyboard(bot, chat_id, message_id, amendments_module.CHECKING_TEXT, None)
        async with sessionmaker() as session:
            await enqueue_job(
                session,
                amendments_module.AMENDMENT_TRIAL,
                {"amendment_id": result.amendment.id},
                dedup_key=f"am:{result.amendment.id}",
            )
        return

    if action == "r":
        async with sessionmaker() as session:
            ok = await amendments_module.reject(session, proposal_id, clock=clock)
        await edit_keyboard(
            bot, chat_id, message_id, DECLINED_TEXT if ok else STALE, None
        )
        return

    await edit_keyboard(bot, chat_id, message_id, STALE, None)

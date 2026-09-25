"""Proposal confirmation messages and their buttons (plan section 8).

Deliberately not in-character and deliberately short: this is the bot
asking permission, not Anchor talking. A persona-voiced confirmation
would blur the one moment where the user is being asked to authorise a
change to how the bot pushes them.

Callback data is `p:a:<id>` / `p:r:<id>`, well inside Telegram's
64-byte limit (plan section 8).
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.clock import Clock, SystemClock
from app.core import obligations, proposal
from app.tg.send import answer_callback, edit_keyboard, send_keyboard

logger = logging.getLogger(__name__)

ACCEPT = "Принять"
REJECT = "Отклонить"

ACCEPTED_TEXT = "✅ Принято"
REJECTED_TEXT = "✖️ Отклонено"
STALE = "Устарело."
CAP_REACHED = "Долгов уже {max} — сначала закрой один (/paid)."

# Plan section 8's example message, generalised over the three fields.
FIELD_LABELS = {
    proposal.DUE_ACTION: "Главное действие",
    proposal.FOCUS_ON: "Фокус",
    proposal.RULE: "Правило",
    proposal.OBLIGATION: "В долг",
}
CONFIRM_TEXT = "Записать? {label}: «{value}»"


def confirm_text(field: str, value: str) -> str:
    return CONFIRM_TEXT.format(label=FIELD_LABELS.get(field, field), value=value)


def confirm_keyboard(proposal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=ACCEPT, callback_data=f"p:a:{proposal_id}"),
                InlineKeyboardButton(text=REJECT, callback_data=f"p:r:{proposal_id}"),
            ]
        ]
    )


async def send_proposal(
    sessionmaker, bot: Bot, *, chat_id: int, proposal_id: int, expired_id: int | None = None
) -> None:
    """Send the confirmation message and remember which message it is.

    `tg_message_id` is stored so that, when a later proposal expires
    this one, its buttons can be edited away -- an expired decision that
    still looks answerable is worse than no buttons at all.
    """
    async with sessionmaker() as session:
        row = await session.get(proposal.Proposal, proposal_id)
        if row is None or row.status != proposal.PENDING:
            return
        field, value = row.field, row.value

    if expired_id is not None:
        # Plan section 8: a superseded proposal's buttons are edited
        # away. Best-effort -- a failure here must not stop the new
        # proposal being sent, which is the one the user needs.
        try:
            await retire_buttons(sessionmaker, bot, chat_id=chat_id, proposal_id=expired_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "retiring expired proposal buttons failed",
                extra={"proposal_id": expired_id, "event": type(exc).__name__},
            )

    message_id = await send_keyboard(
        bot, chat_id, confirm_text(field, value), confirm_keyboard(proposal_id)
    )
    async with sessionmaker() as session:
        await proposal.set_message_id(session, proposal_id, message_id)


async def retire_buttons(sessionmaker, bot: Bot, *, chat_id: int, proposal_id: int) -> None:
    """Edit an expired proposal's buttons away, if we know its message."""
    async with sessionmaker() as session:
        row = await session.get(proposal.Proposal, proposal_id)
    if row is None or row.tg_message_id is None:
        return
    await edit_keyboard(
        bot, chat_id, row.tg_message_id, confirm_text(row.field, row.value) + f"\n{STALE}", None
    )


async def show_decision_outcome(
    bot: Bot, *, chat_id: int, message_id: int, decided: proposal.Proposal, accepted: bool
) -> None:
    """Edit a just-decided proposal's message to show the outcome
    (accepted or rejected) and remove its buttons.

    The shared other half of `handle_decision_callback`'s success
    branch below -- W2's web Proposals panel calls this too after
    `proposal.accept`/`reject`, so a decision made from either
    transport is reflected on the Telegram message identically (never
    `retire_buttons`' "Устарело.", which means something else: a
    proposal superseded by a different decision, not this one being
    decided).

    `message_id` is taken explicitly rather than read off `decided.
    tg_message_id`, matching what this function replaced: the Telegram
    callback path below already knows the message its own button lives
    on and must keep using exactly that id, not a second lookup. The
    web panel, which has no callback message of its own, passes
    `decided.tg_message_id` and skips the call entirely when that is
    None (a proposal never sent to Telegram, e.g. one accepted or
    rejected before `send_proposal` runs).
    """
    outcome = ACCEPTED_TEXT if accepted else REJECTED_TEXT
    await edit_keyboard(
        bot,
        chat_id,
        message_id,
        f"{confirm_text(decided.field, decided.value)}\n{outcome}",
        None,
    )


async def handle_decision_callback(
    sessionmaker,
    bot: Bot,
    clock: Clock | None = None,
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    data: str,
) -> None:
    """`p:a:<id>` / `p:r:<id>`.

    Idempotent by construction: accept()/reject() return None for any
    row that is not pending, and the handler then just removes the
    buttons. A replayed callback therefore changes nothing and leaves
    the message in the state the first press put it in (plan section 8:
    "If the proposal is not `pending`, just remove the buttons").
    """
    _, action, raw_id = data.split(":", 2)

    try:
        proposal_id = int(raw_id)
    except ValueError:
        await answer_callback(bot, callback_id)
        await edit_keyboard(bot, chat_id, message_id, STALE, None)
        return

    # Phase 5: accepting a debt at the cap would open nothing. Say so
    # and leave the proposal pending, so it can be accepted once a debt
    # is closed.
    if action == "a":
        async with sessionmaker() as session:
            row = await session.get(proposal.Proposal, proposal_id)
            full = (
                row is not None
                and row.field == proposal.OBLIGATION
                and await obligations.open_count(session) >= obligations.MAX_OPEN
            )
        if full:
            await answer_callback(
                bot, callback_id, CAP_REACHED.format(max=obligations.MAX_OPEN)
            )
            return
    await answer_callback(bot, callback_id)

    clock = clock or SystemClock()
    async with sessionmaker() as session:
        if action == "a":
            decided = await proposal.accept(session, clock, proposal_id)
        else:
            decided = await proposal.reject(session, clock, proposal_id)

    if decided is None:
        # Not pending: already decided, expired, or gone. Strip the
        # buttons without claiming an outcome that did not happen.
        async with sessionmaker() as session:
            row = await session.get(proposal.Proposal, proposal_id)
        text = confirm_text(row.field, row.value) + f"\n{STALE}" if row else STALE
        await edit_keyboard(bot, chat_id, message_id, text, None)
        return

    await show_decision_outcome(
        bot, chat_id=chat_id, message_id=message_id, decided=decided, accepted=action == "a"
    )

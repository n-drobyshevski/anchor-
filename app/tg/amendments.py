"""`/amendments` and its `am:x:<id>` [Отозвать] button (phase-5 plan
sections 3 and 9; milestone 5d).

Everything Telegram-shaped about persona amendments lives here;
app/core/amendments.py is the domain layer and imports no aiogram
types. `am:a:<id>`/`am:r:<id>` (adopting or declining a `persona_note`
review proposal) live in app/tg/review.py instead -- those two ids are
`review_proposal` ids, not `persona_amendment` ids, and the card that
carries them is one of the review's own proposal cards, not this
module's list.

E1a (anchor-eval-reliability-plan.md section 3.3): the trial's result
message names each case that did not pass cleanly with its count, «2 из
3», read from `eval_report["runs"]`, and the advisory items when the
trial had any (section 5.4). Ids and counts only, like the report.
"""

from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core import amendments as amendments_module
from app.core.amendments import DisplayRow
from app.core.clock import Clock
from app.core.prompt import PERSONA_PATH
from app.db.models import PersonaAmendment
from app.tg.send import answer_callback, edit_keyboard, send_keyboard, send_reply

logger = logging.getLogger(__name__)

REVOKE_LABEL = "Отозвать"
REVOKED_TEXT = "Отозвано."
STALE = "Устарело."

# How much of the amendment text a button label carries, matching
# app/tg/orders.py's own `[Снять] «{text[:24]}»` convention.
_LABEL_TEXT_MAX = 24

# The trial result's extra lines (module docstring).
UNEVEN_LINE = "Кейс {case}: {passed} из {runs}"
ADVISORY_LINE = "Не решали исход: {items}"


def render_amendments_list(rows: list[DisplayRow]) -> tuple[str, InlineKeyboardMarkup | None]:
    if not rows:
        return amendments_module.EMPTY_LIST_TEXT, None
    lines = []
    buttons = []
    for item in rows:
        amendment = item.amendment
        suffix = f" {amendments_module.STALE_CHANGED}" if item.stale else ""
        lines.append(f"«{amendment.text}»{suffix}")
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{REVOKE_LABEL} «{amendment.text[:_LABEL_TEXT_MAX]}»",
                    callback_data=f"am:x:{amendment.id}",
                )
            ]
        )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def run_amendments_list(
    sessionmaker, bot: Bot, *, chat_id: int, persona_path: Path = PERSONA_PATH
) -> None:
    async with sessionmaker() as session:
        rows = await amendments_module.list_for_display(session, persona_path)
    text, markup = render_amendments_list(rows)
    await send_keyboard(bot, chat_id, text, markup)


async def handle_revoke_callback(
    sessionmaker,
    bot: Bot,
    clock: Clock,
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    data: str,
    persona_path: Path = PERSONA_PATH,
) -> None:
    """`am:x:<id>` -- `/amendments`' own [Отозвать], re-rendering the list."""
    await answer_callback(bot, callback_id)
    _, _, raw_id = data.split(":", 2)

    try:
        amendment_id = int(raw_id)
    except ValueError:
        await edit_keyboard(bot, chat_id, message_id, STALE, None)
        return

    async with sessionmaker() as session:
        ok = await amendments_module.revoke(session, amendment_id, clock=clock)

    if not ok:
        await edit_keyboard(bot, chat_id, message_id, STALE, None)
        return

    async with sessionmaker() as session:
        rows = await amendments_module.list_for_display(session, persona_path)
    text, markup = render_amendments_list(rows)
    await edit_keyboard(bot, chat_id, message_id, text, markup)


async def send_trial_result(sessionmaker, bot: Bot, *, chat_id: int, amendment_id: int) -> None:
    """The `amendment_trial` job's result message (app/worker.py's own
    post-processing hook, the same shape as
    app/tg/orders.py's `send_order_proposal`)."""
    async with sessionmaker() as session:
        row = await session.get(PersonaAmendment, amendment_id)
    if row is None:
        return
    await send_reply(bot, chat_id, render_trial_result(row))


def render_trial_result(row: PersonaAmendment) -> str:
    """The result message: accepted or not, then one line per case whose
    runs did not all pass («Кейс 04: 2 из 3»), then the advisory items.
    A report from before E1a, or from a trial of one run per case, is
    the one line it always was."""
    lines = [
        amendments_module.ACTIVE_TEXT
        if row.status == amendments_module.ACTIVE
        else amendments_module.FAILED_TEXT
    ]
    report = row.eval_report or {}
    for case_id, counts in sorted((report.get("runs") or {}).items()):
        runs, passed = int(counts.get("runs", 0)), int(counts.get("passed", 0))
        # One run per case (EVAL_RUNS_PER_CASE=1) says nothing «из»
        # adds: the message stays the one line it was before E1a.
        if runs > 1 and passed < runs:
            lines.append(UNEVEN_LINE.format(case=case_id, passed=passed, runs=runs))
    advisory = report.get("advisory") or []
    if advisory:
        lines.append(ADVISORY_LINE.format(items=", ".join(advisory)))
    return "\n".join(lines)

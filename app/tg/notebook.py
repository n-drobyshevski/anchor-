"""`/mind` and `/mind add`, plus the `nb:x:<id>` close button (plan section 6).

Everything Telegram-shaped about the notebook lives here; app/core/
notebook.py is the domain layer and imports no aiogram types -- same
split as app/tg/memory.py and app/core/memory.py.

**The user can close any entry, including Anchor's.** `handle_close_
callback` always calls `notebook.close_entry(..., by="user")`, which
(per that function's own docstring) accepts any `source`. There is
therefore no ownership check to get wrong here: the one rule this
milestone protects -- Anchor cannot close a user- or review-authored
entry -- lives entirely on the *other* path, `run_notebook_reflect`,
and is enforced there and in `validate()`.

**Idempotency.** `/mind` is read-only, so it needs no replay gate.
`/mind add` mutates and goes through router.py's `_once`, exactly like
`/remember`. The close callback needs no gate of its own: it is a
single `active -> inactive` transition, so a replayed press finds the
row already closed and gets the same STALE answer a genuinely stale id
would.

Callback data: `nb:x:<id>` -- mirrors app/tg/memory.py's `m:k:...`/
`m:p:...` shape and stays far inside Telegram's 64-byte limit.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.core import notebook
from app.core.clock import Clock
from app.tg.memory import STALE
from app.tg.send import answer_callback, edit_keyboard, send_keyboard

logger = logging.getLogger(__name__)

# Plan section 10's three labels, in the fixed order app/core/prompt.py
# already renders them in -- kept in sync by naming the same attribute
# on NotebookView rather than re-deriving the order.
_KIND_LABELS = (
    ("intentions", "Намерения"),
    ("observations", "Наблюдения"),
    ("threads", "Незакрытое"),
)

MIND_EMPTY = "Заметок пока нет."
MIND_ADD_USAGE = "Что за намерение? Напиши так: /mind add быть добрее к себе."

ADD_REPLIES: dict[str, str] = {
    "ok": "Записал.",
    "refused": "Такое не записываю.",
    "cap": "Сначала закрой одно из намерений.",
    "too_long": "Слишком длинно — до 240 символов.",
    "duplicate": "Такое уже записано.",
}


def render_list(view: notebook.NotebookView) -> tuple[str, InlineKeyboardMarkup | None]:
    """`#id текст` grouped by kind, one `[✖ #id]` button per entry."""
    by_key = {
        "intentions": view.intentions,
        "observations": view.observations,
        "threads": view.threads,
    }
    lines: list[str] = []
    buttons: list[list[InlineKeyboardButton]] = []
    for key, label in _KIND_LABELS:
        items = by_key[key]
        if not items:
            continue
        lines.append(f"{label}:")
        for entry_id, text, _source in items:
            lines.append(f"#{entry_id} {text}")
            buttons.append(
                [InlineKeyboardButton(text=f"✖ #{entry_id}", callback_data=f"nb:x:{entry_id}")]
            )

    if not lines:
        return MIND_EMPTY, None
    markup = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    return "\n".join(lines), markup


async def run_mind(sessionmaker, bot: Bot, *, chat_id: int) -> None:
    async with sessionmaker() as session:
        view = await notebook.active_entries(session)
    text, markup = render_list(view)
    await send_keyboard(bot, chat_id, text, markup)


async def run_mind_add(sessionmaker, settings: Settings, clock: Clock, *, text: str) -> str:
    """`/mind add <текст>`. Returns the reply text."""
    async with sessionmaker() as session:
        result = await notebook.add_user_intention(session, settings, text, clock=clock)
    return ADD_REPLIES[result]


async def handle_close_callback(
    sessionmaker,
    bot: Bot,
    clock: Clock,
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    data: str,
) -> None:
    """`nb:x:<id>` -- close one entry, then re-render the list in place."""
    await answer_callback(bot, callback_id)
    _, _, raw_id = data.split(":", 2)

    try:
        entry_id = int(raw_id)
    except ValueError:
        await edit_keyboard(bot, chat_id, message_id, STALE, None)
        return

    async with sessionmaker() as session:
        closed = await notebook.close_entry(session, entry_id, by="user", clock=clock)

    if not closed:
        await edit_keyboard(bot, chat_id, message_id, STALE, None)
        return

    async with sessionmaker() as session:
        view = await notebook.active_entries(session)
    text, markup = render_list(view)
    await edit_keyboard(bot, chat_id, message_id, text, markup)

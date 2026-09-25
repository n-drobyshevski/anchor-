"""/export and /delete (plan section 11).

The two commands that give the user control over what Phase 2 started
storing about them.

**The confirmation is an edit, not a new message**, and that is load
bearing rather than cosmetic. /delete wipes `telegram_update`, which
includes the row for the very callback being handled, and
`message.update_id` is a foreign key into that table -- so storing the
«Удалено…» reply as a message afterwards would violate the constraint,
and storing it beforehand would leave a row the wipe then deletes.
Editing the existing message sidesteps both, and hands back idempotency
for free: a replayed press re-runs a wipe over already-empty tables and
re-edits to identical text, which edit_keyboard already swallows as
"message is not modified".

**The confirm button expires.** Section 11 specifies a two-step confirm
and no expiry; left literal, a `[Да, удалить]` sitting in scrollback
wipes everything irreversibly when tapped by accident weeks later. The
issue time rides in the callback data (`d:yes:<epoch>`), so both "not
that old button" and "not the one from an hour ago" fall out of one
check, with no new column and no bookkeeping about which request was
newest.
"""

from __future__ import annotations

import logging
import time

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.core import export, purge
from app.core import clock as clock_module
from app.core.clock import Clock, SystemClock
from app.core.outbound import cancel_outbound
from app.tg.send import DOCUMENT_LIMIT, answer_callback, edit_keyboard, send_document

logger = logging.getLogger(__name__)

# How long a delete confirmation stays live, in seconds.
CONFIRM_TTL = 300

CONFIRM_TEXT = "Удалить все данные? Это необратимо."
# 8b (phase-8 plan section 10): the one copy Anchor cannot reach, stated
# rather than implied. Obsidian Sync Standard keeps version history for
# a month (Plus: a year); the user is on Standard. Shown only when a
# vault is configured -- a vault line with no vault would be a lie too.
CONFIRM_VAULT_LINE = (
    "Файлы Anchor в хранилище тоже удалятся. Obsidian Sync хранит их "
    "в истории версий ещё до месяца, зашифрованными."
)
CONFIRM_YES = "Да, удалить"
CONFIRM_NO = "Отмена"

# Plan section 11's text says copies sit with xAI for up to 30 days.
# Both halves stopped being true: milestone 1e moved this bot to
# OpenRouter, and LLM_DATA_COLLECTION=deny routes only to providers that
# do not retain prompts at all. A privacy statement the user would act
# on has to describe what the code actually does.
DELETED_TEXT = (
    "Удалено. Запросы к модели идут через OpenRouter с запретом на хранение "
    "промптов — копий у провайдера не остаётся."
)
CANCELLED_TEXT = "Отменено."
STALE_TEXT = "Устарело."

EXPORT_CAPTION = "Все данные на {date}."
EXPORT_TOO_BIG = "Слишком много данных для одного файла ({size} МБ). Напиши — разберёмся."


def confirm_text(settings: Settings) -> str:
    if settings.VAULT_API_TOKEN:
        return CONFIRM_TEXT + "\n" + CONFIRM_VAULT_LINE
    return CONFIRM_TEXT


def confirm_keyboard(issued_at: int | None = None) -> InlineKeyboardMarkup:
    issued_at = int(time.time()) if issued_at is None else issued_at
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=CONFIRM_YES, callback_data=f"d:yes:{issued_at}"),
                InlineKeyboardButton(text=CONFIRM_NO, callback_data="d:no"),
            ]
        ]
    )


def is_fresh(issued_at: int, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return 0 <= now - issued_at <= CONFIRM_TTL


async def run_export(
    sessionmaker, bot: Bot, clock: Clock, *, chat_id: int, timezone: str
) -> bool:
    """Build and send the export. Returns False if it was too large to send.

    Only sizes and row counts are logged (plan section 11: "Never logs
    contents"). The rows go into the file and nowhere else.
    """
    async with sessionmaker() as session:
        payload = await export.build_export(session, clock)

    data = export.to_bytes(payload)
    counts = export.row_counts(payload)

    if len(data) > DOCUMENT_LIMIT:
        megabytes = len(data) / 1024 / 1024
        logger.warning("export too large to send", extra={"count": len(data)})
        await bot.send_message(chat_id, EXPORT_TOO_BIG.format(size=f"{megabytes:.0f}"))
        return False

    await send_document(
        bot,
        chat_id,
        data,
        export.export_filename(clock, timezone),
        caption=EXPORT_CAPTION.format(
            date=clock_module.local_date(clock, timezone).isoformat()
        ),
    )
    logger.info("export sent", extra={"count": len(data), "event": "export"})
    logger.info("export row counts", extra={"count": sum(counts.values())})
    return True


async def handle_delete_callback(
    sessionmaker,
    bot: Bot,
    settings: Settings,
    clock: Clock | None = None,
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    data: str,
) -> None:
    """`d:yes:<epoch>` / `d:no`."""
    clock = clock or SystemClock()
    await answer_callback(bot, callback_id)
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action != "yes":
        await edit_keyboard(bot, chat_id, message_id, CANCELLED_TEXT, None)
        return

    try:
        issued_at = int(parts[2])
    except (IndexError, ValueError):
        await edit_keyboard(bot, chat_id, message_id, STALE_TEXT, None)
        return

    if not is_fresh(issued_at):
        logger.info("stale delete confirmation ignored", extra={"event": "stale"})
        await edit_keyboard(bot, chat_id, message_id, STALE_TEXT, None)
        return

    async with sessionmaker() as session:
        # 3b (plan section 6): /delete cancels first. The wipe
        # truncates `outbound` and `job` anyway, so this is
        # belt and braces -- but the plan names /delete as a
        # cancel trigger, and a future wipe that spared a table
        # must not silently resurrect a scheduled message.
        await cancel_outbound(session, clock)
        await purge.delete_everything(session, settings, clock)

    await edit_keyboard(bot, chat_id, message_id, DELETED_TEXT, None)

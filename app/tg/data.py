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
from app.ops import backup
from app.tg.send import DOCUMENT_LIMIT, answer_callback, edit_keyboard, send_document
from app.web.hub import WebHub

logger = logging.getLogger(__name__)

# How long a delete confirmation stays live, in seconds.
CONFIRM_TTL = 300

CONFIRM_TEXT = "Удалить все данные и все резервные копии? Это необратимо."
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

# 6e (plan section 9.3): the wipe now also purges every backup object,
# and the confirm/final text says so. Superseded the 1e-era wording
# (which described only the OpenRouter side) because a privacy
# statement the user would act on has to describe what the code
# actually does, and 6e adds a whole other thing it does.
DELETED_TEXT = (
    "Удалено, включая бэкапы. Копии у провайдеров моделей удаляются по их "
    "правилам хранения."
)
# The bucket refused (or could not be reached) for some backup objects:
# the database is wiped, the backups may not be.
DELETED_BACKUPS_FAILED_TEXT = (
    "Данные удалены, но часть резервных копий удалить не удалось — удали их "
    "в бакете вручную (префикс anchor/). Копии у провайдеров моделей удаляются "
    "по их правилам хранения."
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
    hub: WebHub | None = None,
    claude_pending=None,
) -> None:
    """`d:yes:<epoch>` / `d:no`.

    `hub` (web-chat plan track 1/2, optional and keyword-only so every
    call site predating it -- there is exactly one production caller,
    app/tg/router.py's delete_decision, which always has a hub to pass
    once WEB_UI_ENABLED -- keeps working unchanged) is closed after a
    successful wipe below. Without this, a Telegram-issued `/delete`
    truncated `message` and `web_session` but never touched the hub's
    in-memory ring buffer: any later GET /api/events with a low
    Last-Event-ID would still replay the supposedly deleted
    conversation's text straight out of that buffer (a medium-severity
    finding). `/weblogout`'s own kill switch (app/web/auth.py's
    revoke_all) already does this for its own path; `/delete` needed
    the same call on this one, since app/core/purge.py itself has no
    idea a WebHub exists (by design -- see app/web/sink.py's docstring).
    """
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

    # 6e (plan section 9.3): purge every backup object under the
    # `anchor/` prefix before the database wipe. Order does not matter
    # for correctness -- the two are unrelated stores -- but doing it
    # first means a crash between the two steps leaves the smaller
    # blast radius (backups already gone, database still there) rather
    # than the reverse. A no-op, not a failure, when S3 isn't
    # configured: the database wipe still proceeds either way (backup.
    # purge_all_backups' own docstring).
    purged = await backup.purge_all_backups(settings)
    logger.info(
        "backup objects purged",
        extra={"event": "delete", "count": len(purged.deleted), "error_code": purged.failed or None},
    )

    async with sessionmaker() as session:
        # 3b (plan section 6): /delete cancels first. The wipe
        # truncates `outbound` and `job` anyway, so this is
        # belt and braces -- but the plan names /delete as a
        # cancel trigger, and a future wipe that spared a table
        # must not silently resurrect a scheduled message.
        await cancel_outbound(session, clock)
        await purge.delete_everything(session, settings, clock)
        # After the wipe, which truncates backup_log: the record of which
        # objects were purged is the one thing that must outlive it.
        await backup.record_purged(session, clock, purged)

    if hub is not None:
        hub.close_all()
    # The Claude connector's pending authorize requests live in memory
    # (app/web/oauth_store.py); the truncate took the rest.
    if claude_pending is not None:
        claude_pending.clear()

    # Never claim the backups are gone when the bucket said otherwise.
    final = DELETED_BACKUPS_FAILED_TEXT if purged.failed else DELETED_TEXT
    await edit_keyboard(bot, chat_id, message_id, final, None)

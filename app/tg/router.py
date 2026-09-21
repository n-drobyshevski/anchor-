"""aiogram Router: commands, text (store + echo), and a catch-all.

Handler registration order follows plan section 6.4:
1. Commands: /start, /state in 1b (/out, /in land in 1d).
2. Text: store the user message, then reply (turn.run() replaces the
   echo reply in 1c).
3. Anything else (stickers, photos, voice): a fixed "text only" reply,
   no LLM call.

build_router() takes sessionmaker/settings explicitly and closes over
them in its nested handlers, rather than using aiogram's dp[...]
workflow-data injection — matching this codebase's style of passing
dependencies in explicitly (app/db/session.py's factory, app/main.py's
wiring) instead of relying on framework DI magic.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BotCommand, Message, Update
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.spend import today_usd
from app.core.state import get_state
from app.db.models import Message as MessageRow

NON_TEXT_REPLY = "Пока только текст."

START_TEXT = (
    "Я — Anchor. Здесь по-русски, коротко и по делу.\n"
    "Выйти из роли можно командой /out или словом «пурпурный»."
)

BOT_COMMANDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="state", description="Текущее состояние"),
]


async def register_commands(bot) -> None:
    """set_my_commands on startup (plan section 12)."""
    await bot.set_my_commands(BOT_COMMANDS)


async def _store_user_message_once(
    session: AsyncSession, update_id: int | None, content: str
) -> None:
    """Insert a `message` row for this update, unless one already exists.

    Idempotency guard for queue replay (e.g. after worker.recover_stuck
    resets a crashed row back to pending): plan section 5 defines no
    unique constraint on message.update_id, so this is a check-then-
    insert. That is race-free only because the worker's concurrency is
    1 (plan section 6.3) — never call this from more than one worker.

    # TODO(phase-1c): this call moves into core/turn.py's idempotent
    # turn (plan section 8 step 1), alongside the assistant-side row
    # (reply_to_update, sent_at) — do not write that row here.
    """
    if update_id is not None:
        existing = await session.execute(
            select(MessageRow.id).where(
                MessageRow.update_id == update_id, MessageRow.role == "user"
            )
        )
        if existing.scalar_one_or_none() is not None:
            return
    session.add(MessageRow(role="user", content=content, update_id=update_id, ooc=False))
    await session.commit()


def _format_state(user_state, spend, settings: Settings) -> str:
    now_local = datetime.datetime.now(ZoneInfo(user_state.timezone)).strftime("%Y-%m-%d %H:%M")
    return (
        "Персона: {persona}\n"
        "Интенсивность: {intensity}/5\n"
        "Локальное время: {time} ({tz})\n"
        "Потрачено сегодня: {spend:.2f} / {cap:.2f} USD\n"
        "Модель: {model}"
    ).format(
        persona="вкл" if user_state.persona_active else "выкл",
        intensity=user_state.intensity,
        time=now_local,
        tz=user_state.timezone,
        spend=spend,
        cap=settings.DAILY_USD_CAP,
        model=settings.XAI_MODEL,
    )


def build_router(sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> Router:
    """Build a fresh Router with 1b's handlers.

    A factory rather than a shared module-level instance, because a
    Router can only ever be attached to one Dispatcher — tests that
    build several Dispatchers each need their own Router instance.
    """
    router = Router(name="anchor")

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        await message.answer(START_TEXT)

    @router.message(Command("state"))
    async def state(message: Message) -> None:
        async with sessionmaker() as session:
            user_state = await get_state(session)
            spend = await today_usd(session, user_state.timezone)
        await message.answer(_format_state(user_state, spend, settings))

    # TODO(phase-1d): /out (hard pause, section 7) and /in (resume) command
    # handlers go here, registered before the text handler below.

    @router.message(F.text)
    async def handle_text(message: Message, event_update: Update) -> None:
        async with sessionmaker() as session:
            await _store_user_message_once(session, event_update.update_id, message.text)

        # TODO(phase-1d): pause.match(message.text) branch goes here,
        # before turn.run() (plan section 6.4 step 2, section 7).
        # TODO(phase-1c): replace this echo with turn.run() — persona
        # reply via the LLM provider, idempotent, plan section 8.
        await message.answer(message.text)

    @router.message()
    async def handle_other(message: Message) -> None:
        """Stickers, photos, voice, etc. — no LLM call (plan section 6.4 step 3)."""
        await message.answer(NON_TEXT_REPLY)

    return router

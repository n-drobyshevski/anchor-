"""aiogram Router: commands, text (persona turn), and a catch-all.

Handler registration order follows plan section 6.4:
1. Commands: /start, /state (1b); /out, /in (1d); /search (1f) --
   registered before the F.text handler so aiogram's first-match-wins
   routes it as a command turn.run() call (web_search=True), never the
   plain-text branch.
2. Text: turn.run() — the idempotent persona turn (plan section 8),
   which now owns storing the user message too (moved into core/
   turn.py in 1c; see that module), and, as of 1d, the pause-word
   branch (plan section 7) as well. That branch is not handled here:
   it lives in turn.run() so it inherits the turn's idempotency
   rather than needing its own.
3. Anything else (stickers, photos, voice): a fixed "text only" reply,
   no LLM call.

build_router() takes sessionmaker/settings/provider explicitly and
closes over them in its nested handlers, rather than using aiogram's
dp[...] workflow-data injection — matching this codebase's style of
passing dependencies in explicitly (app/db/session.py's factory, app/
main.py's wiring) instead of relying on framework DI magic. The
provider is built once in app/main.py and threaded through
build_dispatcher() so a single AsyncOpenAI client (and its connection
pool) is shared across every turn.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, Message, Update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core import turn
from app.core.spend import today_usd
from app.core.state import get_state
from app.llm.provider import LLMProvider

NON_TEXT_REPLY = "Пока только текст."

START_TEXT = (
    "Я — Anchor. Здесь по-русски, коротко и по делу.\n"
    "Выйти из роли можно командой /out или словом «пурпурный»."
)

BOT_COMMANDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="state", description="Текущее состояние"),
    BotCommand(command="out", description="Пауза, выйти из роли"),
    BotCommand(command="in", description="Вернуться в роль"),
    BotCommand(command="search", description="Найти в сети и ответить"),
]


async def register_commands(bot) -> None:
    """set_my_commands on startup (plan section 12)."""
    await bot.set_my_commands(BOT_COMMANDS)


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
        model=settings.LLM_MODEL,
    )


def build_router(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, provider: LLMProvider
) -> Router:
    """Build a fresh Router with 1b's commands and 1c's persona turn.

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

    @router.message(Command("out"))
    async def out(message: Message, event_update: Update) -> None:
        # source="command": /out is a typed command, not a pause word.
        # The state_change audit log is the only record of which one
        # switched the persona off.
        #
        # 2a: ensure_scene here too, not just in turn.run(). A command
        # that produces a turn is an inbound message like any other
        # (phase-2 plan section 5), so arriving as /out after six hours
        # of silence must close the stale scene exactly as a chat
        # message would.
        scene_id = await turn.ensure_scene(sessionmaker, settings)
        await turn.run_hard_pause(
            sessionmaker,
            message.bot,
            chat_id=message.chat.id,
            update_id=event_update.update_id,
            source="command",
            scene_id=scene_id,
        )

    @router.message(Command("in"))
    async def resume(message: Message, event_update: Update) -> None:
        scene_id = await turn.ensure_scene(sessionmaker, settings)
        await turn.run_resume(
            sessionmaker,
            message.bot,
            chat_id=message.chat.id,
            update_id=event_update.update_id,
            scene_id=scene_id,
        )

    @router.message(Command("search"))
    async def search(message: Message, event_update: Update, command: CommandObject) -> None:
        """/search <query>: opt-in web search (plan milestone 1f).

        Routes through the same turn.run() the text handler uses, with
        web_search=True and the query as user_text -- never a hand-rolled
        model call here, so pause words, idempotency, the spend cap and
        neutral mode all still apply exactly as they do to an ordinary turn.
        """
        if not settings.LLM_WEB_SEARCH:
            await turn.run_search_canned_reply(
                sessionmaker,
                message.bot,
                chat_id=message.chat.id,
                update_id=event_update.update_id,
                text=turn.SEARCH_DISABLED_REPLY_TEXT,
                scene_id=await turn.ensure_scene(sessionmaker, settings),
            )
            return

        query = (command.args or "").strip()
        if not query:
            await turn.run_search_canned_reply(
                sessionmaker,
                message.bot,
                chat_id=message.chat.id,
                update_id=event_update.update_id,
                text=turn.SEARCH_EMPTY_REPLY_TEXT,
                scene_id=await turn.ensure_scene(sessionmaker, settings),
            )
            return

        await turn.run(
            sessionmaker,
            message.bot,
            settings,
            provider,
            chat_id=message.chat.id,
            update_id=event_update.update_id,
            user_text=query,
            web_search=True,
        )

    @router.message(F.text)
    async def handle_text(message: Message, event_update: Update) -> None:
        await turn.run(
            sessionmaker,
            message.bot,
            settings,
            provider,
            chat_id=message.chat.id,
            update_id=event_update.update_id,
            user_text=message.text,
        )

    @router.message()
    async def handle_other(message: Message) -> None:
        """Stickers, photos, voice, etc. — no LLM call (plan section 6.4 step 3)."""
        await message.answer(NON_TEXT_REPLY)

    return router

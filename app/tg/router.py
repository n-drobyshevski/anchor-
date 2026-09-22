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

2b adds the memory commands and this bot's first callback_query
handlers (plan section 11). The commands go **before** the F.text
handler for the reason stated above -- registered after it, they are
shadowed by it and every /remember is answered by the persona instead.
The callback handlers go last: aiogram routes callback_query to its own
observer, so the @router.message() catch-all cannot swallow them and
their position is free.

Every mutating memory command goes through _once(), the same replay
gate app/core/turn.py's run_hard_pause uses: the worker re-runs an
update after a crash or a stuck sweep, and without the gate a replayed
/remember would park a second pending row and a replayed /forget would
write a second audit row.

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
from aiogram.types import BotCommand, CallbackQuery, Message, Update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core import turn
from app.core.spend import today_usd
from app.core.state import get_state
from app.llm.provider import LLMProvider
from app.tg import memory as memory_ui
from app.tg import proposals as proposals_ui

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
    BotCommand(command="remember", description="Запомнить факт"),
    BotCommand(command="memories", description="Что я помню"),
    BotCommand(command="forget", description="Забыть запись по id"),
    BotCommand(command="pin", description="Закрепить запись"),
    BotCommand(command="unpin", description="Открепить запись"),
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

    # --- 2b: memory (plan section 11) ---

    async def _once(update_id: int) -> bool:
        """True iff this update has not been handled before.

        The worker replays an update after any crash between feed_update
        and complete(), and after the 60s stuck sweep. Mutating commands
        must be gated on this, exactly as turn.run_hard_pause is.
        """
        return not await turn.already_handled(sessionmaker, update_id)

    async def _reply_once(message: Message, update_id: int, text: str) -> None:
        """Store-and-send a canned command reply, idempotently."""
        await turn.send_command_reply(
            sessionmaker,
            message.bot,
            chat_id=message.chat.id,
            update_id=update_id,
            text=text,
            scene_id=await turn.ensure_scene(sessionmaker, settings),
        )

    @router.message(Command("remember"))
    async def remember(message: Message, event_update: Update, command: CommandObject) -> None:
        text = (command.args or "").strip()
        if not text:
            await _reply_once(message, event_update.update_id, memory_ui.REMEMBER_USAGE)
            return
        if len(text) > memory_ui.MEMORY_TEXT_MAX:
            await _reply_once(
                message,
                event_update.update_id,
                memory_ui.TOO_LONG.format(max=memory_ui.MEMORY_TEXT_MAX),
            )
            return
        # Not a canned reply: this one carries a keyboard, so it is sent
        # directly and gated on _once rather than on a stored row.
        if not await _once(event_update.update_id):
            return
        await turn.ensure_scene(sessionmaker, settings)
        await memory_ui.run_remember(
            sessionmaker, message.bot, chat_id=message.chat.id, text=text
        )
        await turn.mark_update_handled(
            sessionmaker, update_id=event_update.update_id, text=memory_ui.REMEMBER_PROMPT.format(text=text)
        )

    @router.message(Command("memories"))
    async def memories(message: Message, event_update: Update) -> None:
        if not await _once(event_update.update_id):
            return
        await memory_ui.run_memories(sessionmaker, message.bot, chat_id=message.chat.id)
        await turn.mark_update_handled(
            sessionmaker, update_id=event_update.update_id, text="[/memories]"
        )

    @router.message(Command("forget"))
    async def forget(message: Message, event_update: Update, command: CommandObject) -> None:
        memory_id = memory_ui.parse_id(command.args)
        if memory_id is None:
            await _reply_once(message, event_update.update_id, memory_ui.FORGET_USAGE)
            return
        if not await _once(event_update.update_id):
            return
        reply = await memory_ui.run_forget(
            sessionmaker, message.bot, chat_id=message.chat.id, memory_id=memory_id
        )
        await _reply_once(message, event_update.update_id, reply)

    @router.message(Command("pin"))
    async def pin(message: Message, event_update: Update, command: CommandObject) -> None:
        await _toggle_pin(message, event_update, command, pinned=True)

    @router.message(Command("unpin"))
    async def unpin(message: Message, event_update: Update, command: CommandObject) -> None:
        await _toggle_pin(message, event_update, command, pinned=False)

    async def _toggle_pin(
        message: Message, event_update: Update, command: CommandObject, *, pinned: bool
    ) -> None:
        memory_id = memory_ui.parse_id(command.args)
        if memory_id is None:
            await _reply_once(
                message,
                event_update.update_id,
                memory_ui.PIN_USAGE if pinned else memory_ui.UNPIN_USAGE,
            )
            return
        # Pinning is naturally idempotent (setting pinned=True twice is
        # the same state), so no _once gate is needed here.
        reply = await memory_ui.run_set_pinned(
            sessionmaker, settings, memory_id=memory_id, pinned=pinned
        )
        await _reply_once(message, event_update.update_id, reply)

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

    @router.callback_query(F.data.startswith("m:k:"))
    async def memory_kind(callback: CallbackQuery) -> None:
        await memory_ui.handle_kind_callback(
            sessionmaker,
            callback.bot,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            data=callback.data,
        )

    @router.callback_query(F.data.startswith("m:p:"))
    async def memory_page(callback: CallbackQuery) -> None:
        await memory_ui.handle_page_callback(
            sessionmaker,
            callback.bot,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            data=callback.data,
        )

    @router.callback_query(F.data.startswith("p:"))
    async def proposal_decision(callback: CallbackQuery) -> None:
        """`p:a:<id>` / `p:r:<id>` -- the extractor's confirmation buttons."""
        await proposals_ui.handle_decision_callback(
            sessionmaker,
            callback.bot,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            data=callback.data,
        )

    @router.callback_query()
    async def unknown_callback(callback: CallbackQuery) -> None:
        """Always answer, or the button spins in the client until it times out."""
        await callback.answer(memory_ui.STALE)

    @router.message()
    async def handle_other(message: Message) -> None:
        """Stickers, photos, voice, etc. — no LLM call (plan section 6.4 step 3)."""
        await message.answer(NON_TEXT_REPLY)

    return router

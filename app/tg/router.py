"""aiogram Router: commands, text (persona turn), and a catch-all.

Handler registration order follows plan section 6.4:
1. Commands: /start, /state (1b); /out, /in (1d) -- registered before
   the F.text handler so aiogram's first-match-wins routes each one to
   its own handler, never the plain-text branch.
2. Text: turn.run() — the idempotent persona turn (plan section 8),
   which now owns storing the user message too (moved into core/
   turn.py in 1c; see that module), and, as of 1d, the pause-word
   branch (plan section 7) as well. That branch is not handled here:
   it lives in turn.run() so it inherits the turn's idempotency
   rather than needing its own.
3. Anything else (stickers, photos, voice): a fixed "text only" reply,
   no LLM call.

2d adds /checkin, /due and /focus, the `c:*` callbacks, and the
codebase's first middleware -- an outer one on the message observer
that clears a pending `awaiting` step on any slash command (plan
section 9). It is registered inside build_router so each Dispatcher a
test builds gets its own, exactly like the handlers.

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


from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, CallbackQuery, Message, Update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core import turn
from app.core import clock as clock_module
from app.core.clock import Clock, SystemClock
from app.core import checkin as checkin_core
from app.core import memory as memory_core
from app.core import safety_events
from app.core import proposal as proposal_core
from app.core.outbound import cancel_outbound, load_state_summary
from app.core.quiet import OFF as QUIET_OFF
from app.core.quiet import clamp as clamp_quiet
from app.core.quiet import parse as parse_quiet
from app.core.spend import today_by_category, today_usd
from app.core.state import get_state, update_state
from app.llm.provider import LLMProvider
from app.planner import actions as planner_actions
from app.planner import auth as planner_auth
from app.planner import parse as planner_parse
from app.planner import snapshot as planner_snapshot
from app.tg.send import send_keyboard
from app.tg import checkin as checkin_ui
from app.tg import data as data_ui
from app.tg import memory as memory_ui
from app.tg import planner as planner_ui
from app.tg import proposals as proposals_ui
from app.tg import research as research_ui
from app.tg import welfare as welfare_ui

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
    BotCommand(command="remember", description="Запомнить факт"),
    BotCommand(command="memories", description="Что я помню"),
    BotCommand(command="forget", description="Забыть запись по id"),
    BotCommand(command="pin", description="Закрепить запись"),
    BotCommand(command="unpin", description="Открепить запись"),
    BotCommand(command="checkin", description="Чек-ин за день"),
    BotCommand(command="due", description="Главное действие"),
    BotCommand(command="focus", description="Фокус вкл/выкл"),
    BotCommand(command="quiet", description="Тишина на время"),
    BotCommand(command="tz", description="Часовой пояс"),
    BotCommand(command="export", description="Выгрузить все данные"),
    BotCommand(command="delete", description="Удалить все данные"),
    # 4b (phase-4 plan section 9): read/notes/card/adopt/reject, /read's
    # loop end to end. 4c adds /study alongside them -- see the module
    # docstring on why all six are worth a menu entry (mirrors /forget,
    # /pin, /unpin above, which also take an id or argument typed by
    # hand rather than offering a picker).
    BotCommand(command="study", description="Найти карточки по теме"),
    BotCommand(command="read", description="Прочитать страницу"),
    BotCommand(command="notes", description="Карточки исследований"),
    BotCommand(command="card", description="Карточка по id"),
    BotCommand(command="adopt", description="Принять карточку"),
    BotCommand(command="reject", description="Отклонить карточку"),
    # P2 (design review section 2.3): the read path + link flow.
    BotCommand(command="plan", description="План на сегодня"),
    BotCommand(command="planner", description="Статус планера, on/off"),
    BotCommand(command="planner_link", description="Подключить планер"),
    # P3: explicit writes, behind a confirm card (/task, /event) or a
    # pick-from-list button (/done).
    BotCommand(command="task", description="Добавить задачу в планер"),
    BotCommand(command="event", description="Добавить событие в планер"),
    BotCommand(command="done", description="Отметить задачу сделанной"),
]

QUIET_SET = "Тихо до {until}."
QUIET_OFF_REPLY = "Снова на связи."
QUIET_USAGE = "Сколько? /quiet 2h, /quiet 30m, /quiet 1d или /quiet off."
QUIET_CLAMPED = "Тихо до {until} — дольше {days} дн. подряд не ставлю."

TZ_SET = "Часовой пояс: {tz}. Сейчас у тебя {time}."
TZ_UNKNOWN = "Не знаю такой пояс. Пример: Europe/Paris."
TZ_USAGE = "Какой пояс? Пример: /tz Europe/Paris."

# /state's outbound block (plan section 10).
OUTBOUND_KIND_LABELS = {
    "morning": "утро",
    "evening_nag": "вечер",
    "silence": "тишина",
    "tick": "тик",
}
NOTHING = "—"

DUE_CLEARED = "Главное действие снято."
DUE_SET = "Главное действие: «{text}»."
FOCUS_USAGE = "Как именно? /focus on или /focus off."
FOCUS_ON = "Фокус включён."
FOCUS_OFF = "Фокус выключен."


async def register_commands(bot) -> None:
    """set_my_commands on startup (plan section 12)."""
    await bot.set_my_commands(BOT_COMMANDS)


def _format_outbound(summary, tz: ZoneInfo, now_utc) -> list[str]:
    """/state's three proactive lines (plan section 10).

    Kept beside _format_state rather than inside it because it is the
    one block whose absence is meaningful: before 3b there was nothing
    to say, and a summary of None still renders, as three lines of
    "nothing yet", rather than silently disappearing.
    """
    if summary is None:
        return []

    if summary.quiet_until is not None and summary.quiet_until > now_utc:
        quiet = summary.quiet_until.astimezone(tz).strftime("%d.%m %H:%M")
    else:
        quiet = NOTHING

    if summary.next_kind is None:
        upcoming = NOTHING
    else:
        label = OUTBOUND_KIND_LABELS.get(summary.next_kind, summary.next_kind)
        when = summary.next_planned_for.astimezone(tz).strftime("%H:%M")
        upcoming = f"{label} в {when}"

    return [
        f"Тихо до: {quiet} · Без ответа подряд: {summary.ignored_in_row}",
        f"Сам написал сегодня: {summary.sent_today} / {summary.max_per_day}",
        f"Следующее: {upcoming} · Последний отказ: "
        f"{summary.last_skip_reason or NOTHING}",
    ]


def _format_state(
    user_state,
    spend,
    settings: Settings,
    clock: Clock,
    *,
    by_category=None,
    memories=0,
    outbound=None,
    welfare_counts=None,
    research_counts=None,
) -> str:
    """Plan section 11's /state: Phase 1's fields plus 2c/2d's.

    Spend is broken down by ledger category so a day where the
    background jobs cost more than the conversation is visible at a
    glance rather than hidden inside one total.
    """
    tz = ZoneInfo(user_state.timezone)
    now = clock_module.now_local(clock, user_state.timezone)
    today = now.date()
    now_local = now.strftime("%Y-%m-%d %H:%M")

    if user_state.last_checkin_at is None:
        last_checkin = "давно"
    else:
        local = user_state.last_checkin_at.astimezone(tz)
        days = (today - local.date()).days
        when = "сегодня" if days <= 0 else "вчера" if days == 1 else f"{days} дн. назад"
        last_checkin = f"{when} {local.strftime('%H:%M')}"

    if user_state.due_action:
        due = f"«{user_state.due_action}»"
        if user_state.due_set_at:
            days = (today - user_state.due_set_at.astimezone(tz).date()).days
            due += " (задано сегодня)" if days <= 0 else f" (задано {days} дн. назад)"
    else:
        due = "нет"

    # H2. Its own line rather than part of _format_outbound's block:
    # that helper returns nothing at all when there is no summary, and
    # this is not a proactive-message line -- it answers "is the welfare
    # check actually running", which matters most on a quiet week when
    # the outbound block has nothing to say.
    welfare_line = ""
    if welfare_counts is not None:
        ok, failures = welfare_counts
        welfare_line = (
            f"Проверка благополучия ({safety_events.WINDOW_DAYS} дн.): "
            f"ok {ok} · сбои {failures}\n"
        )

    # 4d fixes. Shown only once there is something to show, unlike the
    # welfare line: the welfare check runs on ordinary turns and a line
    # of zeroes there means "it has stopped", while research only runs
    # when asked, so a permanent "0 · 0" would be noise for anyone who
    # does not use /study or /read.
    #
    # The number that matters is the failures. A distiller returning
    # unparseable JSON makes `done` jobs with no cards, which reads as a
    # quiet week of unhelpful pages until this line says otherwise.
    research_line = ""
    if research_counts is not None:
        distill_counts, search_counts = research_counts
        if any(distill_counts) or any(search_counts):
            research_line = (
                f"Исследования ({safety_events.WINDOW_DAYS} дн.): "
                f"разбор ok {distill_counts[0]} · сбои {distill_counts[1]} · "
                f"поиск ok {search_counts[0]} · сбои {search_counts[1]}\n"
            )

    breakdown = ""
    if by_category:
        breakdown = " · " + " · ".join(f"{name} {total:.2f}" for name, total in by_category.items())

    return (
        "Персона: {persona}\n"
        "Интенсивность: {intensity}/5 · Фокус: {focus}\n"
        "Серия: {streak} дн. · Последний чек-ин: {last_checkin}\n"
        "Главное действие: {due}\n"
        "{outbound}"
        "{welfare}"
        "{research}"
        "Помню: {memories} записей\n"
        "Локальное время: {time} ({tz})\n"
        "Потрачено сегодня: {spend:.2f} / {cap:.2f} USD{breakdown}\n"
        "Модель: {model}"
    ).format(
        persona="вкл" if user_state.persona_active else "выкл",
        intensity=user_state.intensity,
        focus="вкл" if user_state.focus_on else "выкл",
        streak=user_state.streak,
        last_checkin=last_checkin,
        due=due,
        outbound="".join(
            line + "\n" for line in _format_outbound(outbound, tz, clock.now_utc())
        ),
        welfare=welfare_line,
        research=research_line,
        memories=memories,
        time=now_local,
        tz=user_state.timezone,
        spend=spend,
        cap=settings.DAILY_USD_CAP,
        breakdown=breakdown,
        model=settings.LLM_MODEL,
    )


def build_router(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    provider: LLMProvider,
    safety_provider: LLMProvider | None = None,
    clock: Clock | None = None,
) -> Router:
    """Build a fresh Router with 1b's commands and 1c's persona turn.

    A factory rather than a shared module-level instance, because a
    Router can only ever be attached to one Dispatcher — tests that
    build several Dispatchers each need their own Router instance.

    `safety_provider` (2e, renamed in H2) is what runs the welfare
    classifier beside each in-character generation. It defaults to None
    so that the many tests predating 2e keep their three-argument call,
    and a turn without it simply skips the check — but app/main.py always
    supplies it, and tests/test_welfare.py asserts that this function
    threads it into every turn.run() call, so production cannot quietly
    lose it.

    It was `cheap_provider` until H2, when the welfare classifier moved
    off the shared background model onto LLM_MODEL_SAFETY. The router
    never used it for anything else, so the old name would now describe
    the wrong model.
    """
    # 3a: one clock for every handler in this router. Defaulted
    # rather than required, matching safety_provider above -- the
    # tests predating Phase 3 keep their shorter call, and
    # app/main.py always passes the real one.
    clock = clock or SystemClock()

    router = Router(name="anchor")

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        await message.answer(START_TEXT)

    @router.message(Command("state"))
    async def state(message: Message) -> None:
        async with sessionmaker() as session:
            user_state = await get_state(session)
            spend = await today_usd(session, clock, user_state.timezone)
            by_category = await today_by_category(
                session, clock, user_state.timezone
            )
            memories = await memory_core.count_active(session)
            outbound = await load_state_summary(session, clock, settings, user_state)
            welfare_counts = await safety_events.counts(
                session, clock, user_state.timezone
            )
            research_counts = (
                await safety_events.counts(
                    session, clock, user_state.timezone, kind=safety_events.DISTILL
                ),
                await safety_events.counts(
                    session, clock, user_state.timezone, kind=safety_events.SEARCH
                ),
            )
        await message.answer(
            _format_state(
                user_state,
                spend,
                settings,
                clock,
                by_category=by_category,
                memories=memories,
                outbound=outbound,
                welfare_counts=welfare_counts,
                research_counts=research_counts,
            )
        )

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
        scene_id = await turn.ensure_scene(sessionmaker, settings, clock)
        await turn.run_hard_pause(
            sessionmaker,
            message.bot,
            clock=clock,
            chat_id=message.chat.id,
            update_id=event_update.update_id,
            source="command",
            scene_id=scene_id,
        )

    @router.message(Command("in"))
    async def resume(message: Message, event_update: Update) -> None:
        scene_id = await turn.ensure_scene(sessionmaker, settings, clock)
        await turn.run_resume(
            sessionmaker,
            message.bot,
            clock=clock,
            chat_id=message.chat.id,
            update_id=event_update.update_id,
            scene_id=scene_id,
        )

    # --- 2d: clearing `awaiting` on any command (plan section 9) ---

    @router.message.outer_middleware()
    async def clear_awaiting_on_command(handler, event, data):
        """Any slash command clears a pending conversational step.

        Plan section 9 states this as a blanket rule, so it is enforced
        by a blanket mechanism rather than a line at the top of each of
        the fourteen command handlers -- one that a future command would
        eventually forget.

        This is the codebase's first middleware, and deliberately not
        the dependency injection this module's docstring says the repo
        avoids: it carries no dependencies into handlers, it enforces an
        invariant. Outer rather than inner so it runs before filters,
        which means it also fires for a command no handler matches.
        """
        text = getattr(event, "text", None) or ""
        if text.startswith("/"):
            async with sessionmaker() as session:
                await checkin_core.clear_awaiting(session)
        return await handler(event, data)

    # --- 2d: check-in, /due, /focus (plan section 9) ---

    async def _expire_proposal_for(message: Message, field: str) -> None:
        """A direct command outranks an outstanding proposal for the same field.

        Otherwise a live `Принять` would sit there waiting to overwrite
        what the user just typed. Reuses the expiry machinery plan
        section 8 already defines for one proposal superseding another.
        """
        async with sessionmaker() as session:
            pending = await proposal_core.get_pending(session)
            if pending is None or pending.field != field:
                return
            proposal_id = pending.id
            pending.status = proposal_core.EXPIRED
            pending.decided_at = clock.now_utc()
            await session.commit()
        await proposals_ui.retire_buttons(
            sessionmaker, message.bot, chat_id=message.chat.id, proposal_id=proposal_id
        )

    @router.message(Command("checkin"))
    async def checkin_command(message: Message, event_update: Update) -> None:
        if not await _once(event_update.update_id):
            return
        async with sessionmaker() as session:
            user_state = await get_state(session)
        await turn.ensure_scene(sessionmaker, settings, clock)
        await checkin_ui.start(
            sessionmaker,
            message.bot,
            clock,
            chat_id=message.chat.id,
            timezone=user_state.timezone,
        )
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text="[/checkin]"
        )

    @router.message(Command("due"))
    async def due(message: Message, event_update: Update, command: CommandObject) -> None:
        text = (command.args or "").strip()
        async with sessionmaker() as session:
            if text:
                await update_state(session, "due_action", text, "command")
                await update_state(
                    session, "due_set_at", clock.now_utc(), "command"
                )
            else:
                await update_state(session, "due_action", None, "command")
                await update_state(session, "due_set_at", None, "command")
        await _expire_proposal_for(message, proposal_core.DUE_ACTION)
        await _reply_once(
            message,
            event_update.update_id,
            DUE_SET.format(text=text) if text else DUE_CLEARED,
        )

    @router.message(Command("focus"))
    async def focus(message: Message, event_update: Update, command: CommandObject) -> None:
        raw = (command.args or "").strip().lower()
        if raw not in ("on", "off", "вкл", "выкл"):
            await _reply_once(message, event_update.update_id, FOCUS_USAGE)
            return
        # parse_focus is shared with the proposal button, so a command
        # and a button can never disagree about what "on" means.
        enabled = proposal_core.parse_focus(raw)
        async with sessionmaker() as session:
            await update_state(session, "focus_on", enabled, "command")
            await update_state(
                session,
                "focus_since",
                clock.now_utc() if enabled else None,
                "command",
            )
        await _expire_proposal_for(message, proposal_core.FOCUS_ON)
        await _reply_once(
            message, event_update.update_id, FOCUS_ON if enabled else FOCUS_OFF
        )

    # --- 2f: data control (plan section 11) ---

    @router.message(Command("quiet"))
    async def quiet(message: Message, event_update: Update, command: CommandObject) -> None:
        """/quiet <N>m|h|d and /quiet off (plan section 10).

        Setting quiet cancels what is already planned as well as
        blocking what would be: the gate's `quiet_cmd` check stops new
        planning, and cancel_outbound revokes the message that may
        already be sitting in the queue with its jitter running. Either
        alone would leave a hole.
        """
        parsed = parse_quiet(command.args or "")

        if parsed is None:
            await _reply_once(message, event_update.update_id, QUIET_USAGE)
            return

        if parsed == QUIET_OFF:
            async with sessionmaker() as session:
                await update_state(session, "quiet_until", None, "command")
            await _reply_once(message, event_update.update_id, QUIET_OFF_REPLY)
            return

        capped = clamp_quiet(parsed, settings.QUIET_MAX_DAYS)
        until = clock.now_utc() + capped
        async with sessionmaker() as session:
            user_state = await get_state(session)
            await update_state(session, "quiet_until", until, "command")
            await cancel_outbound(session, clock)

        local = until.astimezone(ZoneInfo(user_state.timezone)).strftime("%d.%m %H:%M")
        template = QUIET_CLAMPED if capped < parsed else QUIET_SET
        await _reply_once(
            message,
            event_update.update_id,
            template.format(until=local, days=settings.QUIET_MAX_DAYS),
        )

    @router.message(Command("tz"))
    async def timezone_command(
        message: Message, event_update: Update, command: CommandObject
    ) -> None:
        """/tz <IANA> (plan section 10).

        Validated by actually constructing the ZoneInfo rather than by
        matching a pattern: the tz database is the only authority on
        what is a real zone, and an unknown-but-plausible name is
        exactly the input that would otherwise be accepted and then
        crash every local-time computation afterwards.
        """
        raw = (command.args or "").strip()
        if not raw:
            await _reply_once(message, event_update.update_id, TZ_USAGE)
            return

        try:
            zone = ZoneInfo(raw)
        except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError, ValueError, OSError
            await _reply_once(message, event_update.update_id, TZ_UNKNOWN)
            return

        async with sessionmaker() as session:
            await update_state(session, "timezone", raw, "command")

        now_there = clock.now_utc().astimezone(zone).strftime("%H:%M")
        await _reply_once(
            message,
            event_update.update_id,
            TZ_SET.format(tz=raw, time=now_there),
        )

    @router.message(Command("export"))
    async def export_command(message: Message, event_update: Update) -> None:
        if not await _once(event_update.update_id):
            return
        async with sessionmaker() as session:
            user_state = await get_state(session)
        scene_id = await turn.ensure_scene(sessionmaker, settings, clock)
        await data_ui.run_export(
            sessionmaker,
            message.bot,
            clock,
            chat_id=message.chat.id,
            timezone=user_state.timezone,
        )
        # mark_update_handled, not _reply_once: send_command_reply is
        # text-only end to end and would re-send stored *text* on a
        # replay, which is meaningless for a document. Same shape
        # /checkin uses.
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text="[/export]", scene_id=scene_id
        )

    @router.message(Command("delete"))
    async def delete_command(message: Message, event_update: Update) -> None:
        if not await _once(event_update.update_id):
            return
        scene_id = await turn.ensure_scene(sessionmaker, settings, clock)
        await send_keyboard(
            message.bot, message.chat.id, data_ui.CONFIRM_TEXT, data_ui.confirm_keyboard()
        )
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text="[/delete]", scene_id=scene_id
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
            clock=clock,
            chat_id=message.chat.id,
            update_id=update_id,
            text=text,
            scene_id=await turn.ensure_scene(sessionmaker, settings, clock),
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
        await turn.ensure_scene(sessionmaker, settings, clock)
        await memory_ui.run_remember(
            sessionmaker, message.bot, chat_id=message.chat.id, text=text
        )
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text=memory_ui.REMEMBER_PROMPT.format(text=text)
        )

    @router.message(Command("memories"))
    async def memories(message: Message, event_update: Update) -> None:
        if not await _once(event_update.update_id):
            return
        await memory_ui.run_memories(sessionmaker, message.bot, chat_id=message.chat.id)
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text="[/memories]"
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

    # --- 4b/4c: research (plan section 9) ---
    #
    # Every one of these six checks RESEARCH_ENABLED first and replies
    # research_ui.DISABLED when it is off -- the default, until 4d. The
    # refusal check runs before argument parsing (matching /focus's
    # ordering, not /remember's) because a disabled feature should say
    # so before it says anything about how to use it.
    #
    # /adopt and /reject skip the `_once` replay gate, like /pin and
    # /unpin above: app/core/cards.py's adopt()/reject() are themselves
    # idempotent (ALREADY, no write), so there is no double-mutation for
    # the gate to prevent. /read and /study cannot make that claim --
    # enqueue_read()/enqueue_study() insert a fresh study_job row on
    # every call -- so both keep the manual `_once` + mark_update_handled
    # dance /remember uses, and /notes keeps it too, purely to avoid
    # re-sending the same keyboard message on a replay (send_keyboard
    # bypasses send_command_reply's own per-update dedup, exactly as
    # /remember's keyboard does).

    @router.message(Command("study"))
    async def study_command(
        message: Message, event_update: Update, command: CommandObject
    ) -> None:
        if not settings.RESEARCH_ENABLED:
            await _reply_once(message, event_update.update_id, research_ui.DISABLED)
            return
        parsed = research_ui.parse_study_args(command.args)
        if parsed is None:
            await _reply_once(message, event_update.update_id, research_ui.STUDY_USAGE)
            return
        packet, topic = parsed
        if not await _once(event_update.update_id):
            return
        async with sessionmaker() as session:
            user_state = await get_state(session)
        reply = await research_ui.run_study(
            sessionmaker,
            settings,
            clock,
            timezone=user_state.timezone,
            packet=packet,
            topic=topic,
        )
        # Same shape as /read below: run_study only enqueues, so the
        # reply goes out through _reply_once rather than
        # mark_update_handled.
        await _reply_once(message, event_update.update_id, reply)

    @router.message(Command("read"))
    async def read_command(
        message: Message, event_update: Update, command: CommandObject
    ) -> None:
        if not settings.RESEARCH_ENABLED:
            await _reply_once(message, event_update.update_id, research_ui.DISABLED)
            return
        url = (command.args or "").strip()
        if not url:
            await _reply_once(message, event_update.update_id, research_ui.READ_USAGE)
            return
        if not await _once(event_update.update_id):
            return
        async with sessionmaker() as session:
            user_state = await get_state(session)
        reply = await research_ui.run_read(
            sessionmaker,
            settings,
            clock,
            timezone=user_state.timezone,
            url=url,
        )
        # run_read only enqueues; unlike /notes below, nothing has been
        # sent yet, so the reply goes out through _reply_once (matching
        # /forget's shape: gate with _once, mutate, then _reply_once) --
        # not mark_update_handled, which is for a reply already sent by
        # some other means.
        await _reply_once(message, event_update.update_id, reply)

    @router.message(Command("notes"))
    async def notes_command(message: Message, event_update: Update) -> None:
        if not settings.RESEARCH_ENABLED:
            await _reply_once(message, event_update.update_id, research_ui.DISABLED)
            return
        if not await _once(event_update.update_id):
            return
        await research_ui.run_notes(sessionmaker, message.bot, chat_id=message.chat.id)
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text="[/notes]"
        )

    @router.message(Command("card"))
    async def card_command(
        message: Message, event_update: Update, command: CommandObject
    ) -> None:
        if not settings.RESEARCH_ENABLED:
            await _reply_once(message, event_update.update_id, research_ui.DISABLED)
            return
        card_id = memory_ui.parse_id(command.args)
        if card_id is None:
            await _reply_once(message, event_update.update_id, research_ui.CARD_USAGE)
            return
        reply = await research_ui.run_card(sessionmaker, card_id=card_id)
        await _reply_once(message, event_update.update_id, reply)

    @router.message(Command("adopt"))
    async def adopt_command(
        message: Message, event_update: Update, command: CommandObject
    ) -> None:
        if not settings.RESEARCH_ENABLED:
            await _reply_once(message, event_update.update_id, research_ui.DISABLED)
            return
        card_id = memory_ui.parse_id(command.args)
        if card_id is None:
            await _reply_once(message, event_update.update_id, research_ui.ADOPT_USAGE)
            return
        reply = await research_ui.run_adopt(sessionmaker, clock, card_id=card_id)
        await _reply_once(message, event_update.update_id, reply)

    @router.message(Command("reject"))
    async def reject_command(
        message: Message, event_update: Update, command: CommandObject
    ) -> None:
        if not settings.RESEARCH_ENABLED:
            await _reply_once(message, event_update.update_id, research_ui.DISABLED)
            return
        card_id = memory_ui.parse_id(command.args)
        if card_id is None:
            await _reply_once(message, event_update.update_id, research_ui.REJECT_USAGE)
            return
        reply = await research_ui.run_reject(sessionmaker, clock, card_id=card_id)
        await _reply_once(message, event_update.update_id, reply)

    # --- P2: planner read path + link flow ---

    @router.message(Command("plan"))
    async def plan_command(message: Message, event_update: Update) -> None:
        if not settings.PLANNER_ENABLED:
            await _reply_once(message, event_update.update_id, planner_ui.DISABLED)
            return
        async with sessionmaker() as session:
            user_state = await get_state(session)
            snap = await planner_snapshot.get_snapshot(session)
        text = planner_ui.render_plan_text(
            snap, clock, user_state.timezone, max_age_min=settings.PLANNER_SNAPSHOT_MAX_AGE_MIN
        )
        await _reply_once(message, event_update.update_id, text)

    @router.message(Command("planner"))
    async def planner_command(
        message: Message, event_update: Update, command: CommandObject
    ) -> None:
        if not settings.PLANNER_ENABLED:
            await _reply_once(message, event_update.update_id, planner_ui.DISABLED)
            return

        raw = (command.args or "").strip().lower()
        if raw in ("on", "off"):
            if not await _once(event_update.update_id):
                return
            async with sessionmaker() as session:
                row = await planner_auth.set_enabled(session, raw == "on")
            if row is None:
                await _reply_once(message, event_update.update_id, planner_ui.ON_OFF_NOT_LINKED)
                return
            await _reply_once(
                message,
                event_update.update_id,
                planner_ui.ON_REPLY if raw == "on" else planner_ui.OFF_REPLY,
            )
            return
        if raw:
            await _reply_once(message, event_update.update_id, planner_ui.ON_OFF_USAGE)
            return

        async with sessionmaker() as session:
            user_state = await get_state(session)
            credential = await planner_auth.get_status(session)
            snap = await planner_snapshot.get_snapshot(session)
        text = planner_ui.render_status_text(
            credential,
            snap,
            clock,
            user_state.timezone,
            max_age_min=settings.PLANNER_SNAPSHOT_MAX_AGE_MIN,
        )
        await _reply_once(message, event_update.update_id, text)

    @router.message(Command("planner_link"))
    async def planner_link_command(message: Message, event_update: Update) -> None:
        if not settings.PLANNER_ENABLED:
            await _reply_once(message, event_update.update_id, planner_ui.LINK_DISABLED)
            return
        if not await _once(event_update.update_id):
            return
        async with sessionmaker() as session:
            existing = await planner_auth.get_status(session)
        if existing is not None and existing.status == planner_auth.ACTIVE:
            await _reply_once(message, event_update.update_id, planner_ui.LINK_ALREADY)
            return
        try:
            url = await planner_auth.link_url(settings, clock)
        except Exception as exc:  # noqa: BLE001 - a failed discovery must still reply
            await _reply_once(
                message, event_update.update_id, planner_ui.LINK_FAILED.format(
                    reason=type(exc).__name__
                )
            )
            return
        await _reply_once(message, event_update.update_id, planner_ui.LINK_INTRO.format(url=url))

    # --- P3: /task, /event (deterministic parse, then a confirm card) ---

    async def _handle_write_command(
        message: Message,
        event_update: Update,
        *,
        text: str,
        usage: str,
        kind: str,
        parser,
    ) -> None:
        """Shared shape for /task and /event: usage check, replay gate,
        the daily write cap, parse (regex, then the safety-model
        fallback), a planner_action row, then its confirm card.

        Not a canned reply (like /remember): a card carries a keyboard,
        so it is sent directly once mark_update_handled records the
        text-only trace of what was typed.
        """
        if not settings.PLANNER_ENABLED:
            await _reply_once(message, event_update.update_id, planner_ui.DISABLED)
            return
        if not text:
            await _reply_once(message, event_update.update_id, usage)
            return
        if not await _once(event_update.update_id):
            return

        reply: str | None = None
        action_id: int | None = None
        async with sessionmaker() as session:
            user_state = await get_state(session)
            timezone = user_state.timezone
            count = await planner_actions.count_today(session, clock, timezone)
            if count >= settings.PLANNER_MAX_WRITES_PER_DAY:
                reply = planner_ui.WRITE_CAP_REACHED.format(
                    count=count, cap=settings.PLANNER_MAX_WRITES_PER_DAY
                )
            else:
                try:
                    payload = await parser(session, timezone)
                except planner_parse.ParseError as exc:
                    reply = exc.message
                else:
                    action = await planner_actions.create(session, clock, kind=kind, payload=payload)
                    action_id = action.id

        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text=f"[/{kind}]"
        )
        if action_id is None:
            await message.answer(reply)
            return
        await planner_ui.send_confirm_card(
            sessionmaker, message.bot, chat_id=message.chat.id, action_id=action_id, timezone=timezone
        )

    @router.message(Command("task"))
    async def task_command(message: Message, event_update: Update, command: CommandObject) -> None:
        async def _parse(session, timezone: str) -> dict:
            parsed = await planner_parse.parse_task(
                session, settings, safety_provider or provider, clock,
                text=(command.args or "").strip(), timezone=timezone,
            )
            return {
                "title": parsed.title,
                "due_date": parsed.due_date.isoformat() if parsed.due_date else None,
            }

        await _handle_write_command(
            message, event_update,
            text=(command.args or "").strip(), usage=planner_ui.TASK_USAGE,
            kind=planner_actions.CREATE_TASK, parser=_parse,
        )

    @router.message(Command("event"))
    async def event_command(message: Message, event_update: Update, command: CommandObject) -> None:
        async def _parse(session, timezone: str) -> dict:
            parsed = await planner_parse.parse_event(
                session, settings, safety_provider or provider, clock,
                text=(command.args or "").strip(), timezone=timezone,
            )
            return {
                "title": parsed.title,
                "start": parsed.start.isoformat(),
                "end": parsed.end.isoformat(),
                "all_day": parsed.all_day,
            }

        await _handle_write_command(
            message, event_update,
            text=(command.args or "").strip(), usage=planner_ui.EVENT_USAGE,
            kind=planner_actions.CREATE_EVENT, parser=_parse,
        )

    # --- P3: /done (pick a task from a list; the tap is the confirmation) ---

    @router.message(Command("done"))
    async def done_command(message: Message, event_update: Update) -> None:
        if not settings.PLANNER_ENABLED:
            await _reply_once(message, event_update.update_id, planner_ui.DISABLED)
            return
        async with sessionmaker() as session:
            user_state = await get_state(session)
            snap = await planner_snapshot.get_snapshot(session)
        text, tasks = planner_ui.render_done_list(
            snap, clock, user_state.timezone, max_age_min=settings.PLANNER_SNAPSHOT_MAX_AGE_MIN
        )
        if not tasks:
            await _reply_once(message, event_update.update_id, text)
            return
        if not await _once(event_update.update_id):
            return
        await send_keyboard(message.bot, message.chat.id, text, planner_ui.done_keyboard(tasks))
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text="[/done]"
        )

    @router.message(F.text)
    async def handle_text(message: Message, event_update: Update) -> None:
        await turn.run(
            sessionmaker,
            message.bot,
            settings,
            provider,
            clock=clock,
            chat_id=message.chat.id,
            update_id=event_update.update_id,
            user_text=message.text,
            safety_provider=safety_provider,
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

    @router.callback_query(F.data.startswith("c:"))
    async def checkin_callback(callback: CallbackQuery, event_update: Update) -> None:
        """`c:r:<n>` / `c:d:<result>` / `c:n:skip` -- the check-in flow."""
        await checkin_ui.handle_callback(
            sessionmaker,
            callback.bot,
            settings,
            provider,
            safety_provider,
            clock,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            update_id=event_update.update_id,
            data=callback.data,
        )

    @router.callback_query(F.data.startswith("d:"))
    async def delete_decision(callback: CallbackQuery) -> None:
        """`d:yes:<epoch>` / `d:no` -- the two-step delete confirmation.

        Deliberately not gated on _once: the wipe destroys the `message`
        rows that gate reads, and a replayed press is harmless anyway
        (see app/tg/data.py).
        """
        await data_ui.handle_delete_callback(
            sessionmaker,
            callback.bot,
            settings,
            clock,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            data=callback.data,
        )

    @router.callback_query(F.data.startswith("w:"))
    async def welfare_decision(callback: CallbackQuery, event_update: Update) -> None:
        """`w:resume` / `w:stay` -- the welfare reply's buttons."""
        await welfare_ui.handle_callback(
            sessionmaker,
            callback.bot,
            settings,
            clock,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            update_id=event_update.update_id,
            data=callback.data,
            message_text=callback.message.text,
        )

    @router.callback_query(F.data.startswith("p:"))
    async def proposal_decision(callback: CallbackQuery) -> None:
        """`p:a:<id>` / `p:r:<id>` -- the extractor's confirmation buttons."""
        await proposals_ui.handle_decision_callback(
            sessionmaker,
            callback.bot,
            clock,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            data=callback.data,
        )

    @router.callback_query(F.data.startswith("pa:"))
    async def planner_action_decision(callback: CallbackQuery) -> None:
        """`pa:y:<id>` / `pa:n:<id>` -- the /task and /event confirm card."""
        async with sessionmaker() as session:
            user_state = await get_state(session)
        await planner_ui.handle_confirm_callback(
            sessionmaker,
            callback.bot,
            clock,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            data=callback.data,
            timezone=user_state.timezone,
        )

    @router.callback_query(F.data.startswith("pl:d:"))
    async def planner_done(callback: CallbackQuery) -> None:
        """`pl:d:<task id>` -- a /done list button."""
        async with sessionmaker() as session:
            user_state = await get_state(session)
        await planner_ui.handle_done_callback(
            sessionmaker,
            callback.bot,
            settings,
            clock,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            data=callback.data,
            timezone=user_state.timezone,
        )

    @router.callback_query(F.data.startswith("r:a:"))
    async def research_adopt(callback: CallbackQuery) -> None:
        """`r:a:<id>` -- a /notes card's [Принять] button."""
        await research_ui.handle_decision_callback(
            sessionmaker,
            callback.bot,
            clock,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            data=callback.data,
        )

    @router.callback_query(F.data.startswith("r:r:"))
    async def research_reject(callback: CallbackQuery) -> None:
        """`r:r:<id>` -- a /notes card's [Отклонить] button."""
        await research_ui.handle_decision_callback(
            sessionmaker,
            callback.bot,
            clock,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            data=callback.data,
        )

    @router.callback_query(F.data.startswith("r:p:"))
    async def research_page(callback: CallbackQuery) -> None:
        """`r:p:<page>` -- a /notes paging arrow."""
        await research_ui.handle_page_callback(
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

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

import logging
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, CallbackQuery, Message, ReplyKeyboardRemove, Update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core import turn
from app.core import clock as clock_module
from app.core.clock import Clock, SystemClock
from app.core import checkin as checkin_core
from app.core import commands as commands_core
from app.core import memory as memory_core
from app.core import mood as mood_core
from app.core import obligations as obligations_core
from app.core import safety_events
from app.core.prompt import load_persona, persona_path_for
from app.core import proposal as proposal_core
from app.core.outbound import load_state_summary
from app.core.quiet import OFF as QUIET_OFF
from app.core.quiet import clamp as clamp_quiet
from app.core.quiet import parse as parse_quiet
from app.core.idle.facts import idle_jobs_today, latest_canary_status
from app.core.spend import today_by_category, today_idle_usd, today_usd
from app.core.state import get_state
from app.llm.provider import LLMProvider
from app.ops.backup import latest_backup_status
from app.planner import actions as planner_actions
from app.planner import auth as planner_auth
from app.planner import parse as planner_parse
from app.planner import snapshot as planner_snapshot
from app.tg.send import answer_callback, edit_keyboard, send_keyboard
from app.tg import amendments as amendments_ui
from app.tg import checkin as checkin_ui
from app.tg import claude as claude_ui
from app.tg import data as data_ui
from app.tg import grok as grok_ui
from app.tg import idle as idle_ui
from app.tg import interests as interests_ui
from app.tg import memory as memory_ui
from app.tg import menu
from app.tg import notebook as notebook_ui
from app.tg import obligations as obligations_ui
from app.tg import orders as orders_ui
from app.tg import planner as planner_ui
from app.tg import proposals as proposals_ui
from app.tg import research as research_ui
from app.tg import state_view
from app.tg import vault as vault_ui
from app.tg import review as review_ui
from app.tg import welfare as welfare_ui
from app.web import auth as web_auth
from app.web import oauth_store
from app.web.hub import WebHub
from app.vault import consent as vault_consent
from app.vault import status as vault_status

logger = logging.getLogger(__name__)

NON_TEXT_REPLY = "Пока только текст."

START_TEXT = (
    "Я — Anchor. Здесь по-русски, коротко и по делу.\n"
    "Выйти из роли можно командой /out или словом «пурпурный»."
)

# 6e: /privacy (plan section 9.5). Fixed text, 8-10 lines, reflecting
# what the code actually does -- same "a privacy statement the user
# would act on has to describe what the code actually does" reasoning
# app/tg/data.py's DELETED_TEXT docstring already gives. The 14/8
# backup figures and the 30-day clip-text figure are app/config.py's
# BACKUP_KEEP_DAILY/BACKUP_KEEP_WEEKLY defaults and
# app/research/sweeps.py's RETENTION_DAYS, spelled out rather than
# read live -- the plan calls this "fixed text", and a deploy that
# changes those settings is also the deploy that should update this
# string, the same way DELETED_TEXT is not computed from settings
# either.
PRIVACY_TEXT = (
    "Что хранится и где: сообщения, память, заметки, чек-ины, журнал, "
    "договорённости и настройки — в базе Postgres на Railway.\n"
    "Запросы к модели идут через OpenRouter с запретом на сбор данных "
    "(data collection: deny); сам провайдер модели хранит данные по своим "
    "правилам.\n"
    "Если подключён планер: агенда (включая общие события партнёра) и, при "
    "PLANNER_HEALTH, метрики сна/пульса попадают в запрос к модели; токены "
    "планера хранятся в базе и не выгружаются в /export. Если открыт доступ "
    "(/grok, /claude), прочитанное уходит в xAI или Anthropic; закрыть можно "
    "дальнейшее чтение, но не уже прочитанное.\n"
    "Переписка в Telegram не имеет сквозного шифрования — сообщения "
    "проходят через серверы Telegram и этого бота.\n"
    "Резервные копии базы зашифрованы (age) и хранятся: 14 ежедневных + 8 "
    "еженедельных копий, остальные удаляются.\n"
    "Текст страниц, найденных при поиске, хранится 30 дней, потом "
    "стирается — карточки и ссылки остаются.\n"
    "Заметки из Obsidian Anchor читает только с твоей меткой: личные — только для "
    "разговора с тобой, никогда для поиска или исследований; знания — как справка, "
    "а при /claude library on их может искать Claude (найденное уходит в Anthropic). "
    "В режиме sync правка или удаление файла факта в папке Anchor меняет его память.\n"
    "Логи сервера содержат только коды, счётчики и стоимость — без текста.\n"
    "/export — выгрузить все свои данные одним файлом.\n"
    "/delete — удалить все данные и все резервные копии, безвозвратно."
)

BOT_COMMANDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="menu", description="Меню с кнопками"),
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
    BotCommand(command="grok", description="Открыть данные для Grok"),
    BotCommand(command="claude", description="Подключение и окно для Claude"),
    BotCommand(command="revoke", description="Закрыть доступ для Grok и Claude"),
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
    # 5b (phase-5 plan section 6).
    BotCommand(command="mind", description="Заметки Anchor"),
    # 5c (phase-5 plan section 7).
    BotCommand(command="order", description="Новая договорённость"),
    BotCommand(command="orders", description="Список договорённостей"),
    # Phase 5 (spec 2026-09-25): the debt queue. Not /done, which is the
    # planner's.
    BotCommand(command="paid", description="Долги: список и закрытие"),
    # 5d (phase-5 plan sections 8 and 9).
    BotCommand(command="review", description="Итоги недели"),
    BotCommand(command="amendments", description="Поправки к стилю"),
    # 6a (Phase 6 plan section 7).
    BotCommand(command="digest", description="Фоновая работа"),
    # 6d (Phase 6 plan section 7).
    BotCommand(command="interests", description="Темы для фонового поиска"),
    # 6e (Phase 6 plan section 9.5).
    BotCommand(command="privacy", description="Приватность и хранение данных"),
    # P2 (design review section 2.3): the read path + link flow.
    BotCommand(command="plan", description="План на сегодня"),
    BotCommand(command="planner", description="Статус планера, on/off"),
    BotCommand(command="planner_link", description="Подключить планер"),
    # P3: explicit writes, behind a confirm card (/task, /event) or a
    # pick-from-list button (/done).
    BotCommand(command="task", description="Добавить задачу в планер"),
    BotCommand(command="event", description="Добавить событие в планер"),
    BotCommand(command="done", description="Отметить задачу сделанной"),
    # 8a (phase-8 plan section 8): the Obsidian vault's status.
    BotCommand(command="vault", description="Хранилище Obsidian"),
]

# Web-chat plan track 2 (design section 4): the kill switch for a stolen
# or merely forgotten-open web session. Kept out of BOT_COMMANDS proper
# and added by register_commands() only when WEB_UI_ENABLED -- listing
# it, and registering its handler above, unconditionally used to mean
# the command showed in Telegram's menu and replied "closed" even on a
# deploy where the web UI was never turned on (a low-severity finding:
# "Telegram behavior changes even when WEB_UI_ENABLED=false").
WEBLOGOUT_COMMAND = BotCommand(command="weblogout", description="Закрыть все веб-сессии")

QUIET_SET = "Тихо до {until}."
QUIET_OFF_REPLY = "Снова на связи."
QUIET_USAGE = "Сколько? /quiet 2h, /quiet 30m, /quiet 1d или /quiet off."
QUIET_CLAMPED = "Тихо до {until} — дольше {days} дн. подряд не ставлю."

TZ_SET = "Часовой пояс: {tz}. Сейчас у тебя {time}."
TZ_UNKNOWN = "Не знаю такой пояс. Пример: Europe/Paris."
TZ_USAGE = "Какой пояс? Пример: /tz Europe/Paris."

# /state's outbound block (plan section 10). Defined once in
# state_view.py (which the rich /state view also needs them for) and
# aliased here rather than duplicated, so this module's _format_outbound
# and state_view.render() can never disagree on either one.
OUTBOUND_KIND_LABELS = state_view.OUTBOUND_KIND_LABELS
NOTHING = state_view.NOTHING

DUE_CLEARED = "Главное действие снято."
DUE_SET = "Главное действие: «{text}»."
FOCUS_USAGE = "Как именно? /focus on или /focus off."
FOCUS_ON = "Фокус включён."
FOCUS_OFF = "Фокус выключен."

# Web-chat plan track 1: the second, independent layer of defense
# against /export and /delete from the web (design section 2; the
# adversarial review's finding 2 calls this out specifically -- ingress
# blocking the text/callback is layer one, app/web/ingress.py, and must
# not be the *only* layer). A guard checking `message.bot.is_web_sink` /
# `callback.bot.is_web_sink` at the top of each handler below means
# these stay safe even if a future change to ingress.py's tokenization
# ever drifts from what this router actually matches.
WEB_ONLY_REPLY = "Эта команда доступна только в Telegram."

# Web-chat plan track 2's /weblogout reply (design section 4).
WEBLOGOUT_REPLY = "Все веб-сессии закрыты."

# /menu's own "Убрать кнопку меню" action (app/tg/menu.py's ACTIONS table):
# the one action with no existing slash-command handler behind it, since
# no command ever needed to remove the persistent reply keyboard before.
HIDE_KB_REPLY = "Кнопка меню убрана. Вернуть — /start."


async def register_commands(bot, *, web_ui_enabled: bool = False) -> None:
    """set_my_commands on startup (plan section 12).

    `web_ui_enabled` defaults to False so every call site and test that
    predates the web UI keeps behaving exactly as before; app/main.py
    passes settings.WEB_UI_ENABLED explicitly. Only then is /weblogout
    added to the menu -- see BOT_COMMANDS' comment on WEBLOGOUT_COMMAND.
    """
    commands = list(BOT_COMMANDS)
    if web_ui_enabled:
        commands.append(WEBLOGOUT_COMMAND)
    await bot.set_my_commands(commands)


def _format_outbound(summary, tz: ZoneInfo, now_utc) -> list[str]:
    """/state's three proactive lines (plan section 10).

    Kept beside _format_state rather than inside it because it is the
    one block whose absence is meaningful: before 3b there was nothing
    to say, and a summary of None still renders, as three lines of
    "nothing yet", rather than silently disappearing.

    The per-value work (what "quiet until" and "the next planned
    message" actually say) is state_view.quiet_until_parts/
    next_outbound_parts -- shared with the rich /state view below so the
    two can never show a different answer to the same question.
    """
    if summary is None:
        return []

    quiet = state_view.quiet_until_parts(summary, tz, now_utc)[0]
    upcoming = state_view.next_outbound_parts(summary, tz)[0]

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
    mood=None,
    idle=None,
    canary=None,
    backup=None,
    debts=None,
    persona_version=None,
    vault_line=None,
) -> str:
    """Plan section 11's /state: Phase 1's fields plus 2c/2d's.

    Phase 5 (spec 2026-09-25): `debts` is `(open, overdue)`. The debt
    line appears only when a debt is open, and the attention line only
    while Anchor is in a short stretch, so /state is unchanged for a
    user who has neither.

    Spend is broken down by ledger category so a day where the
    background jobs cost more than the conversation is visible at a
    glance rather than hidden inside one total.

    5a: `mood`, computed by the caller through the same
    load_mood_facts()+mood() pair the persona prompt uses, so /state
    can never show a mood the prompt itself would not have shown this
    turn. Plain -- no gloss here; the gloss is an instruction to the
    model, not information for the user.

    Every value below is computed by a state_view helper shared with
    the rich /state view (app/tg/state_view.py's render()) -- this
    function only lays the same answers out as lines of text instead of
    table rows, so the two can never disagree about what a line says.
    """
    tz = ZoneInfo(user_state.timezone)
    now = clock_module.now_local(clock, user_state.timezone)
    today = now.date()
    now_local = now.strftime("%Y-%m-%d %H:%M")

    last_checkin = state_view.last_checkin_parts(user_state, tz, today)[0]
    due = state_view.due_text(user_state, tz, today)

    # H2. Its own line rather than part of _format_outbound's block:
    # that helper returns nothing at all when there is no summary, and
    # this is not a proactive-message line -- it answers "is the welfare
    # check actually running", which matters most on a quiet week when
    # the outbound block has nothing to say.
    welfare = state_view.welfare_value(welfare_counts)
    welfare_line = (
        f"Проверка благополучия ({safety_events.WINDOW_DAYS} дн.): {welfare}\n"
        if welfare is not None
        else ""
    )

    # 4d fixes. Shown only once there is something to show, unlike the
    # welfare line: the welfare check runs on ordinary turns and a line
    # of zeroes there means "it has stopped", while research only runs
    # when asked, so a permanent "0 · 0" would be noise for anyone who
    # does not use /study or /read.
    # The number that matters is the failures. A distiller returning
    # unparseable JSON makes `done` jobs with no cards, which reads as a
    # quiet week of unhelpful pages until this line says otherwise.
    research = state_view.research_value(research_counts)
    research_line = (
        f"Исследования ({safety_events.WINDOW_DAYS} дн.): {research}\n"
        if research is not None
        else ""
    )

    breakdown = state_view.spend_breakdown_text(by_category)

    # 6a: "Фон: $x / $cap, задач N" (approved plan §5). `idle` is
    # (spend_today, usd_cap, jobs_today) or None -- optional the same
    # way `outbound`/`welfare_counts`/`research_counts` are, so tests
    # predating 6a that call _format_state directly keep working.
    idle_line = f"Фон: {state_view.idle_value(idle)}\n" if idle is not None else ""

    # 6c: "Канарейка: <дата> ок/⚠️" alongside the idle line -- `canary`
    # is `(local_date, passed)` from app/core/idle/facts.latest_canary_status,
    # or None if no canary has ever completed (nothing shown, same
    # "optional the same way idle/outbound/... is" posture idle_line
    # follows above).
    canary_line = f"Канарейка: {state_view.canary_value(canary)}\n" if canary is not None else ""

    # 6e: "Бэкап: <дата время> ок" / "Бэкап: ⚠️ ошибка <дата>" (plan
    # section 9.1) -- `backup` is `(local_date, status)` from
    # app/ops/backup.latest_backup_status, or None if no backup has ever
    # run.
    backup_line = (
        f"Бэкап: {state_view.backup_parts(backup, tz)[0]}\n" if backup is not None else ""
    )

    debt = state_view.debt_value(debts)
    debt_line = f"Долг: {debt}\n" if debt is not None else ""
    attention = state_view.attention_parts(user_state, clock, tz)
    attention_line = f"Внимание: {attention[0]}\n" if attention is not None else ""

    return (
        "Персона: {persona}\n"
        "Интенсивность: {intensity}/5 · Фокус: {focus}\n"
        "Серия: {streak} дн. · Последний чек-ин: {last_checkin}\n"
        "Настроение: {mood}\n"
        "Главное действие: {due}\n"
        "{debt}"
        "{attention}"
        "{outbound}"
        "{welfare}"
        "{research}"
        "{idle}"
        "{canary}"
        "{backup}"
        "{vault}"
        "Помню: {memories} записей\n"
        "Локальное время: {time} ({tz})\n"
        "Потрачено сегодня: {spend:.2f} / {cap:.2f} USD{breakdown}\n"
        "Модель: {model}"
    ).format(
        persona=("вкл" if user_state.persona_active else "выкл")
        + (f" · v{persona_version}" if persona_version else ""),
        intensity=user_state.intensity,
        focus="вкл" if user_state.focus_on else "выкл",
        streak=user_state.streak,
        last_checkin=last_checkin,
        mood=mood,
        due=due,
        debt=debt_line,
        attention=attention_line,
        outbound="".join(
            line + "\n" for line in _format_outbound(outbound, tz, clock.now_utc())
        ),
        welfare=welfare_line,
        research=research_line,
        idle=idle_line,
        canary=canary_line,
        backup=backup_line,
        # 8a (phase-8 plan section 8). Always shown, «выключено» included:
        # whether the vault is connected is a fact about the bot's state
        # worth one line even when the answer is no.
        vault=(vault_line + "\n") if vault_line else "",
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
    hub: WebHub | None = None,
    code_store: web_auth.CodeStore | None = None,
    claude_pending: oauth_store.PendingStore | None = None,
) -> Router:
    """Build a fresh Router with 1b's commands and 1c's persona turn.

    `hub` (web-chat plan track 2) defaults to None so every test
    predating the web UI keeps its shorter call; app/main.py passes the
    process's one WebHub only when WEB_UI_ENABLED. It, and `code_store`
    beside it, back exactly one handler, `/weblogout` below, which is
    itself only registered -- and only listed in BOT_COMMANDS -- when
    `hub is not None`: with the web UI disabled there is no web_session
    table row and no hub to matter, so `/weblogout` behaves exactly as
    it did before the web chat existed, falling through to an ordinary
    persona turn like any other unrecognized-as-a-command text (a
    low-severity finding: it used to always register, always show in
    the Telegram command menu, and always reply "closed", even with the
    web UI off). Every other handler in this router reaches the web
    chat only indirectly, through `message.bot.is_web_sink`/`callback.
    bot.is_web_sink`, which needs no hub at all.

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
        await message.answer(START_TEXT, reply_markup=menu.reply_keyboard())

    async def _send_menu(message: Message) -> None:
        """Shared by `/menu` and the `☰ Меню` reply-keyboard button below."""
        web = getattr(message.bot, "is_web_sink", False)
        await send_keyboard(message.bot, message.chat.id, *menu.render(menu.MAIN_SECTION, settings, web=web))

    @router.message(Command("menu"))
    async def menu_command(message: Message, event_update: Update) -> None:
        if not await _once(event_update.update_id):
            return
        await _send_menu(message)
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text="[/menu]"
        )

    @router.message(F.text == menu.MENU_BUTTON_TEXT)
    async def menu_button(message: Message, event_update: Update) -> None:
        """The persistent reply-keyboard button. Registered ahead of the
        plain `@router.message(F.text)` persona-turn handler below, or
        aiogram's first-match-wins routing would hand this text straight
        to turn.run() like any other chat line -- exactly the bug this
        handler exists to prevent (module docstring, point 2).

        Also clears a pending check-in `awaiting` step, the same as the
        outer command middleware does for every slash command: pressing
        the menu button is a command, not a check-in note.
        """
        if not await _once(event_update.update_id):
            return
        async with sessionmaker() as session:
            await checkin_core.clear_awaiting(session)
        await _send_menu(message)
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text="[/menu]"
        )

    async def _state_inputs() -> dict:
        """Everything `_format_state`/`state_view.render` take, gathered
        once and shared by /state's send path and the `st:r` refresh
        callback below -- so a refresh can never show a different set of
        facts than a fresh /state would.
        """
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
            # 5a: the same pair the persona prompt uses (app/core/
            # persona_context.py's gather()), so /state can never claim
            # a mood the prompt itself would not have shown this turn.
            # `exclude_update_id=None`: /state is not itself a chat
            # turn, so there is no "this turn's own message" to exclude.
            mood_facts = await mood_core.load_mood_facts(
                session, user_state, clock, exclude_update_id=None
            )
            current_mood = mood_core.mood(user_state, mood_facts, clock.now_utc())
            # Phase 5: the same debt queue the prompt's "## Долг" reads.
            open_debts = await obligations_core.open_list(session)
            today_local = clock_module.local_date(clock, user_state.timezone)
            debt_counts = (
                len(open_debts),
                sum(
                    1
                    for row in open_debts
                    if row.due_local_date is not None and row.due_local_date < today_local
                ),
            )
            # 6a.
            idle_spend = await today_idle_usd(session, clock, user_state.timezone)
            idle_jobs = await idle_jobs_today(session, clock, user_state.timezone)
            # 6c.
            canary_status = await latest_canary_status(session)
            # 6e.
            backup_status = await latest_backup_status(session)
            vault_health = await vault_status.probe(session, settings, clock)
            vault_purge_pending = await vault_status.purge_pending(session)
        return dict(
            user_state=user_state,
            spend=spend,
            settings=settings,
            clock=clock,
            by_category=by_category,
            idle=(idle_spend, settings.IDLE_USD_CAP, idle_jobs),
            canary=canary_status,
            backup=backup_status,
            memories=memories,
            outbound=outbound,
            welfare_counts=welfare_counts,
            research_counts=research_counts,
            mood=current_mood,
            debts=debt_counts,
            # The short hash of the persona file actually served
            # (PERSONA_FILE), the same sha persona_version rows use.
            persona_version=load_persona(persona_path_for(settings))[1][:8],
            vault_line=vault_ui.format_state_line(
                vault_health, clock, user_state.timezone, purge_pending=vault_purge_pending
            ),
        )

    @router.message(Command("state"))
    async def state(message: Message) -> None:
        kw = await _state_inputs()
        # The web sink only ever understands SendMessage/EditMessageText
        # text (app/web/sink.py) -- it must never be handed a rich
        # message, so this is the one path left exactly as it was before
        # 10.1's sendRichMessage existed.
        if getattr(message.bot, "is_web_sink", False):
            await message.answer(_format_state(**kw))
            return
        try:
            await message.bot.send_rich_message(
                chat_id=message.chat.id,
                rich_message=state_view.render(**kw),
                reply_markup=state_view.refresh_keyboard(),
            )
        except TelegramBadRequest as exc:
            # A rich-message rejection must never leave /state silent --
            # fall back to the plain text every version before 10.1 sent.
            logger.warning(
                "state rich message rejected: %s",
                type(exc).__name__,
                extra={"event": "state_rich_fallback"},
            )
            await message.answer(_format_state(**kw))

    @router.callback_query(F.data == state_view.REFRESH_CALLBACK)
    async def state_refresh(callback: CallbackQuery) -> None:
        """`st:r` -- /state's own [🔄 Обновить], refreshing in place.

        Always answers exactly once, whatever branch it takes below (the
        same "an unanswered button spins until Telegram times it out"
        convention every other callback here follows).
        """
        if not isinstance(callback.message, Message) or getattr(
            callback.bot, "is_web_sink", False
        ):
            await answer_callback(callback.bot, callback.id, memory_ui.STALE)
            return

        kw = await _state_inputs()
        try:
            await callback.bot.edit_message_text(
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id,
                rich_message=state_view.render(**kw),
                reply_markup=state_view.refresh_keyboard(),
            )
        except TelegramBadRequest as exc:
            # Same substring app/tg/send.py's _NOT_MODIFIED matches --
            # a replayed refresh (or one that lands on already-current
            # data) is a no-op, not a failure.
            if "message is not modified" in str(exc).lower():
                await answer_callback(callback.bot, callback.id, "Обновлено")
                return
            # Same fallback the initial send uses: an edit that Telegram
            # refuses for any other reason must not leave the button dead
            # -- send a fresh plain message rather than nothing at all.
            logger.warning(
                "state rich message refresh rejected: %s",
                type(exc).__name__,
                extra={"event": "state_rich_fallback"},
            )
            await callback.bot.send_message(callback.message.chat.id, _format_state(**kw))
        await answer_callback(callback.bot, callback.id, "Обновлено")

    @router.message(Command("vault"))
    async def vault(message: Message, command: CommandObject) -> None:
        """Status (phase-8 plan section 8), and 8e's `/vault notes on|off`.

        Off makes no request. The manifest is read only while notes
        consent is on, and only to count notes by class.
        """
        args = (command.args or "").split()
        if args:
            if args == ["notes", "on"]:
                async with sessionmaker() as session:
                    await vault_consent.set_notes_consent(session, True)
                await message.answer(vault_ui.NOTES_ON_REPLY)
            elif args == ["notes", "off"]:
                async with sessionmaker() as session:
                    await vault_consent.set_notes_consent(session, False)
                await message.answer(vault_ui.NOTES_OFF_REPLY)
            else:
                await message.answer(vault_ui.VAULT_USAGE)
            return
        async with sessionmaker() as session:
            user_state = await get_state(session)
            health = await vault_status.probe(session, settings, clock)
            facts = await vault_status.count_fact_files(session)
            # 8c: the problem list is mirror/sync's own (quarantines and
            # holds only ever come from ingest, which status never
            # runs) -- status mode shows the first line only.
            problems = None
            if settings.VAULT_MODE in ("mirror", "sync"):
                problems = await vault_status.vault_problems(session)
        notes_line = None
        if health.state != vault_status.OFF:
            overview = None
            if user_state.notes_consent:
                overview = await vault_status.notes_overview(settings, health)
            notes_line = vault_ui.format_notes_line(user_state.notes_consent, overview)
        await message.answer(
            vault_ui.format_vault(
                health,
                settings,
                clock,
                user_state.timezone,
                facts=facts,
                notes_line=notes_line,
                problems=problems,
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
        what the user just typed. The expiry itself is
        commands_core.expire_proposal_for (W2: shared with the web
        panels); this wrapper adds what only Telegram has, the message
        and bot to retire the stale buttons on.
        """
        async with sessionmaker() as session:
            expired = await commands_core.expire_proposal_for(session, clock, field)
        if expired is None:
            return
        await proposals_ui.retire_buttons(
            sessionmaker, message.bot, chat_id=message.chat.id, proposal_id=expired.id
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
            await commands_core.set_due(session, clock, text, "command")
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
            await commands_core.set_focus(session, clock, enabled, "command")
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
                await commands_core.set_quiet(session, clock, None, "command")
            await _reply_once(message, event_update.update_id, QUIET_OFF_REPLY)
            return

        capped = clamp_quiet(parsed, settings.QUIET_MAX_DAYS)
        until = clock.now_utc() + capped
        async with sessionmaker() as session:
            user_state = await get_state(session)
            await commands_core.set_quiet(session, clock, until, "command")

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

        async with sessionmaker() as session:
            try:
                await commands_core.set_timezone(session, raw, "command")
            except commands_core.InvalidTimezone:
                await _reply_once(message, event_update.update_id, TZ_UNKNOWN)
                return

        now_there = clock.now_utc().astimezone(ZoneInfo(raw)).strftime("%H:%M")
        await _reply_once(
            message,
            event_update.update_id,
            TZ_SET.format(tz=raw, time=now_there),
        )

    @router.message(Command("export"))
    async def export_command(message: Message, event_update: Update) -> None:
        if not await _once(event_update.update_id):
            return
        if getattr(message.bot, "is_web_sink", False):
            await _reply_once(message, event_update.update_id, WEB_ONLY_REPLY)
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
        if getattr(message.bot, "is_web_sink", False):
            await _reply_once(message, event_update.update_id, WEB_ONLY_REPLY)
            return
        scene_id = await turn.ensure_scene(sessionmaker, settings, clock)
        await send_keyboard(
            message.bot, message.chat.id, data_ui.confirm_text(settings), data_ui.confirm_keyboard()
        )
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text="[/delete]", scene_id=scene_id
        )

    @router.message(Command("grok"))
    async def grok_command(message: Message, event_update: Update) -> None:
        """Opt-in read access for grok.com (docs/grok-access.md).

        Sends the scope picker only; nothing is opened until the
        [Разрешить] callback.
        """
        reason = grok_ui.available(settings)
        if reason is not None:
            await _reply_once(message, event_update.update_id, reason)
            return
        if not await _once(event_update.update_id):
            return
        if getattr(message.bot, "is_web_sink", False):
            await _reply_once(message, event_update.update_id, WEB_ONLY_REPLY)
            return
        scene_id = await turn.ensure_scene(sessionmaker, settings, clock)
        await send_keyboard(
            message.bot,
            message.chat.id,
            await grok_ui.opening_text(sessionmaker, clock, 0, 0),
            grok_ui.grant_keyboard(0, 0, 0, int(clock.now_utc().timestamp())),
        )
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text="[/grok]", scene_id=scene_id
        )

    @router.message(Command("claude"))
    async def claude_command(
        message: Message, event_update: Update, command: CommandObject
    ) -> None:
        """Claude access (docs/claude-connector.md): status and the window
        picker, `connect <code>`, `disconnect`.

        Telegram only: approval happens here and nowhere else, so a web
        chat must never be able to type `/claude connect`.
        """
        reason = claude_ui.available(settings)
        if reason is not None:
            await _reply_once(message, event_update.update_id, reason)
            return
        if not await _once(event_update.update_id):
            return
        if getattr(message.bot, "is_web_sink", False):
            await _reply_once(message, event_update.update_id, WEB_ONLY_REPLY)
            return
        scene_id = await turn.ensure_scene(sessionmaker, settings, clock)
        text, keyboard = await claude_ui.command(
            sessionmaker, settings, clock, claude_pending, command.args
        )
        await send_keyboard(message.bot, message.chat.id, text, keyboard)
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text="[/claude]", scene_id=scene_id
        )

    @router.message(Command("revoke"))
    async def revoke_command(message: Message, event_update: Update) -> None:
        """Closes every open grant. Works even with the feature switched
        off, so turning the flag off never strands a live grant."""
        if not await _once(event_update.update_id):
            return
        text = await grok_ui.revoke(sessionmaker, clock)
        await _reply_once(message, event_update.update_id, text)

    @router.message(Command("privacy"))
    async def privacy(message: Message, event_update: Update) -> None:
        # 6e (plan section 9.5). Fixed text, no arguments, same
        # store-and-send-idempotently shape as /focus's usage line --
        # see _reply_once's own docstring. No is_web_sink guard: unlike
        # /export and /delete this sends neither a document nor a data
        # mutation, only a canned informational reply, so there is
        # nothing here a web-origin update could do that a Telegram one
        # could not.
        await _reply_once(message, event_update.update_id, PRIVACY_TEXT)

    if hub is not None:

        @router.message(Command("weblogout"))
        async def weblogout(message: Message, event_update: Update) -> None:
            """The kill switch (design section 4): end every web_session
            row, close every live SSE stream/callback allowlist, and
            invalidate every pending login code.

            Telegram-only in practice already -- a stolen web session
            cannot reach this handler at all, since it can only ever
            produce a synthetic Update fed to WebSinkSession's Bot, not
            a real Telegram message -- so this needs no is_web_sink
            guard of its own the way export_command/delete_command do.
            Only registered at all when `hub is not None` (the WEB_UI_
            ENABLED signal): see build_router's docstring for why the
            web-UI-disabled case is no longer "register it anyway and
            reply as if something happened."
            """
            if not await _once(event_update.update_id):
                return
            async with sessionmaker() as session:
                await web_auth.revoke_all(session, hub, code_store)
            logger.info(
                "web sessions revoked", extra={"event": "web_logout", "route": "weblogout"}
            )
            await _reply_once(message, event_update.update_id, WEBLOGOUT_REPLY)

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

    # --- 5b: the notebook (plan section 6) ---

    @router.message(Command("mind"))
    async def mind(message: Message, event_update: Update, command: CommandObject) -> None:
        args = (command.args or "").strip()
        parts = args.split(maxsplit=1)
        if parts and parts[0] == "add":
            text = parts[1].strip() if len(parts) > 1 else ""
            if not text:
                await _reply_once(message, event_update.update_id, notebook_ui.MIND_ADD_USAGE)
                return
            if not await _once(event_update.update_id):
                return
            reply = await notebook_ui.run_mind_add(sessionmaker, settings, clock, text=text)
            await _reply_once(message, event_update.update_id, reply)
            return

        if not await _once(event_update.update_id):
            return
        await notebook_ui.run_mind(sessionmaker, message.bot, chat_id=message.chat.id)
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text="[/mind]"
        )

    # --- 5c: standing orders (plan section 7) ---

    @router.message(Command("order"))
    async def order_command(
        message: Message, event_update: Update, command: CommandObject
    ) -> None:
        if not await _once(event_update.update_id):
            return
        reply = await orders_ui.run_order_command(
            sessionmaker, settings, clock, text=command.args or ""
        )
        await _reply_once(message, event_update.update_id, reply)

    @router.message(Command("orders"))
    async def orders_command(message: Message, event_update: Update) -> None:
        if not await _once(event_update.update_id):
            return
        await orders_ui.run_orders_list(sessionmaker, message.bot, chat_id=message.chat.id)
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text="[/orders]"
        )

    @router.message(Command("paid"))
    async def paid_command(
        message: Message, event_update: Update, command: CommandObject
    ) -> None:
        """Phase 5: `/paid` lists the open debts; `/paid N` closes the Nth."""
        if not await _once(event_update.update_id):
            return
        arg = (command.args or "").strip()
        if arg:
            reply = await obligations_ui.run_paid_number(sessionmaker, clock, arg=arg)
            await _reply_once(message, event_update.update_id, reply)
            return
        await obligations_ui.run_paid_list(sessionmaker, message.bot, chat_id=message.chat.id)
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text="[/paid]"
        )

    # --- 5d: weekly review and persona amendments (plan sections 8, 9) ---

    @router.message(Command("review"))
    async def review_command(message: Message, event_update: Update) -> None:
        if not await _once(event_update.update_id):
            return
        await review_ui.run_review_command(
            sessionmaker,
            settings,
            provider,
            safety_provider,
            message.bot,
            clock,
            chat_id=message.chat.id,
        )
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text="[/review]"
        )

    @router.message(Command("amendments"))
    async def amendments_command(message: Message, event_update: Update) -> None:
        if not await _once(event_update.update_id):
            return
        await amendments_ui.run_amendments_list(
            sessionmaker,
            message.bot,
            chat_id=message.chat.id,
            persona_path=persona_path_for(settings),
        )
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text="[/amendments]"
        )

    # --- 6a: /digest (plan section 7) ---

    @router.message(Command("digest"))
    async def digest_command(
        message: Message, event_update: Update, command: CommandObject
    ) -> None:
        if not await _once(event_update.update_id):
            return
        window = idle_ui.parse_digest_args(command.args)
        if window is None:
            await _reply_once(message, event_update.update_id, idle_ui.DIGEST_USAGE)
            return
        await idle_ui.run_digest(
            sessionmaker, message.bot, settings, clock, chat_id=message.chat.id, window=window
        )
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text="[/digest]"
        )

    # --- 6d: /interests (plan section 7) ---

    @router.message(Command("interests"))
    async def interests_command(
        message: Message, event_update: Update, command: CommandObject
    ) -> None:
        args = (command.args or "").strip()
        parts = args.split(maxsplit=1)
        if parts and parts[0].lower() == "add":
            parsed = interests_ui.parse_add_args(parts[1] if len(parts) > 1 else "")
            if parsed is None:
                await _reply_once(message, event_update.update_id, interests_ui.ADD_USAGE)
                return
            packet, topic = parsed
            if not await _once(event_update.update_id):
                return
            reply = await interests_ui.run_add(sessionmaker, settings, packet=packet, topic=topic)
            await _reply_once(message, event_update.update_id, reply)
            return

        if not await _once(event_update.update_id):
            return
        await interests_ui.run_list(sessionmaker, message.bot, chat_id=message.chat.id)
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text="[/interests]"
        )

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
        """Telegram-only, same shape and reasoning as export_command/
        delete_command: this sends a live OAuth authorize URL that
        completes a credential link, so a stolen web session must not
        be able to trigger or read it -- app/web/ingress.py's
        BLOCKED_COMMANDS refuses to ever enqueue a `/planner_link`
        request from the web sink in the first place; this is the same
        belt-and-braces second layer those two handlers already use.
        """
        if getattr(message.bot, "is_web_sink", False):
            await _reply_once(message, event_update.update_id, WEB_ONLY_REPLY)
            return
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
        # Not _reply_once: that stores the sent text as a `message` row,
        # and app/web/tail.py's _mirror_query / GET /api/history show
        # every sent assistant row regardless of kind, with no filter for
        # a live OAuth authorize URL. Same shape as export_command/
        # delete_command above -- send the real reply straight to
        # Telegram and store only a placeholder, via mark_update_handled.
        await message.answer(planner_ui.LINK_INTRO.format(url=url))
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text="[/planner_link]"
        )

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

    # --- /menu: dispatching its leaf actions to the handlers above ------
    #
    # Built here, after every command handler above already exists, so
    # each entry just names the nested function it calls -- aiogram's
    # decorators return the function itself, so `checkin_command` etc.
    # are plain, directly callable coroutines. A handful of actions need
    # a `CommandObject` the way a real command line would produce one
    # (read each handler above to see what it does with `command.args`);
    # the rest already take exactly (message, event_update) and are
    # listed as-is. Kept explicit and one-to-one on purpose -- no
    # signature introspection, so any reader can trace an action to the
    # exact call it makes.

    async def _menu_state(message: Message, event_update: Update) -> None:
        del event_update  # state() takes no update_id -- nothing to gate here.
        await state(message)

    async def _menu_quiet(message: Message, event_update: Update, arg: str) -> None:
        await quiet(message, event_update, CommandObject(prefix="/", command="quiet", args=arg))

    async def _menu_quiet_30m(message: Message, event_update: Update) -> None:
        await _menu_quiet(message, event_update, "30m")

    async def _menu_quiet_2h(message: Message, event_update: Update) -> None:
        await _menu_quiet(message, event_update, "2h")

    async def _menu_quiet_8h(message: Message, event_update: Update) -> None:
        await _menu_quiet(message, event_update, "8h")

    async def _menu_quiet_1d(message: Message, event_update: Update) -> None:
        await _menu_quiet(message, event_update, "1d")

    async def _menu_quiet_off(message: Message, event_update: Update) -> None:
        await _menu_quiet(message, event_update, "off")

    async def _menu_focus_on(message: Message, event_update: Update) -> None:
        await focus(message, event_update, CommandObject(prefix="/", command="focus", args="on"))

    async def _menu_focus_off(message: Message, event_update: Update) -> None:
        await focus(message, event_update, CommandObject(prefix="/", command="focus", args="off"))

    async def _menu_vault(message: Message, event_update: Update) -> None:
        del event_update  # vault() takes no event_update -- see its own signature above.
        await vault(message, CommandObject(prefix="/", command="vault", args=None))

    async def _menu_claude(message: Message, event_update: Update) -> None:
        await claude_command(
            message, event_update, CommandObject(prefix="/", command="claude", args=None)
        )

    async def _menu_mind(message: Message, event_update: Update) -> None:
        # args=None takes the same "list, don't add" branch a bare
        # "/mind" does (mind()'s own `(command.args or "").strip()`).
        await mind(message, event_update, CommandObject(prefix="/", command="mind", args=None))

    async def _menu_paid(message: Message, event_update: Update) -> None:
        # args=None is the list branch, matching a bare "/paid".
        await paid_command(message, event_update, CommandObject(prefix="/", command="paid", args=None))

    async def _menu_interests(message: Message, event_update: Update) -> None:
        # args=None is the list branch, matching a bare "/interests".
        await interests_command(
            message, event_update, CommandObject(prefix="/", command="interests", args=None)
        )

    async def _menu_digest(message: Message, event_update: Update) -> None:
        # args=None is /digest's own default window (idle_ui.parse_digest_args(None)).
        await digest_command(
            message, event_update, CommandObject(prefix="/", command="digest", args=None)
        )

    async def _menu_hide_kb(message: Message, event_update: Update) -> None:
        del event_update  # a fixed reply, nothing to gate or store.
        await message.answer(HIDE_KB_REPLY, reply_markup=ReplyKeyboardRemove())

    MENU_ACTIONS = {
        "checkin": checkin_command,
        "state": _menu_state,
        "plan": plan_command,
        "memories": memories,
        "mind": _menu_mind,
        "amendments": amendments_command,
        "notes": notes_command,
        "interests": _menu_interests,
        "orders": orders_command,
        "paid": _menu_paid,
        "review": review_command,
        "quiet_30m": _menu_quiet_30m,
        "quiet_2h": _menu_quiet_2h,
        "quiet_8h": _menu_quiet_8h,
        "quiet_1d": _menu_quiet_1d,
        "quiet_off": _menu_quiet_off,
        "focus_on": _menu_focus_on,
        "focus_off": _menu_focus_off,
        "out": out,
        "in": resume,
        "digest": _menu_digest,
        "privacy": privacy,
        "vault": _menu_vault,
        "claude": _menu_claude,
        "revoke": revoke_command,
        "hide_kb": _menu_hide_kb,
    }
    # The table above and app/tg/menu.ACTIONS must name exactly the same
    # actions, or a button the hub renders could dispatch nowhere (or an
    # entry here could sit dead, dispatched by nothing render() ever
    # draws) -- see that module's own docstring for why the excluded
    # commands (export, delete, grok, ...) are absent from both.
    assert set(MENU_ACTIONS) == set(menu.ACTIONS), (
        "app/tg/router.py's MENU_ACTIONS and app/tg/menu.ACTIONS have drifted apart"
    )

    @router.callback_query(F.data.startswith("mn:"))
    async def menu_callback(callback: CallbackQuery, event_update: Update) -> None:
        """`mn:s:<section>` / `mn:a:<action>` / `mn:x` -- the /menu hub.

        Registered ahead of the catch-all `unknown_callback` below.
        Order against every *other* callback_query handler in this
        router does not matter: no existing prefix is a prefix of
        "mn:", or the reverse (app/tg/menu.py's own docstring lists
        them), so aiogram's first-match-wins routing can never confuse
        this with one of them.
        """
        if not isinstance(callback.message, Message):
            # InaccessibleMessage (or, in principle, None): Telegram
            # gives this back for a button under a message too old to
            # edit. Nothing to edit, nothing to dispatch.
            await answer_callback(callback.bot, callback.id, "Меню устарело — /menu.")
            return

        chat_id = callback.message.chat.id
        message_id = callback.message.message_id
        web = getattr(callback.bot, "is_web_sink", False)

        parsed = menu.parse_callback(callback.data)
        if parsed is None:
            await answer_callback(callback.bot, callback.id, memory_ui.STALE)
            return
        kind, value = parsed

        if kind == "close":
            await answer_callback(callback.bot, callback.id)
            await edit_keyboard(callback.bot, chat_id, message_id, menu.CLOSED_TEXT, None)
            return

        if kind == "section":
            rendered = menu.render(value, settings, web=web)
            if rendered is None:
                await answer_callback(callback.bot, callback.id, memory_ui.STALE)
                return
            await answer_callback(callback.bot, callback.id)
            await edit_keyboard(callback.bot, chat_id, message_id, *rendered)
            return

        # kind == "action". Re-checking action_available here (render()
        # already only ever draws a button that passes it) is what makes
        # a forged "mn:a:export"/"mn:a:delete"/"mn:a:grok" from a
        # tampered web client harmless: it answers stale and dispatches
        # nothing, exactly like a section this build has never heard of.
        if not menu.action_available(value, settings, web=web):
            await answer_callback(callback.bot, callback.id, memory_ui.STALE)
            return
        await answer_callback(callback.bot, callback.id)
        async with sessionmaker() as session:
            await checkin_core.clear_awaiting(session)
        await MENU_ACTIONS[value](callback.message, event_update)

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

    @router.callback_query(F.data.startswith("nb:x:"))
    async def notebook_close(callback: CallbackQuery) -> None:
        """`nb:x:<id>` -- a `/mind` entry's [✖] button. Closes any entry,
        including Anchor's own -- see app/tg/notebook.py's docstring."""
        await notebook_ui.handle_close_callback(
            sessionmaker,
            callback.bot,
            clock,
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

        Web-chat plan track 1: guarded the same way export_command/
        delete_command are, and belt-and-braces on top of that --
        app/web/ingress.py refuses to ever enqueue a `d:`-prefixed press
        in the first place, and delete_command's own guard above means
        WebSinkSession never sends this keyboard to begin with. Three
        independent things would all have to fail at once for this
        branch to matter, which is the point.
        """
        if getattr(callback.bot, "is_web_sink", False):
            await callback.bot.answer_callback_query(callback.id, text=WEB_ONLY_REPLY)
            return
        await data_ui.handle_delete_callback(
            sessionmaker,
            callback.bot,
            settings,
            clock,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            data=callback.data,
            hub=hub,
            claude_pending=claude_pending,
            code_store=code_store,
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

    @router.callback_query(F.data.startswith("so:x:"))
    async def order_retire(callback: CallbackQuery) -> None:
        """`so:x:<id>` -- `/orders`' own [Снять]. Registered ahead of the
        generic `so:` handler below, which would otherwise swallow it
        (aiogram routes callback_query first-match-wins, same reason
        the memory/research paging callbacks above are ordered)."""
        await orders_ui.handle_retire_callback(
            sessionmaker,
            callback.bot,
            clock,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            data=callback.data,
        )

    @router.callback_query(F.data.startswith("so:"))
    async def order_decision(callback: CallbackQuery) -> None:
        """`so:a:<id>` / `so:c:<id>` / `so:r:<id>` -- accept, start a
        counter, or decline/cancel. Shared by the proposal card and the
        counter card (app/tg/orders.py's own docstring says why)."""
        await orders_ui.handle_decision_callback(
            sessionmaker,
            callback.bot,
            settings,
            clock,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            data=callback.data,
        )

    @router.callback_query(F.data.startswith("ob:"))
    async def obligation_decision(callback: CallbackQuery) -> None:
        """Phase 5: `ob:d:<id>` / `ob:x:<id>` -- close or drop a debt."""
        await obligations_ui.handle_callback(
            sessionmaker,
            callback.bot,
            clock,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            data=callback.data,
        )

    @router.callback_query(F.data.startswith("am:x:"))
    async def amendment_revoke(callback: CallbackQuery) -> None:
        """`am:x:<id>` -- `/amendments`' own [Отозвать]. Registered ahead
        of the generic `am:` handler below, which would otherwise
        swallow it (same first-match-wins reasoning as `so:x:` above)."""
        await amendments_ui.handle_revoke_callback(
            sessionmaker,
            callback.bot,
            clock,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            data=callback.data,
            persona_path=persona_path_for(settings),
        )

    @router.callback_query(F.data.startswith("am:"))
    async def amendment_decision(callback: CallbackQuery) -> None:
        """`am:a:<id>` / `am:r:<id>` -- adopt or decline a `persona_note`
        review proposal card."""
        await review_ui.handle_decision_callback(
            sessionmaker,
            callback.bot,
            settings,
            clock,
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
            clock,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            data=callback.data,
        )

    async def _planner_button_off(callback: CallbackQuery) -> bool:
        """True (and the button is retired) when the planner is off.

        The commands refuse while PLANNER_ENABLED is false; a button left
        on an older message must too. Without this a stale confirm card
        or /done button created a planner_action and queued a
        PLANNER_WRITE the worker cannot run with no planner client.
        """
        if settings.PLANNER_ENABLED:
            return False
        await answer_callback(callback.bot, callback.id, planner_ui.DISABLED)
        await edit_keyboard(
            callback.bot,
            callback.message.chat.id,
            callback.message.message_id,
            planner_ui.DISABLED,
            None,
        )
        return True

    @router.callback_query(F.data.startswith("pa:"))
    async def planner_action_decision(callback: CallbackQuery) -> None:
        """`pa:y:<id>` / `pa:n:<id>` -- the /task and /event confirm card."""
        if await _planner_button_off(callback):
            return
        async with sessionmaker() as session:
            user_state = await get_state(session)
        await planner_ui.handle_confirm_callback(
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

    @router.callback_query(F.data.startswith("pl:d:"))
    async def planner_done(callback: CallbackQuery) -> None:
        """`pl:d:<task id>` -- a /done list button."""
        if await _planner_button_off(callback):
            return
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

    @router.callback_query(F.data.startswith("g:"))
    async def grok_decision(callback: CallbackQuery) -> None:
        """`g:<action>:<mask>:<period>:<ttl>:<epoch>` -- the /grok picker.

        Telegram only, like /export and /delete: a grant's capability URL
        must never be rendered into the web chat.
        """
        if getattr(callback.bot, "is_web_sink", False):
            await callback.answer(WEB_ONLY_REPLY)
            return
        await grok_ui.handle_callback(
            sessionmaker,
            callback.bot,
            settings,
            clock,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            data=callback.data,
        )

    @router.callback_query(F.data.startswith("cl:"))
    async def claude_decision(callback: CallbackQuery) -> None:
        """`cl:<action>:<mask>:<period>:<ttl>:<epoch>` -- the /claude picker.

        Telegram only, like /grok's: a window is opened from here or not
        at all.
        """
        if getattr(callback.bot, "is_web_sink", False):
            await callback.answer(WEB_ONLY_REPLY)
            return
        await claude_ui.handle_callback(
            sessionmaker,
            callback.bot,
            settings,
            clock,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            data=callback.data,
        )

    @router.callback_query(F.data.startswith("it:x:"))
    async def interest_remove(callback: CallbackQuery) -> None:
        """`it:x:<id>` -- `/interests`' own [✖]."""
        await interests_ui.handle_remove_callback(
            sessionmaker,
            callback.bot,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            data=callback.data,
        )

    @router.callback_query(F.data.startswith("idle:u:"))
    async def idle_undo(callback: CallbackQuery) -> None:
        """`idle:u:<run_id>` -- /digest's own [Отменить] button."""
        await idle_ui.handle_undo_callback(
            sessionmaker,
            callback.bot,
            settings,
            clock,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            data=callback.data,
        )

    @router.callback_query(F.data.startswith("v:"))
    async def vault_decision(callback: CallbackQuery) -> None:
        """`v:y:<hold_id>:<epoch>` / `v:n:<hold_id>:<epoch>` -- a hold's own [Да]/[Нет, вернуть].

        Refused from the web chat, like `d:`, `g:` and `cl:`: a rule is
        the user instructing Anchor, and until 8c only an authenticated
        Telegram chat could create one (phase-8 plan section 8). Hold
        messages are only ever sent to Telegram, so a press arriving
        through the web sink is not one the user made on that message.
        """
        if getattr(callback.bot, "is_web_sink", False):
            await callback.bot.answer_callback_query(callback.id, text=WEB_ONLY_REPLY)
            return
        await vault_ui.handle_callback(
            sessionmaker,
            callback.bot,
            clock,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            data=callback.data,
            message_text=callback.message.text,
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

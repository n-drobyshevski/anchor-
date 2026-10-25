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
from app.core import grants as grants_core
from app.core import memory as memory_core
from app.core import menu_prompt
from app.core import mood as mood_core
from app.core import obligations as obligations_core
from app.core import pause
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
from app.llm.provider import DecisionProvider, LLMProvider
from app.ops.backup import latest_backup_status
from app.planner import actions as planner_actions
from app.planner import auth as planner_auth
from app.planner import parse as planner_parse
from app.planner import snapshot as planner_snapshot
from app.research import jobs as research_jobs
from app.tg.send import answer_callback, edit_keyboard, send_keyboard
from app.tg import amendments as amendments_ui
from app.tg import checkin as checkin_ui
from app.tg import claude as claude_ui
from app.tg import data as data_ui
from app.tg import garden as garden_ui
from app.tg import grok as grok_ui
from app.tg import idle as idle_ui
from app.tg import interests as interests_ui
from app.tg import graph as graph_ui
from app.tg import lens as lens_ui
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
from app.vault.client import VaultClient

logger = logging.getLogger(__name__)

NON_TEXT_REPLY = "Пока только текст."

START_TEXT = (
    "Я — Echo. Здесь по-русски, коротко и по делу.\n"
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
    "Заметки из Obsidian Echo читает только с твоей меткой: личные — только для "
    "разговора с тобой, никогда для поиска или исследований; знания — как справка, "
    "а при /claude library on их может искать Claude (найденное уходит в Anthropic), "
    "при /claude library write on — и менять (откат: /claude undo). "
    "Заметки линзы при /lens code on может читать Claude Code (прочитанное уходит в Anthropic) — "
    "вместе с тем, какие из них выбрал еженедельный разбор или рефлексия заметок, но не с объяснением почему: "
    "у разбора оно написано по твоей неделе и видно только тебе, у рефлексии не хранится вовсе. "
    "Пока линза включена, еженедельный разбор отправляет модели "
    "каталог линзы (названия, краткие описания или начало текста, связи) и выбранные "
    "заметки целиком — как справочный материал. "
    "Пока включена линза в рефлексии заметок, модели уходят каталог линзы, черновик изменений "
    "рабочих заметок Echo и выбранные заметки целиком; по линзе переформулируются только "
    "незакрытые темы, наблюдения о тебе — никогда, и такие темы остаются в заметках, которые "
    "читает персонаж, пока не закроются или не истекут, даже если линзу выключить. "
    "Рефлексия по отдельному разговору и оценка ответов текста линзы модели не отправляют; "
    "на какие заметки линзы опирались оценённые ответы, видно только в /export. "
    "Пока включён сад линзы, раз в неделю модели уходят названия и краткие описания "
    "(или начало текста) заметок линзы, связи между ними и сколько у каждой связей "
    "с заметками знаний (без их названий); его предложения при "
    "/lens code on видит и Claude Code. "
    "Если нажать «исследовать» под пунктом сада, модели уходят этот пункт, названия и краткие "
    "описания его заметок, поисковый запрос — в Exa (через OpenRouter), страницы берутся только с сайтов из "
    "PACKET_LENS; принятое («в Inbox») становится заметкой знаний в Echo/Inbox, её можно "
    "отменить 14 дней (/lens undo). "
    "Предложенные темы для поиска (/interests suggest и раз в неделю сами): модели уходят "
    "сохранённые и снятые темы, тексты отклонённых предложений и, пока линза включена, "
    "названия и краткие описания (или начало текста) её заметок — без личных заметок, "
    "сообщений, памяти, журнала и карточек. Запрос идёт только в OpenRouter и ничего не ищет: "
    "в поиск тема попадает, лишь когда нажмёшь «добавить» или «добавить и искать». "
    "Предложения и что ты с ними сделал есть в /export и удаляются с /delete; "
    "Claude Code видит только, что они есть (номера, статус, время), но не текст. "
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
    BotCommand(command="intensity", description="Интенсивность 1–5"),
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
    BotCommand(command="mind", description="Заметки Echo"),
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
    # L1 (anchor-lens-plan.md section 11): the lens and Claude Code's door.
    BotCommand(command="lens", description="Линза и доступ Claude Code"),
    # Footer link pruning (app/tg/graph.py): dry run, apply, undo.
    BotCommand(command="graph", description="Чистка ссылок в футерах заметок"),
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
INTENSITY_NOW = "Интенсивность: {value}/5. Сменить: /intensity 1–5 (1 — мягче, 5 — строже)."
INTENSITY_USAGE = "Сколько? /intensity 1–5: 1 — мягче, 5 — строже."
INTENSITY_SET = "Интенсивность: {value}/5."

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

# The menu's «⚠️ Закрыть веб-сессии» on a router built without a web hub
# (app/tg/menu.py gates the button on WEB_UI_ENABLED, which is what
# builds the hub in app/main.py; a test can build one without).
WEB_UI_OFF_REPLY = "Веб-чат выключен."


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
    welfare_decider: DecisionProvider | None = None,
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

    `welfare_decider` is the welfare shadow's decision model (app/core/
    welfare_shadow.py), None unless WELFARE_SHADOW_MODEL is set. Only
    the chat turn below takes it: the shadow is a comparison, and the
    ordinary turn is where the classifier runs most.
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

    async def _menu_view(section: str, *, web: bool = False) -> menu.MenuView:
        """The live state menu.py's sections show beyond `settings`: the
        user_state row for every section (main, today and settings all
        show some of it), plus -- only for the section that shows it, so
        no query runs for a card that draws nothing of it:

        - knowledge: 8f's `settings_state`/`knowledge_roots` and the
          notes counts, from vaultd's manifest, fetched the same way
          /vault itself does (a status probe, then the manifest) and
          only while notes consent is on -- consent off leaves them at
          their "not fetched" default, exactly like
          `format_notes_line`'s own `notes=None`;
        - research: today's quota use (app/research/jobs.py's
          `used_today`);
        - lens: the lens counts and Claude Code's door
          (app/tg/lens.py's `menu_counts`);
        - links: the planner credential, the Claude connection, the
          open Grok grants and the live web sessions, each only while
          its feature is on -- and, on the web sink, only the planner,
          whose status is the one the web card shows;
        - planner: the planner credential; claude: the connection.

        Counts and switches only: nothing here reads a note, a title or
        a token.
        """
        connection = None
        credential = None
        overview = None
        research_used = None
        lens_counts = None
        grok_grants = None
        web_sessions = None
        async with sessionmaker() as session:
            user_state = await get_state(session)
            wants_connection = section == "claude" or (section == "links" and not web)
            if wants_connection and settings.CLAUDE_ACCESS_ENABLED:
                connection = await oauth_store.current_connection(session, clock)
            if section == "knowledge" and user_state.notes_consent:
                health = await vault_status.probe(session, settings, clock)
                overview = await vault_status.notes_overview(settings, health)
            if section in ("planner", "links") and settings.PLANNER_ENABLED:
                credential = await planner_auth.get_status(session)
            if section == "research" and settings.RESEARCH_ENABLED:
                used = await research_jobs.used_today(
                    session, clock_module.local_date(clock, user_state.timezone)
                )
                research_used = (
                    used[research_jobs.STUDY],
                    used[research_jobs.MANUAL],
                    used[research_jobs.READ],
                )
            if section == "links" and not web:
                if settings.GROK_ACCESS_ENABLED:
                    grok_grants = len(
                        await grants_core.list_active(session, clock, client=grants_core.GROK)
                    )
                if settings.WEB_UI_ENABLED:
                    web_sessions = await web_auth.count_live(session, clock, settings)
        if section == "lens" and not web:
            lens_counts = await lens_ui.menu_counts(sessionmaker, clock)
        return menu.MenuView(
            now=clock.now_utc(),
            timezone=user_state.timezone,
            persona_active=user_state.persona_active,
            focus_on=user_state.focus_on,
            intensity=user_state.intensity,
            quiet_until=user_state.quiet_until,
            due_action=user_state.due_action,
            notes_consent=user_state.notes_consent,
            library_read=connection.library_read if connection else None,
            library_write=bool(connection.library_write) if connection else False,
            planner_status=credential.status if credential else None,
            planner_enabled=bool(credential.enabled) if credential else False,
            settings_state=overview.settings if overview is not None else None,
            knowledge_roots=overview.knowledge_roots if overview is not None else (),
            knowledge_notes=overview.knowledge if overview is not None else None,
            personal_notes=overview.personal if overview is not None else None,
            research_used=research_used,
            lens_size=lens_counts[0] if lens_counts else None,
            lens_code=lens_counts[1] if lens_counts else None,
            lens_today_reads=lens_counts[2] if lens_counts else None,
            grok_grants=grok_grants,
            web_sessions=web_sessions,
        )

    async def _edit_menu(
        bot,
        chat_id: int,
        message_id: int,
        rich,
        plain_text: str,
        plain_markup,
    ) -> None:
        """Edit the menu message in place to `rich`, falling back to the
        plain `(plain_text, plain_markup)` pair on any rejection other
        than the idempotent "not modified" -- mirrors /state's own edit
        fallback (router.py ~672-695, above)."""
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, rich_message=rich, reply_markup=None
            )
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            logger.warning(
                "menu rich message rejected: %s",
                type(exc).__name__,
                extra={"event": "menu_rich_fallback"},
            )
            await edit_keyboard(bot, chat_id, message_id, plain_text, plain_markup)

    async def _show_menu(
        bot,
        chat_id: int,
        section: str,
        *,
        message_id: int | None = None,
        origin: str | None = None,
    ) -> int | None:
        """Send (`message_id=None`) or edit (`message_id` set) the
        `/menu` hub, showing `section`. Shared by `/menu`, the `☰ Меню`
        reply-keyboard button, every `mn:s:` section tap, and the
        re-render after a switch (menu.ACTIONS' `refresh`). `mn:x` (closing
        the menu) is not a section and is rendered separately, in
        `menu_callback` below.

        Returns the shown message's id, or None if `section` does not
        exist -- `menu.render`/`render_rich`'s own None, both built from
        the same spec (menu.py's own docstring), so a caller can treat
        this exactly like those functions' own "no such section" and
        never sees the two disagree. `/menu`'s own first send and a
        switch's re-render both always pass a section that exists, so
        that branch is reachable only from a stale/forged `mn:s:<name>`
        tap.

        On the web sink: the same plain `send_keyboard`/`edit_keyboard`
        path `/menu` always used (app/web/sink.py only ever understands
        plain SendMessage/EditMessageText). Otherwise: 10.1's
        `sendRichMessage`/`editMessageText(rich_message=...)`, with a
        `TelegramBadRequest` on *send* falling back to a plain message
        and on *edit* falling back through `_edit_menu` above -- so a
        rich-message rejection, or a pre-upgrade plain menu message
        someone taps later, never leaves `/menu` silent.

        `origin` is where a two-parent section (quiet) was opened from,
        so its back button returns there (app/tg/menu.py's docstring).
        """
        web = getattr(bot, "is_web_sink", False)
        if not menu.section_exists(section, settings, web=web):
            return None
        view = await _menu_view(section, web=web)

        if web:
            rendered = menu.render(section, settings, web=web, view=view, origin=origin)
            if rendered is None:
                return None
            text, markup = rendered
            if message_id is None:
                return await send_keyboard(bot, chat_id, text, markup)
            await edit_keyboard(bot, chat_id, message_id, text, markup)
            return message_id

        rich = menu.render_rich(section, settings, web=web, view=view, origin=origin)
        if rich is None:
            return None
        if message_id is None:
            try:
                message = await bot.send_rich_message(chat_id=chat_id, rich_message=rich)
                return message.message_id
            except TelegramBadRequest as exc:
                logger.warning(
                    "menu rich message rejected: %s",
                    type(exc).__name__,
                    extra={"event": "menu_rich_fallback"},
                )
                text, markup = menu.render(section, settings, web=web, view=view, origin=origin)
                return await send_keyboard(bot, chat_id, text, markup)
        text, markup = menu.render(section, settings, web=web, view=view, origin=origin)
        await _edit_menu(bot, chat_id, message_id, rich, text, markup)
        return message_id

    async def _retire_menu_message(bot, chat_id: int, message_id: int | None, text: str) -> None:
        """Edit a prompt or a confirmation card to `text` with no
        buttons, so it never lingers asking for something nothing will
        read. Best effort: a message the user deleted, or one too old to
        edit, must not fail the update that is ending it -- the step it
        belonged to is over either way. (A web-issued id edited from
        Telegram is already a no-op in `edit_keyboard`.)"""
        if message_id is None:
            return
        try:
            await edit_keyboard(bot, chat_id, message_id, text, None)
        except TelegramBadRequest as exc:
            logger.warning(
                "menu prompt edit rejected: %s",
                type(exc).__name__,
                extra={"event": "menu_prompt_retire"},
            )

    async def _end_menu_prompt(bot, chat_id: int, text: str = menu.CANCELLED_TEXT) -> None:
        """End a pending /menu text prompt, if there is one, and edit its
        message to `text` (app/core/menu_prompt.py). Called by everything
        that ends a prompt besides its own reply: a slash command (the
        outer middleware), the ☰ Меню button, any menu tap, ✕ Отмена.
        Touches no other kind of `awaiting` step -- those keep their own
        clearing rules, unchanged."""
        async with sessionmaker() as session:
            prompt = await menu_prompt.current(session)
            if prompt is None:
                return
            await menu_prompt.close_prompt(session, prompt.key)
        await _retire_menu_message(bot, chat_id, prompt.message_id, text)

    async def _send_menu(message: Message) -> None:
        """Shared by `/menu` and the `☰ Меню` reply-keyboard button below."""
        await _show_menu(message.bot, message.chat.id, menu.MAIN_SECTION)

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
        the menu button is a command, not a check-in note -- and, by the
        same rule, not the reply a /menu text prompt is waiting for
        (`_end_menu_prompt`).
        """
        if not await _once(event_update.update_id):
            return
        await _end_menu_prompt(message.bot, message.chat.id)
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
            # The menu redesign's text prompt is one of these steps: a
            # slash command ends it, and its message is edited to
            # «Отменено.» rather than left asking for text nothing will
            # read.
            bot = getattr(event, "bot", None)
            chat = getattr(event, "chat", None)
            if bot is not None and chat is not None:
                await _end_menu_prompt(bot, chat.id)
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

    @router.message(Command("intensity"))
    async def intensity_command(
        message: Message, event_update: Update, command: CommandObject
    ) -> None:
        """/intensity 1-5: the dial plan section 13 says only a command,
        a button, pause handling or check-in logic may move. Before this
        existed a soft pause word could lower it and nothing could ever
        raise it back. Bare /intensity shows the current value."""
        raw = (command.args or "").strip()
        if not raw:
            async with sessionmaker() as session:
                user_state = await get_state(session)
            await _reply_once(
                message, event_update.update_id, INTENSITY_NOW.format(value=user_state.intensity)
            )
            return
        if raw not in {str(n) for n in range(commands_core.INTENSITY_MIN, commands_core.INTENSITY_MAX + 1)}:
            await _reply_once(message, event_update.update_id, INTENSITY_USAGE)
            return
        async with sessionmaker() as session:
            await commands_core.set_intensity(session, int(raw), "command")
        await _reply_once(message, event_update.update_id, INTENSITY_SET.format(value=raw))

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

    @router.message(Command("lens"))
    async def lens_command(
        message: Message, event_update: Update, command: CommandObject
    ) -> None:
        """The lens (anchor-lens-plan.md section 11): status, and
        `/lens code on|off`, Claude Code's database door. L4: `/lens
        undo`, Echo's newest inbox write taken back through vaultd.

        Telegram only, like /claude: `code on` opens a login to the
        database, and a web chat must never be able to type it.
        """
        if not await _once(event_update.update_id):
            return
        if getattr(message.bot, "is_web_sink", False):
            await _reply_once(message, event_update.update_id, WEB_ONLY_REPLY)
            return
        scene_id = await turn.ensure_scene(sessionmaker, settings, clock)
        text = await lens_ui.command(
            sessionmaker, settings, clock, command.args, client_factory=VaultClient.from_settings
        )
        await send_keyboard(message.bot, message.chat.id, text, None)
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text="[/lens]", scene_id=scene_id
        )

    @router.message(Command("graph"))
    async def graph_command(
        message: Message, event_update: Update, command: CommandObject
    ) -> None:
        """Footer link pruning (app/tg/graph.py): `/graph` dry run,
        `/graph apply <hash>`, `/graph undo`. Telegram only: applying
        edits the vault."""
        if not await _once(event_update.update_id):
            return
        if getattr(message.bot, "is_web_sink", False):
            await _reply_once(message, event_update.update_id, WEB_ONLY_REPLY)
            return
        scene_id = await turn.ensure_scene(sessionmaker, settings, clock)
        reply = await graph_ui.command(
            sessionmaker, settings, clock, command.args, client_factory=VaultClient.from_settings
        )
        await graph_ui.send(message.bot, message.chat.id, reply)
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text="[/graph]", scene_id=scene_id
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

            Telegram only: a web session's typed `/weblogout` would
            reach this handler as a synthetic Update like any other web
            text, so app/web/ingress.py's BLOCKED_COMMANDS refuses to
            queue it, and the menu button is TELEGRAM_ONLY
            (app/tg/menu.py). The kill switch for a stolen web session
            must not be in that session's hands.
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
    #
    # `run` is /study's shape, not `add`'s: research_ui.DISABLED before
    # anything else (a disabled feature says so before it says how to
    # use it, as in the research block below), then the usage reply,
    # then the `_once` gate. The gate is not optional here:
    # enqueue_interest inserts a fresh study_job row and stamps the
    # topic's last_run_at on every call, so a replayed update without it
    # would queue the search twice and skip a topic in the rotation. The
    # list, and the [✖] callback's re-render, carry the "run" hint only
    # while research is on (interests_ui.render_topics_list).
    #
    # `suggest` (anchor-interest-suggestions-plan.md section 5, step 1)
    # is Telegram only, like /lens: its message can carry lens text and
    # its buttons can start a paid search, so the web chat gets
    # WEB_ONLY_REPLY before anything else. Then research_ui.DISABLED,
    # then `_once` -- enqueue_suggest inserts a batch and a job, so a
    # replayed update must not pay twice. A finished batch that never
    # went out is sent as the reply itself (run_suggest returns None),
    # and the update is then only marked handled, as the list's is.

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

        if parts and parts[0].lower() == "run":
            if not settings.RESEARCH_ENABLED:
                await _reply_once(message, event_update.update_id, research_ui.DISABLED)
                return
            ok, topic_id = interests_ui.parse_run_args(parts[1] if len(parts) > 1 else "")
            if not ok:
                await _reply_once(message, event_update.update_id, interests_ui.RUN_USAGE)
                return
            if not await _once(event_update.update_id):
                return
            async with sessionmaker() as session:
                user_state = await get_state(session)
            reply = await interests_ui.run_now(
                sessionmaker,
                settings,
                clock,
                timezone=user_state.timezone,
                topic_id=topic_id,
            )
            # Same as /study: run_now only enqueues, so the reply goes
            # out through _reply_once, not mark_update_handled.
            await _reply_once(message, event_update.update_id, reply)
            return

        if parts and parts[0].lower() == "suggest":
            if getattr(message.bot, "is_web_sink", False):
                await _reply_once(message, event_update.update_id, WEB_ONLY_REPLY)
                return
            if not settings.RESEARCH_ENABLED:
                await _reply_once(message, event_update.update_id, research_ui.DISABLED)
                return
            if not await _once(event_update.update_id):
                return
            async with sessionmaker() as session:
                user_state = await get_state(session)
            reply = await interests_ui.run_suggest(
                sessionmaker,
                message.bot,
                settings,
                clock,
                chat_id=message.chat.id,
                timezone=user_state.timezone,
            )
            if reply is None:
                await turn.mark_update_handled(
                    sessionmaker,
                    clock=clock,
                    update_id=event_update.update_id,
                    text="[/interests suggest]",
                )
                return
            await _reply_once(message, event_update.update_id, reply)
            return

        if not await _once(event_update.update_id):
            return
        await interests_ui.run_list(
            sessionmaker,
            message.bot,
            chat_id=message.chat.id,
            research_on=settings.RESEARCH_ENABLED,
        )
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
        reply, markup = await research_ui.run_card_view(sessionmaker, card_id=card_id)
        if markup is None:
            await _reply_once(message, event_update.update_id, reply)
            return
        # A pending card carries its own [Принять][Отклонить] (menu
        # redesign; the `r:a:`/`r:r:` buttons a /notes row has), so it is
        # a keyboard message: sent directly and gated on `_once`, the
        # /notes shape, rather than a stored canned reply.
        if not await _once(event_update.update_id):
            return
        await send_keyboard(message.bot, message.chat.id, reply, markup)
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text="[/card]"
        )

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
    # are plain, directly callable coroutines. Most actions need a
    # `CommandObject` the way a real command line would produce one
    # (read each handler above to see what it does with `command.args`):
    # `_fixed` builds one with the action's fixed argument, `_typed` one
    # with a prompt's prefix plus the text the user typed. The rest
    # already take exactly (message, event_update) and are listed as-is.
    # Kept explicit and one-to-one on purpose -- no signature
    # introspection, and no reading the arguments back out of
    # menu.ACTIONS, so any reader can trace an action to the exact call
    # it makes (menu.ACTIONS' `args` field documents the same values).
    #
    # Three tables, one per kind of action (app/tg/menu.py's docstring):
    # RUN_ACTIONS runs on the tap, CONFIRM_ACTIONS on «Да» (with the
    # card's argument, the typed id or code), INPUT_ACTIONS on a prompt's
    # reply (with its text).

    def _command(name: str, args: str | None) -> CommandObject:
        return CommandObject(prefix="/", command=name, args=args)

    def _fixed(handler, name: str, args: str | None = None):
        async def run(message: Message, event_update: Update) -> None:
            await handler(message, event_update, _command(name, args))

        return run

    def _typed(handler, name: str, prefix: str | None = None):
        async def run(message: Message, event_update: Update, text: str) -> None:
            await handler(message, event_update, _command(name, f"{prefix} {text}" if prefix else text))

        return run

    async def _menu_state(message: Message, event_update: Update) -> None:
        del event_update  # state() takes no update_id -- nothing to gate here.
        await state(message)

    def _menu_vault(args: str | None):
        async def run(message: Message, event_update: Update) -> None:
            del event_update  # vault() takes no event_update -- see its own signature above.
            await vault(message, _command("vault", args))

        return run

    async def _menu_hide_kb(message: Message, event_update: Update) -> None:
        del event_update  # a fixed reply, nothing to gate or store.
        await message.answer(HIDE_KB_REPLY, reply_markup=ReplyKeyboardRemove())

    async def _menu_weblogout(message: Message, event_update: Update) -> None:
        """`/weblogout` exists only with a hub (build_router's
        docstring). The button's gate is WEB_UI_ENABLED, which is what
        builds that hub in production; a router built without one says
        so rather than claiming sessions were closed."""
        if hub is None:
            await _reply_once(message, event_update.update_id, WEB_UI_OFF_REPLY)
            return
        await weblogout(message, event_update)

    RUN_ACTIONS = {
        "checkin": checkin_command,
        "state": _menu_state,
        "plan": plan_command,
        "focus_on": _fixed(focus, "focus", "on"),
        "focus_off": _fixed(focus, "focus", "off"),
        "done": done_command,
        "orders": orders_command,
        # args=None is the list branch of each, matching the bare command.
        "paid": _fixed(paid_command, "paid"),
        "review": review_command,
        "memories": memories,
        "mind": _fixed(mind, "mind"),
        "amendments": amendments_command,
        # args=None is /digest's own default window (idle_ui.parse_digest_args(None)).
        "digest": _fixed(digest_command, "digest"),
        "digest_7d": _fixed(digest_command, "digest", "7d"),
        "interests": _fixed(interests_command, "interests"),
        "interests_run": _fixed(interests_command, "interests", "run"),
        "interests_suggest": _fixed(interests_command, "interests", "suggest"),
        "notes": notes_command,
        "notes_on": _menu_vault("notes on"),
        "notes_off": _menu_vault("notes off"),
        "vault": _menu_vault(None),
        "graph": _fixed(graph_command, "graph"),
        "lens": _fixed(lens_command, "lens"),
        "lens_garden": _fixed(lens_command, "lens", "garden now"),
        "lens_research": _fixed(lens_command, "lens", "research now"),
        "lens_code_off": _fixed(lens_command, "lens", "code off"),
        "grok_open": grok_command,
        "revoke": revoke_command,
        "planner": _fixed(planner_command, "planner"),
        "planner_on": _fixed(planner_command, "planner", "on"),
        "planner_off": _fixed(planner_command, "planner", "off"),
        # `claude_command` keeps its own is_web_sink guard and `_once`
        # gate -- reached here only when `action_available` already
        # refused a web press, this is defence in depth.
        "claude": _fixed(claude_command, "claude"),
        "lib_read_on": _fixed(claude_command, "claude", "library on"),
        "lib_read_off": _fixed(claude_command, "claude", "library off"),
        "lib_write_on": _fixed(claude_command, "claude", "library write on"),
        "lib_write_off": _fixed(claude_command, "claude", "library write off"),
        "claude_limits": _fixed(claude_command, "claude", "limits"),
        **{f"int_{n}": _fixed(intensity_command, "intensity", str(n)) for n in range(1, 6)},
        "out": out,
        "in": resume,
        "hide_kb": _menu_hide_kb,
        "quiet_30m": _fixed(quiet, "quiet", "30m"),
        "quiet_2h": _fixed(quiet, "quiet", "2h"),
        "quiet_8h": _fixed(quiet, "quiet", "8h"),
        "quiet_1d": _fixed(quiet, "quiet", "1d"),
        "quiet_off": _fixed(quiet, "quiet", "off"),
        "privacy": privacy,
        # Its own two-step card (app/tg/data.py) is the confirmation.
        "delete": delete_command,
    }

    def _confirmed(handler, name: str, args: str | None = None):
        async def run(message: Message, event_update: Update, arg: str | None) -> None:
            del arg  # a fixed confirmation carries no argument of its own
            await handler(message, event_update, _command(name, args))

        return run

    def _confirmed_bare(handler):
        async def run(message: Message, event_update: Update, arg: str | None) -> None:
            del arg
            await handler(message, event_update)

        return run

    async def _menu_forget_yes(message: Message, event_update: Update, arg: str | None) -> None:
        # The id rode in the card's own callback data; /forget parses it
        # again, exactly as it would a typed one.
        await forget(message, event_update, _command("forget", arg))

    async def _menu_claude_connect_yes(message: Message, event_update: Update, arg: str | None) -> None:
        await claude_command(message, event_update, _command("claude", f"connect {arg}"))

    CONFIRM_ACTIONS = {
        # A bare /due clears the main action (menu.ACTIONS' own comment).
        "due_clear": _confirmed(due, "due"),
        "graph_undo": _confirmed(graph_command, "graph", "undo"),
        "lens_undo": _confirmed(lens_command, "lens", "undo"),
        "lens_code_on": _confirmed(lens_command, "lens", "code on"),
        "weblogout": _confirmed_bare(_menu_weblogout),
        "planner_link": _confirmed_bare(planner_link_command),
        "claude_undo": _confirmed(claude_command, "claude", "undo"),
        "claude_undo_all": _confirmed(claude_command, "claude", "undo all"),
        "claude_disconnect": _confirmed(claude_command, "claude", "disconnect"),
        "export": _confirmed_bare(export_command),
        # menu.CONFIRM_AFTER_INPUT: prompts whose reply leads to a card.
        "forget": _menu_forget_yes,
        "claude_connect": _menu_claude_connect_yes,
    }

    _BIGINT_MAX = 2**63 - 1

    def _typed_id(handler, name: str, missing: str):
        """`_typed` for a command that takes one record number. A number
        no row could have (zero, negative, past the BIGINT ids) gets the
        command's own "no such record" reply instead of reaching the
        database as an out-of-range parameter."""

        async def run(message: Message, event_update: Update, text: str) -> None:
            record_id = memory_ui.parse_id(text)
            if record_id is not None and not 0 < record_id <= _BIGINT_MAX:
                await _reply_once(message, event_update.update_id, missing)
                return
            await handler(message, event_update, _command(name, text))

        return run

    async def _menu_forget_input(message: Message, event_update: Update, text: str) -> None:
        """«⚠️ Забыть запись №»'s reply: /forget deletes with no
        confirmation of its own, so the menu shows the record first
        («Забыть #12 «…»?» [Да, забыть][Нет]) and only «Да» runs
        /forget (CONFIRM_ACTIONS). An id that names nothing gets
        /forget's own answers, without a card."""
        memory_id = memory_ui.parse_id(text)
        if memory_id is None:
            await _reply_once(message, event_update.update_id, memory_ui.FORGET_USAGE)
            return
        row = None
        if 0 < memory_id <= _BIGINT_MAX:
            async with sessionmaker() as session:
                row = await memory_core.get_active(session, memory_id)
        if row is None:
            await _reply_once(message, event_update.update_id, memory_ui.FORGET_MISSING)
            return
        if not await _once(event_update.update_id):
            return
        scene_id = await turn.ensure_scene(sessionmaker, settings, clock)
        await send_keyboard(
            message.bot,
            message.chat.id,
            menu.forget_confirm_text(memory_id, row.text),
            menu.confirm_markup("forget", arg=str(memory_id), yes_label=menu.FORGET_YES),
        )
        # Recorded at once, not only by _menu_prompt_reply's own mark
        # afterwards: a crash between the two would replay into a second card.
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text="[/forget]", scene_id=scene_id
        )

    async def _menu_claude_connect_input(message: Message, event_update: Update, text: str) -> None:
        """«✏️ Подключить по коду»'s reply: the code, normalised as the
        pending store will read it (Cyrillic look-alikes as Latin), shown
        back on a «Точно?» card before it approves anything. Text that
        is not shaped like a code at all gets `/claude connect`'s own
        answer (usage for several words, unknown code otherwise), sent
        from here: nothing reaches the pending store without a card
        (menu.claude_connect_code)."""
        code = menu.claude_connect_code(text)
        if code is None:
            reply = claude_ui.USAGE if len(text.split()) > 1 else claude_ui.CODE_UNKNOWN
            await _reply_once(message, event_update.update_id, reply)
            return
        if not await _once(event_update.update_id):
            return
        scene_id = await turn.ensure_scene(sessionmaker, settings, clock)
        await send_keyboard(
            message.bot,
            message.chat.id,
            menu.claude_connect_confirm_text(code),
            menu.confirm_markup("claude_connect", arg=code, yes_label=menu.CLAUDE_CONNECT_YES),
        )
        # As in _menu_forget_input: recorded the moment the card is out.
        await turn.mark_update_handled(
            sessionmaker, clock=clock, update_id=event_update.update_id, text="[/claude]", scene_id=scene_id
        )

    INPUT_ACTIONS = {
        "due_set": _typed(due, "due"),
        "task": _typed(task_command, "task"),
        "event": _typed(event_command, "event"),
        "order_daily": _typed(order_command, "order", "daily"),
        "order_weekdays": _typed(order_command, "order", "weekdays"),
        "order_once": _typed(order_command, "order", "once"),
        **{f"order_w{n}": _typed(order_command, "order", f"weekly:{n}") for n in range(1, 8)},
        "remember": _typed(remember, "remember"),
        "pin": _typed_id(pin, "pin", memory_ui.PIN_MISSING),
        "unpin": _typed_id(unpin, "unpin", memory_ui.PIN_MISSING),
        "forget": _menu_forget_input,
        "mind_add": _typed(mind, "mind", "add"),
        "interests_add_forums": _typed(interests_command, "interests", "add forums"),
        "interests_add_guides": _typed(interests_command, "interests", "add guides"),
        "interests_add_ref": _typed(interests_command, "interests", "add ref"),
        "interests_run_id": _typed(interests_command, "interests", "run"),
        "study_forums": _typed(study_command, "study", "forums"),
        "study_guides": _typed(study_command, "study", "guides"),
        "study_ref": _typed(study_command, "study", "ref"),
        "read": _typed(read_command, "read"),
        "card": _typed_id(card_command, "card", research_ui.CARD_MISSING),
        "claude_connect": _menu_claude_connect_input,
        "tz": _typed(timezone_command, "tz"),
    }

    # The three tables above and app/tg/menu.ACTIONS must name exactly
    # the same actions, each in the table of its own kind, or a button
    # the hub renders could dispatch nowhere (or an entry here could sit
    # dead, dispatched by nothing render() ever draws). Checked at
    # import time -- every build_router() call -- so a drift fails the
    # first test that builds a router, not the first user who taps.
    _by_kind = {
        kind: {key for key, action in menu.ACTIONS.items() if action.kind == kind}
        for kind in menu.KINDS
    }
    assert set(RUN_ACTIONS) == _by_kind[menu.RUN], (
        "app/tg/router.py's RUN_ACTIONS and app/tg/menu.ACTIONS have drifted apart"
    )
    assert set(INPUT_ACTIONS) == _by_kind[menu.PROMPT], (
        "app/tg/router.py's INPUT_ACTIONS and app/tg/menu.ACTIONS have drifted apart"
    )
    assert set(CONFIRM_ACTIONS) == _by_kind[menu.CONFIRM] | menu.CONFIRM_AFTER_INPUT, (
        "app/tg/router.py's CONFIRM_ACTIONS and app/tg/menu.ACTIONS have drifted apart"
    )

    async def _menu_prompt_reply(message: Message, event_update: Update) -> bool:
        """The plain-text half of a /menu prompt: True iff this message
        was the reply a live prompt was waiting for, and was dispatched
        as its command instead of a chat turn.

        In order:

        1. No menu prompt pending: False, and nothing else is read.
        2. A pause word: it wins over the prompt, as it wins over every
           `awaiting` step (plan section 13). Only the prompt's message
           is retired here; turn.run's step 0c clears the step itself
           and handles the word exactly as it always does.
        3. Expired (MENU_PROMPT_TTL_MIN): the prompt is ended and the
           text is ordinary chat -- False.
        4. A Telegram-only prompt answered from the web chat (the step
           is one global row, and a web message must never feed a
           Telegram-only command): refused and consumed, so text meant
           for, say, an approval code is not handed to the model either
           -- but the prompt stays open, since its message is still on
           the Telegram screen and the next Telegram line answers it.
           Not available any more (a flag switched off since the tap):
           ended, answered and consumed alike.
        5. Otherwise: the action's INPUT_ACTIONS entry, with the text,
           through the command's own handler -- its `_once` gate, its
           audit rows, its is_web_sink guard. The update is then marked
           handled with a "[/<command>]" placeholder (a no-op when the
           handler already stored its reply), so the text is never
           stored as a chat message and never reaches the model, and
           only then is the step cleared and the prompt edited to
           «Ответ принят.» (neutral: the command may still have refused
           the text, and its own reply says so). That order is the
           replay story: a crash before the clear re-runs this branch
           on the replay, where the handler's own gate makes it a
           no-op; a replay after it finds no prompt and is stopped by
           `turn.answered_without_turn` in handle_text below.
        """
        async with sessionmaker() as session:
            prompt = await menu_prompt.current(session)
        if prompt is None:
            return False
        bot = message.bot
        chat_id = message.chat.id
        update_id = event_update.update_id
        text = (message.text or "").strip()

        if pause.match(text) is not None:
            await _retire_menu_message(bot, chat_id, prompt.message_id, menu.CANCELLED_TEXT)
            return False

        # The TTL is measured when the user sent the reply (Telegram's
        # `date`), not when a backlogged worker reached it; a web update
        # carries date 0, so it falls back to now.
        sent_at = message.date
        if sent_at is None or sent_at.timestamp() <= 0:
            sent_at = clock.now_utc()
        if not prompt.live(sent_at, settings.MENU_PROMPT_TTL_MIN) or not text:
            async with sessionmaker() as session:
                await menu_prompt.close_prompt(session, prompt.key)
            await _retire_menu_message(bot, chat_id, prompt.message_id, menu.CANCELLED_TEXT)
            return False

        web = getattr(bot, "is_web_sink", False)
        key = prompt.key
        if key not in INPUT_ACTIONS or not menu.action_available(key, settings, web=web):
            if (
                web
                and key in menu.TELEGRAM_ONLY
                and key in INPUT_ACTIONS
                and menu.action_available(key, settings, web=False)
            ):
                await _reply_once(message, update_id, WEB_ONLY_REPLY)
                return True
            async with sessionmaker() as session:
                await menu_prompt.close_prompt(session, key)
            await _retire_menu_message(bot, chat_id, prompt.message_id, menu.CANCELLED_TEXT)
            refusal = WEB_ONLY_REPLY if web and key in menu.TELEGRAM_ONLY else menu.PROMPT_UNAVAILABLE
            await _reply_once(message, update_id, refusal)
            return True

        logger.info(
            "menu prompt answered", extra={"event": "menu_prompt", "route": key}
        )
        await INPUT_ACTIONS[key](message, event_update, text)
        command = menu.ACTIONS[key].commands[0]
        await turn.mark_update_handled(
            sessionmaker,
            clock=clock,
            update_id=update_id,
            text=f"[/{command}]",
            scene_id=await turn.ensure_scene(sessionmaker, settings, clock),
        )
        async with sessionmaker() as session:
            await menu_prompt.close_prompt(session, key)
        await _retire_menu_message(bot, chat_id, prompt.message_id, menu.PROMPT_ANSWERED_TEXT)
        return True

    @router.callback_query(F.data.startswith("mn:"))
    async def menu_callback(callback: CallbackQuery, event_update: Update) -> None:
        """`mn:s:` / `mn:a:` / `mn:i:` / `mn:y:` / `mn:c` / `mn:x` -- the
        /menu hub (app/tg/menu.py's docstring has the formats).

        Registered ahead of the catch-all `unknown_callback` below.
        Order against every *other* callback_query handler in this
        router does not matter: no existing prefix is a prefix of
        "mn:", or the reverse (app/tg/menu.py's own docstring lists
        them), so aiogram's first-match-wins routing can never confuse
        this with one of them.

        Any tap here ends a pending menu prompt first (its message is
        edited to «Отменено.»): the user moved on, and the next message
        they type is chat again unless the tap was itself a new prompt.
        """
        if not isinstance(callback.message, Message):
            # InaccessibleMessage (or, in principle, None): Telegram
            # gives this back for a button under a message too old to
            # edit. Nothing to edit, nothing to dispatch.
            await answer_callback(callback.bot, callback.id, "Меню устарело — /menu.")
            return

        bot = callback.bot
        chat_id = callback.message.chat.id
        message_id = callback.message.message_id
        update_id = event_update.update_id
        web = getattr(bot, "is_web_sink", False)

        press = menu.parse_callback(callback.data)
        if press is None:
            await answer_callback(bot, callback.id, memory_ui.STALE)
            return

        # A tap that opens a prompt or sends a card is one message per
        # update: its replay must stop here, before ending or clearing
        # anything -- the step it would end is the very prompt the first
        # run opened.
        opens = press.kind == "input" or (
            press.kind == "action" and menu.action_kind(press.value) == menu.CONFIRM
        )
        if opens and not await _once(update_id):
            return

        await _end_menu_prompt(bot, chat_id)

        if press.kind == "close":
            await answer_callback(bot, callback.id)
            if web:
                await edit_keyboard(bot, chat_id, message_id, menu.CLOSED_TEXT, None)
            else:
                await _edit_menu(bot, chat_id, message_id, menu.closed_rich(), menu.CLOSED_TEXT, None)
            return

        if press.kind == "cancel":
            # ✕ Отмена under a prompt (already ended just above), or «Нет»
            # on a confirmation card: either way this message is over.
            # «Нет» claims the card like «Да» does, so a «Да» that was
            # already on its way cannot still run after the user said no.
            async with sessionmaker() as session:
                claimed = await menu_prompt.claim_card(session, message_id, update_id)
            if not claimed:
                await answer_callback(bot, callback.id, memory_ui.STALE)
                return
            await answer_callback(bot, callback.id)
            await _retire_menu_message(bot, chat_id, message_id, menu.CANCELLED_TEXT)
            return

        if press.kind == "section":
            shown = await _show_menu(
                bot, chat_id, press.value, message_id=message_id, origin=press.arg
            )
            if shown is None:
                await answer_callback(bot, callback.id, memory_ui.STALE)
                return
            await answer_callback(bot, callback.id)
            return

        key = press.value
        kind = menu.action_kind(key)
        # Re-checking action_available here (render() already only ever
        # draws a button that passes it) is what makes a forged
        # "mn:y:export"/"mn:a:delete"/"mn:i:claude_connect" from a
        # tampered web client harmless: each is Telegram only, so it
        # answers stale and dispatches nothing, exactly like a key this
        # build has never heard of. The kind has to match the prefix
        # too: a prompt key under mn:a: never runs without its text, and
        # mn:y: runs only what a confirmation card could have offered.
        if press.kind == "action":
            valid = kind in (menu.RUN, menu.CONFIRM)
        elif press.kind == "input":
            valid = kind == menu.PROMPT and press.arg is None
        else:  # "yes"
            needs_arg = key in menu.CONFIRM_AFTER_INPUT
            valid = menu.confirmable(key) and (press.arg is not None) == needs_arg
        if not valid or not menu.action_available(key, settings, web=web):
            await answer_callback(bot, callback.id, memory_ui.STALE)
            return
        if press.kind == "yes":
            # One-shot: the first «Да» claims the card, atomically, keyed
            # on the card's own message id (menu_prompt.claim_card). A
            # second tap sent before the first was processed is a second
            # update with its own update_id, which `_once` cannot see as
            # a repeat -- it would run the command again (two export
            # files; a second `/claude connect` counted as a wrong
            # code). Every press after the first is stale.
            async with sessionmaker() as session:
                claimed = await menu_prompt.claim_card(session, message_id, update_id)
            if not claimed:
                await answer_callback(bot, callback.id, memory_ui.STALE)
                return
        await answer_callback(bot, callback.id)
        async with sessionmaker() as session:
            await checkin_core.clear_awaiting(session)

        if press.kind == "input":
            # One prompt message per tap: a replayed tap must not ask
            # twice -- gated on `_once` above, before the prompt ended.
            prompt_id = await send_keyboard(bot, chat_id, menu.prompt_text(key), menu.prompt_markup())
            async with sessionmaker() as session:
                await menu_prompt.open_prompt(session, clock, key, prompt_id)
            await turn.mark_update_handled(
                sessionmaker, clock=clock, update_id=update_id, text=f"[menu:{key}]"
            )
            return

        if press.kind == "yes":
            # Retire the card first, so its «Да» cannot be pressed again
            # from the web chat (the hub's allowlist follows the edit),
            # then run the command with its own gates.
            await _retire_menu_message(bot, chat_id, message_id, menu.DONE_TEXT)
            await CONFIRM_ACTIONS[key](callback.message, event_update, press.arg)
            return

        if kind == menu.CONFIRM:
            # Replays already stopped at the `_once` gate above.
            await send_keyboard(bot, chat_id, menu.confirm_text(key), menu.confirm_markup(key))
            await turn.mark_update_handled(
                sessionmaker, clock=clock, update_id=update_id, text=f"[menu:{key}]"
            )
            return

        await RUN_ACTIONS[key](callback.message, event_update)
        # A switch leaves its own section on screen showing the new
        # state -- the same way /state's [🔄 Обновить] shows fresh
        # numbers -- instead of the last-shown value sitting there next
        # to a button that no longer means anything.
        refresh = menu.ACTIONS[key].refresh
        if refresh is not None:
            await _show_menu(bot, chat_id, refresh, message_id=message_id, origin=press.arg)

    @router.message(F.text)
    async def handle_text(message: Message, event_update: Update) -> None:
        if await _menu_prompt_reply(message, event_update):
            return
        # A replay of an update already answered as a prompt's reply
        # (or 5c's counter text) must not become a chat turn now that
        # its step is cleared -- see turn.answered_without_turn.
        if await turn.answered_without_turn(sessionmaker, event_update.update_id):
            return
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
            welfare_decider=welfare_decider,
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

    @router.callback_query(F.data.startswith("lr:"))
    async def lens_round_why(callback: CallbackQuery) -> None:
        """L2: `lr:w:<lens_round id>` -- a grounded review card's
        [почему эти заметки?] (app/tg/review.py)."""
        await review_ui.handle_why_callback(
            sessionmaker,
            callback.bot,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            data=callback.data,
        )

    @router.callback_query(F.data.startswith("lg:"))
    async def lens_garden_decision(callback: CallbackQuery) -> None:
        """L3: `lg:d:<gap id>:<epoch>` / `lg:n:<gap id>:<epoch>` -- the lens
        garden message's «N · сделал» / «N · не нужно» (app/tg/garden.py).
        L4: `lg:r:` «N · исследовать» (paid research, spent at the tap),
        and on a research's result message `lg:a:` «в Inbox» (a write
        into the user's vault) and `lg:x:` «не нужно». The settings
        decide whether research may run; the vault client is looked up
        per press, so a test can swap it.

        Refused from the web chat, like `v:`: the garden's and the
        result's messages are only ever sent to Telegram, so a press
        arriving through the web sink is not one the user made on that
        message (app/web/ingress.py's BLOCKED_CALLBACK_PREFIX is the
        first layer).
        """
        if getattr(callback.bot, "is_web_sink", False):
            await callback.bot.answer_callback_query(callback.id, text=WEB_ONLY_REPLY)
            return
        await garden_ui.handle_callback(
            sessionmaker,
            callback.bot,
            clock,
            settings=settings,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            data=callback.data,
            message_text=getattr(callback.message, "text", None),
            client_factory=VaultClient.from_settings,
        )

    @router.callback_query(F.data.startswith("gp:"))
    async def graph_prune_decision(callback: CallbackQuery) -> None:
        """`gp:a:<plan hash>` «применить» / `gp:u:<changeset>` «отменить»
        (app/tg/graph.py). Refused from the web chat, like `lg:`."""
        if getattr(callback.bot, "is_web_sink", False):
            await callback.bot.answer_callback_query(callback.id, text=WEB_ONLY_REPLY)
            return
        await graph_ui.handle_callback(
            settings,
            callback.bot,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            message_text=getattr(callback.message, "text", None),
            data=callback.data,
            client_factory=VaultClient.from_settings,
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
            research_on=settings.RESEARCH_ENABLED,
        )

    @router.callback_query(F.data.startswith("is:"))
    async def interest_suggestion(callback: CallbackQuery) -> None:
        """`is:<a|r|x>:<suggestion id>:<epoch>` -- a suggested topic's
        «N · добавить», «N · добавить и искать» and «N · ✖»
        (app/tg/interests.py, anchor-interest-suggestions-plan.md
        section 5, step 3).

        Refused from the web chat, like `lg:`: the suggestions message is
        only ever sent to Telegram, so a press arriving through the web
        sink is not one the user made on it (app/web/ingress.py's
        BLOCKED_CALLBACK_PREFIX is the first layer)."""
        if getattr(callback.bot, "is_web_sink", False):
            await callback.bot.answer_callback_query(callback.id, text=WEB_ONLY_REPLY)
            return
        await interests_ui.handle_suggestion_callback(
            sessionmaker,
            callback.bot,
            settings,
            clock,
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

    @router.callback_query(F.data.startswith("cw:"))
    async def claude_limits_callback(callback: CallbackQuery) -> None:
        """`cw:s:<key>:<value>` / `cw:r` / `cw:c` / `cw:i:<key>` -- `/claude
        limits`' own +/- keyboard. Telegram only, like `cl:`/`cu:`: the
        web app has its own card for the same caps
        (POST /api/state/claude-limits)."""
        if getattr(callback.bot, "is_web_sink", False):
            await callback.bot.answer_callback_query(callback.id, text=WEB_ONLY_REPLY)
            return
        await claude_ui.handle_limits_callback(
            sessionmaker,
            settings,
            callback.bot,
            clock,
            callback_id=callback.id,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            data=callback.data,
        )

    @router.callback_query(F.data.startswith("cu:"))
    async def claude_undo_all_callback(callback: CallbackQuery) -> None:
        """`cu:<date>:<epoch>` -- the Claude write digest's own
        [Откатить всё за сутки] (W2b). Refused from the web chat, like
        `v:`, `g:` and `cl:`: the digest that carries this button is
        only ever sent to Telegram.
        """
        if getattr(callback.bot, "is_web_sink", False):
            await callback.bot.answer_callback_query(callback.id, text=WEB_ONLY_REPLY)
            return
        await claude_ui.handle_undo_callback(
            sessionmaker,
            settings,
            callback.bot,
            clock,
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

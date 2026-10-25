"""Button menus for the Telegram interface.

Two things live here, both pure (no DB, no I/O -- app/tg/router.py owns
every side effect, exactly the split app/tg/memory.py's own docstring
draws between "Telegram-shaped" and "domain"):

1. `reply_keyboard()` -- the one-button persistent reply keyboard
   `/start` attaches. Telegram calls the two kinds of keyboard
   differently for a reason: a *reply* keyboard's buttons are plain
   text the user's client sends back as an ordinary message (so
   `F.text == MENU_BUTTON_TEXT` must be caught ahead of the persona-turn
   handler, in the router, or it becomes a chat line); an *inline*
   keyboard's buttons carry `callback_data` a bot answers directly and
   the user never sees as text. One reply-keyboard button rather than a
   full one for the same reason a full keyboard is not built here: every
   row it would offer is itself plain text that would otherwise hit the
   persona turn, and a persistent multi-row keyboard eats the screen of
   a chat app for a single-user companion that also wants to be talked
   to normally (docs/decisions.md has the fuller version of this).

2. `render()` / `render_rich()` / `action_available()` -- the menu hub
   `/menu` sends and the button handler edits in place. Both renderers
   read a single neutral `Section` spec (`_build_section`, below), a
   title, an optional hint paragraph, an optional status table and a
   list of button rows, so plain Telegram-API text/keyboard and Bot API
   10.1's rich blocks can never draw a different picture of the same
   section. `action_available` is the one gate both `_build_section`
   (which button to draw) and the router's callback handler (whether a
   press, a prompt's reply or a confirmation does anything) call, so the
   two can never disagree about which actions exist. That second call is
   what makes a forged `mn:y:export` from a tampered web client
   harmless: export is Telegram-only (`TELEGRAM_ONLY`, below), so on the
   web sink it is refused exactly like a key this build never heard of.

   `render()` returns the plain `(text, InlineKeyboardMarkup)` pair
   every version of `/menu` before Bot API 10.1 sent -- still the only
   shape the web sink understands (app/web/sink.py), and the fallback
   every real Telegram client gets if a rich message is ever rejected.
   `render_rich()` returns an `InputRichMessage` with the buttons laid
   out *in the message body* (`InputRichBlockButtons`/
   `RichMessageButton`), the same upgrade app/tg/state_view.py already
   gave `/state`.

The tree (the menu redesign, docs/decisions.md "Menu redesign": grouped
by task, every command reachable; a section whose feature is switched
off at deploy level is not drawn and not reachable):

    main            status table (bot, quiet, focus, intensity, main
                    action) + check-in, /state, and the sections below
      today         check-in, today's plan, the main action (set ✏️ /
                    clear ⚠️), focus, planner task ✏️ / event ✏️ / done
      deals         orders, a new order ✏️ (via order_cadence), debts,
                    the weekly review
        order_cadence   daily / weekdays / weekly › / once
          order_weekday Пн..Вс
      mem           memories, remember ✏️, pin ✏️ / unpin ✏️, forget
                    ✏️⚠️, Echo's notes and a new one ✏️, style
                    amendments, the background digest (24h, 7d)
      research      (RESEARCH_ENABLED) topics, a new topic ✏️ (via
                    packet_interest), run next / run № ✏️, suggest,
                    study ✏️ (via packet_study), read ✏️, cards, card № ✏️
        packet_interest / packet_study   forums / guides / ref, only the
                    packets that have sites
      knowledge     (VAULT_MODE != off) notes consent, vault status,
                    footer-link pruning and its undo ⚠️
        lens        (LENS_ENABLED, Telegram only) the lens status table,
                    garden now, research now, undo ⚠️, Claude Code's
                    door on ⚠️ / off
      links         planner ›, Claude ›, Grok ⚠️, close web sessions ⚠️,
                    close all access
        planner     (PLANNER_ENABLED) link status, plan, status, sync
                    on/off, link ⚠️
        claude      (CLAUDE_ACCESS_ENABLED, Telegram only) status and the
                    window picker, connect by code ✏️⚠️, the library
                    switches, write limits, undo ⚠️, disconnect ⚠️
      settings      intensity, pause, quiet ›, time zone ✏️, hide the
                    menu button
      quiet         presets, and "off" only while quiet is on --
                    reachable from main *and* from settings
      data          privacy, export ⚠️, delete ⚠️, close all access

Every section's last row is "‹ <parent>" plus "✕ Закрыть" (`_PARENT`,
`_BACK_LABELS`), so no section is a dead end. Quiet is the one section
with two parents; its callbacks carry where it was opened from
(`mn:s:quiet:settings`, `mn:a:quiet_2h:settings`), so its back button,
and the re-render after a preset, return to that same place.

Switches are *state-aware*: only the button that changes the current
state is drawn ("Включить фокус" while focus is off, never both), and
the router re-renders the section the press came from once the command
behind it has run (`Action.refresh`), so the card never shows a stale
value next to a button that no longer means anything.

**Three kinds of action** (`Action.kind`):

- `run` -- the button is the whole command: a fixed argument at most
  (`quiet_2h` -> `/quiet 2h`, `int_4` -> `/intensity 4`). Dispatched at
  once.
- `prompt` -- the command needs typed text. The button sends a prompt
  message («Что запомнить? Напиши одним сообщением.» [✕ Отмена]) and
  opens `user_state.awaiting = 'menu:<key>'` (app/core/menu_prompt.py);
  the next plain message runs the command with that text appended to the
  action's fixed prefix (`order_w3` -> `/order weekly:3 <text>`), is
  never stored as a chat message and never reaches the model. A prompt
  ends on its reply, on ✕ Отмена, on any slash command or menu tap, on a
  pause word, and after MENU_PROMPT_TTL_MIN -- after which the text is
  ordinary chat again.
- `confirm` -- the act is hard to take back. The button sends «Точно?»
  (`confirm_text`) with [Да, …] (`mn:y:<key>`) and [Нет] (`mn:c`); only
  «Да», after `action_available` is checked again, runs it. `forget` and
  `claude_connect` are prompts whose reply leads to the same card
  (`CONFIRM_AFTER_INPUT`), carrying the typed id or code as the
  confirmation's argument. `/delete` and `/grok` are `run` actions on
  purpose: each already has its own confirmation (the wipe card, the
  scope picker), and a third «Точно?» in front of it would only teach
  the user to tap through confirmations.

Callback data, prefix `mn:` (distinct from every other prefix already in
this router's callback_query table -- `m:k:`/`m:p:` (memory), `c:`
(check-in), `d:` (delete), `so:`/`ob:`/`am:`/`p:`/`pa:`/`pl:`/`r:`/`g:`/
`cl:`/`it:`/`is:`/`idle:`/`w:`/`nb:x:`/`st:`/`lr:`/`lg:`/`gp:`/`cw:`/
`cu:`/`v:`: aiogram matches with `F.data.startswith`, and none of those
is a prefix of `mn:` or vice versa, so registration order between this
module's handlers and theirs never matters):

    mn:s:<section>[:<from>]  show a section, editing the menu message in
                             place ("mn:s:main" is the hub itself);
                             `<from>` only for quiet (see above)
    mn:a:<key>[:<from>]      a `run` action -- or, for a `confirm` one,
                             send its «Точно?» card
    mn:i:<key>               a `prompt` action: ask for the text
    mn:y:<key>[:<arg>]       «Да» on a confirmation card. One-shot: the
                             first press claims the card (app/core/
                             menu_prompt.py's `claim_card`, keyed on the
                             card's message id) and runs the command;
                             every later press answers «Устарело.»
    mn:c                     ✕ Отмена on a prompt, «Нет» on a card: edit
                             that message to «Отменено.» and end the
                             prompt («Нет» claims the card too)
    mn:x                     close the menu (edit to a fixed "closed"
                             text, no keyboard)

Every one of these stays inside Telegram's 64-byte callback_data limit:
the longest fixed one, "mn:a:quiet_off:settings", is 23 bytes, and the
two carrying typed input bound it first (`forget_confirm_data` takes a
parsed integer id, `claude_connect_data` a short code-shaped token, both
checked by `fits_callback`).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from collections.abc import Callable
from zoneinfo import ZoneInfo

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichBlockButtons,
    InputRichBlockFooter,
    InputRichBlockParagraph,
    InputRichBlockSectionHeading,
    InputRichBlockTable,
    InputRichMessage,
    KeyboardButton,
    ReplyKeyboardMarkup,
    RichBlockTableCell,
    RichMessageButton,
)

from app.config import Settings
from app.core import orders as orders_core
from app.vault.kinds import SYNC_MODES
from app.web.ingress import BLOCKED_COMMANDS

MENU_BUTTON_TEXT = "☰ Меню"

SECTION_PREFIX = "mn:s:"
ACTION_PREFIX = "mn:a:"
INPUT_PREFIX = "mn:i:"
YES_PREFIX = "mn:y:"
CANCEL_CALLBACK = "mn:c"
CLOSE_CALLBACK = "mn:x"

# Telegram's own ceiling on callback_data, in bytes.
CALLBACK_DATA_MAX = 64

MAIN_SECTION = "main"

CLOSED_TEXT = "Меню закрыто. Открыть снова — /menu."

# What a prompt or a confirmation card is edited to once it is over, so
# neither lingers on screen with buttons that no longer mean anything.
DONE_TEXT = "Готово."
CANCELLED_TEXT = "Отменено."

# Shown after the main section's own buttons, in the plain view as a
# trailing line and in the rich view as an `InputRichBlockFooter` --
# the one place the hub reminds the reader that typing still works.
FOOTER_TEXT = "Писать можно и просто так."

# A prompt's reply that arrives where its action is not available
# (a flag switched off since the button was drawn).
PROMPT_UNAVAILABLE = "Это действие сейчас недоступно. Открой /menu заново."


def reply_keyboard() -> ReplyKeyboardMarkup:
    """The persistent `☰ Меню` button `/start` attaches.

    `is_persistent=True` keeps it showing without the user ever having
    to tap a "show keyboard" icon; `resize_keyboard=True` keeps the one
    row small rather than Telegram's default oversized button grid.
    `input_field_placeholder` is what shows in the empty text field --
    "Пиши как обычно" is there so the persistent button never reads as
    "this is now a menu-only bot", which is exactly the misimpression a
    permanent keyboard risks giving.
    """
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=MENU_BUTTON_TEXT)]],
        is_persistent=True,
        resize_keyboard=True,
        input_field_placeholder="Пиши как обычно",
    )


# --- the action table ---------------------------------------------------

RUN = "run"
PROMPT = "prompt"
CONFIRM = "confirm"
KINDS = (RUN, PROMPT, CONFIRM)


def _always(settings: Settings) -> bool:
    return True


def _planner(settings: Settings) -> bool:
    return settings.PLANNER_ENABLED


def _research(settings: Settings) -> bool:
    return settings.RESEARCH_ENABLED


def _vault(settings: Settings) -> bool:
    """Mirrors /vault itself (app/tg/vault.py's format_vault): "off" is
    the only mode with nothing to show, and "status"/"mirror"/"sync" all
    mean the feature is at least partly live."""
    return settings.VAULT_MODE != "off"


def _lens(settings: Settings) -> bool:
    return _vault(settings) and settings.LENS_ENABLED


def _lens_garden(settings: Settings) -> bool:
    """`/lens garden now`'s own switch row (app/core/idle/gate.py's
    `garden_enabled`): drawn only where the gate could say yes on its
    switches. Every other row -- the pause, the budget, once a week --
    is the gate's to answer, in the reply."""
    return (
        _lens(settings)
        and settings.LENS_GARDEN_ENABLED
        and settings.VAULT_KNOWLEDGE_ENABLED
        and settings.VAULT_MODE in SYNC_MODES
    )


def _lens_research(settings: Settings) -> bool:
    """`/lens research now`'s switch row (the gate's `lens_research_enabled`)."""
    return _lens(settings) and settings.RESEARCH_ENABLED and settings.LENS_GARDEN_ENABLED


def _graph(settings: Settings) -> bool:
    """app/tg/graph.py's own `enabled`."""
    return settings.GRAPH_PRUNE_ENABLED and _vault(settings)


def _claude(settings: Settings) -> bool:
    return settings.CLAUDE_ACCESS_ENABLED


def _grok(settings: Settings) -> bool:
    return settings.GROK_ACCESS_ENABLED


def _web_ui(settings: Settings) -> bool:
    return settings.WEB_UI_ENABLED


PACKETS = ("forums", "guides", "ref")


def packet_domains(settings: Settings, packet: str) -> tuple[str, ...]:
    """The sites behind a packet (app/research/jobs.py's `packet_domains`,
    duplicated rather than imported, like `_table` below: this module
    stays free of the research package)."""
    return {
        "forums": settings.PACKET_FORUMS,
        "guides": settings.PACKET_GUIDES,
        "ref": settings.PACKET_REF,
    }[packet]


def _packet(packet: str) -> Callable[[Settings], bool]:
    """A packet button exists only while research is on and the packet
    has sites: a packet with none would answer «Пакет … пока не
    настроен.» to every topic typed into it."""

    def gate(settings: Settings) -> bool:
        return settings.RESEARCH_ENABLED and bool(packet_domains(settings, packet))

    return gate


def _any_packet(settings: Settings) -> bool:
    return any(_packet(p)(settings) for p in PACKETS)


@dataclass(frozen=True)
class Action:
    """One leaf button.

    `kind` is `RUN`, `PROMPT` or `CONFIRM` (module docstring). `commands`
    names every slash command the action reaches, first the one it runs
    -- `card` also reaches `/adopt` and `/reject` through the buttons on
    its reply -- which is what `TELEGRAM_ONLY` reads and what a coverage
    check can hold against `BOT_COMMANDS`. `args` is the fixed argument
    (`run`/`confirm`) or the prefix the typed text is appended to
    (`prompt`), None for a bare command; the router's dispatch tables
    spell the same values out call by call, and this field documents
    them. `gate` is the deploy-level gate, a pure function of `Settings`
    (see `action_available` for why it never sees live state);
    `refresh` the section the router re-renders after a press; `style`
    Bot API 9.4's optional accent (see `_STYLES`' comment below).
    """

    label: str
    kind: str
    commands: tuple[str, ...]
    args: str | None = None
    gate: Callable[[Settings], bool] = field(default=_always)
    refresh: str | None = None
    style: str | None = None


def _run(label, *commands, args=None, gate=_always, refresh=None, style=None) -> Action:
    return Action(label, RUN, tuple(commands), args, gate, refresh, style)


def _prompt(label, *commands, args=None, gate=_always) -> Action:
    return Action(label, PROMPT, tuple(commands), args, gate)


def _confirm(label, *commands, args=None, gate=_always, style=None) -> Action:
    # No `refresh`: «Да» is pressed on the confirmation card, a message
    # of its own, so there is no menu card under it to re-render.
    return Action(label, CONFIRM, tuple(commands), args, gate, None, style)


_WEEKDAY_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

# Bot API 9.4's `style` field, used sparingly (the spec's own word): a
# check-in is the one affirmative, everyday action worth a green accent,
# closing access and wiping everything are destructive and worth a red
# one, and so is turning a knowledge switch off, whose "on" counterpart
# is the affirmative green. Every other button -- the everyday settings
# switches included, which are routine rather than affirmative or
# destructive -- stays the plain, unstyled default.
_SUCCESS = "success"
_DANGER = "danger"

# Every key here is a leaf button the hub can show, dispatched by the
# router (app/tg/router.py's RUN_ACTIONS / CONFIRM_ACTIONS /
# INPUT_ACTIONS) to the existing slash-command handler behind it, with a
# synthetic `CommandObject` where it takes arguments -- so every effect,
# audit row, `_once` replay gate and is_web_sink guard stays in that one
# handler (docs/decisions.md, "Button menus dispatch to the existing
# handlers").
#
# Since the menu redesign nothing is left out of this table for being
# sensitive: /export, /delete, /grok, /planner_link, `/lens code on`,
# `/claude connect` and `/weblogout` are buttons now, each behind a
# confirmation (or its own picker), and each **Telegram only**
# (`TELEGRAM_ONLY`, below) -- never drawn on the web sink and refused
# there even when forged, the same line app/web/ingress.py's
# BLOCKED_COMMANDS draws for the typed commands. Nothing is left out for
# taking typed text either: a `prompt` action collects it. What is still
# not here: `/start` and `/menu` (the entry points), and the inline
# buttons on the bot's own messages -- proposals, welfare, holds, the
# garden, suggestions, debts, /done's list, /claude limits' +/- keys --
# which stay contextual, on the message they act on.
ACTIONS: dict[str, Action] = {
    # main / today
    "checkin": _run("✅ Чек-ин", "checkin", style=_SUCCESS),
    "state": _run("📊 Состояние", "state"),
    "plan": _run("📋 План на сегодня", "plan", gate=_planner),
    "due_set": _prompt("✏️ Задать главное", "due"),
    # A bare /due does not "show the current one" -- it *clears* it,
    # closing the focus debt with it (app/core/commands.py's set_due),
    # so the button that does that asks first.
    "due_clear": _confirm("⚠️ Снять главное", "due"),
    "focus_on": _run("🎯 Включить фокус", "focus", args="on", refresh="today"),
    "focus_off": _run("🎯 Выключить фокус", "focus", args="off", refresh="today"),
    "task": _prompt("✏️ Задача в планер", "task", gate=_planner),
    "event": _prompt("✏️ Событие в планер", "event", gate=_planner),
    "done": _run("☑️ Отметить сделанной", "done", gate=_planner),
    # deals
    "orders": _run("📜 Договорённости", "orders"),
    "order_daily": _prompt("Каждый день", "order", args="daily"),
    "order_weekdays": _prompt("По будням", "order", args="weekdays"),
    "order_once": _prompt("Один раз", "order", args="once"),
    **{
        f"order_w{n}": _prompt(_WEEKDAY_SHORT[n - 1], "order", args=f"weekly:{n}")
        for n in range(1, 8)
    },
    "paid": _run("💸 Долги", "paid"),
    "review": _run("📈 Итоги недели", "review"),
    # mem
    "memories": _run("🗂 Что я помню", "memories"),
    "remember": _prompt("✏️ Запомнить", "remember"),
    "pin": _prompt("✏️ Закрепить №", "pin"),
    "unpin": _prompt("✏️ Открепить №", "unpin"),
    # /forget deletes with no confirmation of its own, so the menu's
    # version asks for the number, then shows the record (CONFIRM_AFTER_INPUT).
    "forget": _prompt("⚠️ Забыть запись №", "forget"),
    "mind": _run("📝 Заметки Echo", "mind"),
    "mind_add": _prompt("✏️ Новая заметка", "mind", args="add"),
    "amendments": _run("✍️ Поправки к стилю", "amendments"),
    "digest": _run("🌙 Фон за сутки", "digest"),
    "digest_7d": _run("🌙 За неделю", "digest", args="7d"),
    # research
    "interests": _run("🧭 Темы", "interests"),
    **{
        f"interests_add_{p}": _prompt(p, "interests", args=f"add {p}", gate=_packet(p))
        for p in PACKETS
    },
    "interests_run": _run("▶️ Искать следующую", "interests", args="run", gate=_research),
    "interests_run_id": _prompt("✏️ Искать тему №", "interests", args="run", gate=_research),
    # `/interests suggest`: Telegram only, like the command itself (its
    # is_web_sink guard): its message can carry lens text and its
    # buttons start paid searches.
    "interests_suggest": _run("💡 Предложить темы", "interests", args="suggest", gate=_research),
    **{f"study_{p}": _prompt(p, "study", args=p, gate=_packet(p)) for p in PACKETS},
    "read": _prompt("✏️ Прочитать страницу", "read", gate=_research),
    "notes": _run("🗃 Карточки", "notes", gate=_research),
    "card": _prompt("✏️ Карточка №", "card", "adopt", "reject", gate=_research),
    # knowledge
    "notes_on": _run(
        "📗 Включить заметки", "vault", args="notes on", gate=_vault, refresh="knowledge", style=_SUCCESS
    ),
    "notes_off": _run(
        "📗 Выключить заметки", "vault", args="notes off", gate=_vault, refresh="knowledge", style=_DANGER
    ),
    "vault": _run("ℹ️ Статус хранилища", "vault", gate=_vault),
    # The dry run; its own reply carries the `gp:a:` apply button.
    "graph": _run("🧹 Чистка ссылок", "graph", gate=_graph),
    "graph_undo": _confirm("⚠️ Отменить чистку", "graph", args="undo", gate=_graph),
    # lens
    "lens_garden": _run("🌱 Сад сейчас", "lens", args="garden now", gate=_lens_garden),
    "lens_research": _run("🔬 Исследовать сейчас", "lens", args="research now", gate=_lens_research),
    "lens_undo": _confirm("⚠️ Отменить последнее", "lens", args="undo", gate=_lens),
    "lens_code_on": _confirm("⚠️ Доступ Claude Code: вкл", "lens", args="code on", gate=_lens),
    "lens_code_off": _run(
        "Доступ Claude Code: выкл", "lens", args="code off", gate=_lens, refresh="lens"
    ),
    # links
    # Opens read access to everything the bot holds; its own scope
    # picker (app/tg/grok.py) is the confirmation -- nothing opens until
    # its [Разрешить].
    "grok_open": _run("⚠️ Доступ для Grok", "grok", gate=_grok),
    "weblogout": _confirm("⚠️ Закрыть веб-сессии", "weblogout", gate=_web_ui),
    "revoke": _run("⛔ Закрыть весь доступ", "revoke", style=_DANGER),
    # planner
    "planner": _run("ℹ️ Статус", "planner", gate=_planner),
    "planner_on": _run(
        "Синхронизация: включить", "planner", args="on", gate=_planner, refresh="planner"
    ),
    "planner_off": _run(
        "Синхронизация: выключить", "planner", args="off", gate=_planner, refresh="planner"
    ),
    "planner_link": _confirm("⚠️ Подключить планер", "planner_link", gate=_planner),
    # claude
    "claude": _run("🪟 Статус и окно", "claude", gate=_claude),
    # The code is typed, then confirmed (CONFIRM_AFTER_INPUT): approving
    # a connection is the one act /claude exists to guard.
    "claude_connect": _prompt("✏️ Подключить по коду", "claude", args="connect", gate=_claude),
    "lib_read_on": _run(
        "Библиотека: включить чтение", "claude", args="library on", gate=_claude,
        refresh="claude", style=_SUCCESS,
    ),
    "lib_read_off": _run(
        "Библиотека: выключить чтение", "claude", args="library off", gate=_claude,
        refresh="claude", style=_DANGER,
    ),
    "lib_write_on": _run(
        "Библиотека: разрешить запись", "claude", args="library write on", gate=_claude,
        refresh="claude", style=_SUCCESS,
    ),
    "lib_write_off": _run(
        "Библиотека: запретить запись", "claude", args="library write off", gate=_claude,
        refresh="claude", style=_DANGER,
    ),
    # The caps keep their own +/- keyboard (`cw:`), sent with the reply;
    # `limits <KEY> <N>` and `limits counters` live on it.
    "claude_limits": _run("📏 Лимиты записи", "claude", args="limits", gate=_claude),
    "claude_undo": _confirm("⚠️ Отменить последнюю правку", "claude", args="undo", gate=_claude),
    "claude_undo_all": _confirm(
        "⚠️ Отменить правки за сутки", "claude", args="undo all", gate=_claude
    ),
    "claude_disconnect": _confirm("⚠️ Отключить Claude", "claude", args="disconnect", gate=_claude),
    # settings
    **{
        f"int_{n}": _run(f"Интенсивность {n}", "intensity", args=str(n), refresh="settings")
        for n in range(1, 6)
    },
    "out": _run("⏸ Пауза", "out", refresh="settings"),
    "in": _run("▶️ Вернуться", "in", refresh="settings"),
    "tz": _prompt("✏️ Часовой пояс", "tz"),
    # The one action with no command behind it: no command ever needed
    # to remove the persistent reply keyboard. Meaningless through the
    # web sink, which never had one (app/web/sink.py only ever renders
    # InlineKeyboardMarkup).
    "hide_kb": _run("Убрать кнопку меню"),
    # quiet
    "quiet_30m": _run("30 мин", "quiet", args="30m", refresh="quiet"),
    "quiet_2h": _run("2 ч", "quiet", args="2h", refresh="quiet"),
    "quiet_8h": _run("8 ч", "quiet", args="8h", refresh="quiet"),
    "quiet_1d": _run("1 день", "quiet", args="1d", refresh="quiet"),
    "quiet_off": _run("🔔 Снять тишину", "quiet", args="off", refresh="quiet"),
    # data
    "privacy": _run("🛡 Приватность", "privacy"),
    "export": _confirm("⚠️ Выгрузить всё", "export"),
    # The wipe's own two-step card (app/tg/data.py) is its confirmation.
    "delete": _run("⚠️ Удалить всё", "delete", style=_DANGER),
}

# The prompts whose reply leads to a «Точно?» card rather than straight
# to the command: `forget` (show the record before it is gone) and
# `claude_connect` (show the code before it approves a connection).
# Their «Да» is `mn:y:<key>:<the typed id or code>`.
CONFIRM_AFTER_INPUT = frozenset({"forget", "claude_connect"})

# Telegram only: never drawn on the web sink, and refused there however
# the press arrives (`action_available`). Every key whose command
# app/web/ingress.py's BLOCKED_COMMANDS refuses to queue from the web --
# a menu button that reached one would be a route around that block --
# plus the acts that are Telegram's alone for their own reasons:
# `weblogout` (the kill switch for a stolen web session must not be in
# that session's hands), `interests_suggest` (its message can carry lens
# text and its buttons start paid searches; the command refuses the web
# itself), `hide_kb` (the web never had a reply keyboard) -- and, named
# again though BLOCKED_COMMANDS already covers them, `export`, `delete`,
# `grok_open`, `planner_link`, `lens_code_on` and `claude_connect`, so
# this list does not quietly change if that set ever does.
TELEGRAM_ONLY = frozenset(
    {key for key, action in ACTIONS.items() if set(action.commands) & BLOCKED_COMMANDS}
    | {
        "export",
        "delete",
        "grok_open",
        "weblogout",
        "planner_link",
        "lens_code_on",
        "claude_connect",
        "interests_suggest",
        "hide_kb",
    }
)


def action_available(action: str, settings: Settings, *, web: bool) -> bool:
    """Whether `action` is shown in the menu AND whether a press on it
    (a tap, a prompt's reply, a «Да») is honoured -- the single source of
    truth for both, so the section builders below and the router's
    handlers can never drift apart on what exists. A key not in ACTIONS
    at all -- forged or mistyped -- is always False here, and so is a
    Telegram-only key on the web sink, regardless of how the press got
    there.

    This is the *settings*-level gate only. Which of a switch's two
    buttons is drawn depends on live state (`MenuView`) this function
    never sees -- the router calls it with only `settings`/`web`, at
    press time, without a second round of queries -- so a stale press on
    the other one is handled by the command behind it, each of which is
    idempotent (`set_focus`, `set_notes_consent`, `set_enabled`, ...) or
    refuses on its own (`library_write` with reading off, `/planner on`
    never linked, `/planner_link` already linked).
    """
    spec = ACTIONS.get(action)
    if spec is None:
        return False
    if web and action in TELEGRAM_ONLY:
        return False
    return spec.gate(settings)


def action_kind(action: str) -> str | None:
    spec = ACTIONS.get(action)
    return spec.kind if spec else None


def confirmable(action: str) -> bool:
    """Whether «Да» (`mn:y:<action>`) may run `action`: a `confirm`
    action, or a prompt whose reply leads to a card."""
    return action_kind(action) == CONFIRM or action in CONFIRM_AFTER_INPUT


# --- prompt and confirmation texts --------------------------------------

_ONE_MESSAGE = "Напиши одним сообщением."

_ORDER_PROMPT = "Новая договорённость, {cadence}. Что именно? " + _ONE_MESSAGE
_INTEREST_PROMPT = "Новая тема для поиска в {packet}. Какая? " + _ONE_MESSAGE
_STUDY_PROMPT = "Что найти в {packet}? " + _ONE_MESSAGE

_PROMPTS = {
    "due_set": "Какое главное действие? " + _ONE_MESSAGE,
    "task": "Какую задачу добавить в планер? Например: купить хлеб завтра.",
    "event": "Какое событие добавить в планер? Например: врач в пятницу в 15:00.",
    "remember": "Что запомнить? " + _ONE_MESSAGE,
    "pin": "Какую запись закрепить? Напиши её номер, например 12.",
    "unpin": "Какую запись открепить? Напиши её номер.",
    "forget": "Какую запись забыть? Напиши её номер — перед удалением я её покажу.",
    "mind_add": "Что записать в заметки Echo? " + _ONE_MESSAGE,
    "interests_run_id": "Какую тему искать? Напиши её номер из списка тем.",
    "read": "Какую страницу прочитать? Пришли ссылку.",
    "card": "Какую карточку показать? Напиши её номер.",
    "claude_connect": "Пришли код со страницы подключения на claude.ai.",
    "tz": "Какой часовой пояс? Например: Europe/Paris.",
}

_ORDER_CADENCE_TEXT = {"daily": "каждый день", "weekdays": "по будням", "once": "один раз"}


def prompt_text(action: str) -> str:
    """What the prompt message asks. Every `prompt` key has one."""
    spec = ACTIONS[action]
    if action in _PROMPTS:
        return _PROMPTS[action]
    if spec.commands[0] == "order":
        token = spec.args or ""
        if token.startswith("weekly:"):
            cadence = orders_core.cadence_label(orders_core.WEEKLY, int(token.split(":", 1)[1]))
        else:
            cadence = _ORDER_CADENCE_TEXT[token]
        return _ORDER_PROMPT.format(cadence=cadence)
    if action.startswith("interests_add_"):
        return _INTEREST_PROMPT.format(packet=action[len("interests_add_"):])
    if action.startswith("study_"):
        return _STUDY_PROMPT.format(packet=action[len("study_"):])
    raise KeyError(action)


# (question, «Да» label) per `confirm` key.
_CONFIRMS = {
    "due_clear": ("Снять главное действие?", "Да, снять"),
    "graph_undo": ("Отменить последнюю чистку ссылок?", "Да, отменить"),
    "lens_undo": ("Отменить последнюю запись Echo в Inbox?", "Да, отменить"),
    "lens_code_on": (
        "Открыть Claude Code доступ к заметкам линзы? Прочитанное уйдёт в Anthropic.",
        "Да, открыть",
    ),
    "weblogout": ("Закрыть все веб-сессии?", "Да, закрыть"),
    "planner_link": ("Подключить планер? Пришлю ссылку для входа.", "Да, подключить"),
    "claude_undo": ("Откатить последнее изменение Claude в библиотеке?", "Да, откатить"),
    "claude_undo_all": (
        "Откатить все изменения Claude в библиотеке за последние 24 часа?",
        "Да, откатить",
    ),
    "claude_disconnect": (
        "Отключить Claude? Подключение, его токены и окна закроются.",
        "Да, отключить",
    ),
    "export": ("Выгрузить все данные одним файлом?", "Да, выгрузить"),
}

FORGET_CONFIRM = "Забыть #{id} «{text}»?"
FORGET_YES = "Да, забыть"
CLAUDE_CONNECT_CONFIRM = "Подключить Claude по коду {code}?"
CLAUDE_CONNECT_YES = "Да, подключить"
NO_LABEL = "Нет"
CANCEL_LABEL = "✕ Отмена"

# How much of a memory the «Забыть №» card quotes -- enough to recognise
# it, short enough for a confirmation.
_FORGET_PREVIEW_MAX = 120


def confirm_text(action: str) -> str:
    return _CONFIRMS[action][0]


def forget_confirm_text(memory_id: int, text: str) -> str:
    return FORGET_CONFIRM.format(id=memory_id, text=_preview(text, _FORGET_PREVIEW_MAX))


def claude_connect_confirm_text(code: str) -> str:
    return CLAUDE_CONNECT_CONFIRM.format(code=code)


def fits_callback(data: str) -> bool:
    return len(data.encode("utf-8")) <= CALLBACK_DATA_MAX


def yes_data(action: str, arg: str | None = None) -> str:
    return f"{YES_PREFIX}{action}" + (f":{arg}" if arg is not None else "")


def claude_connect_data(text: str) -> str | None:
    """The «Да» callback for a typed code, or None when the text cannot
    be one -- more than one word, a character outside letters, digits
    and "-", or too long to ride in callback data. The router then hands
    the text straight to `/claude connect`, whose own answer (unknown
    code, usage) is the right one: a confirmation card for something
    that is not even shaped like a code would only add a tap."""
    code = text.strip()
    if not code or len(code) > 16 or not all(ch.isascii() and (ch.isalnum() or ch == "-") for ch in code):
        return None
    data = yes_data("claude_connect", code)
    return data if fits_callback(data) else None


def prompt_markup() -> InlineKeyboardMarkup:
    """[✕ Отмена] under a prompt."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=CANCEL_LABEL, callback_data=CANCEL_CALLBACK)]]
    )


def confirm_markup(action: str, *, arg: str | None = None, yes_label: str | None = None) -> InlineKeyboardMarkup:
    """[Да, …][Нет] under a confirmation card."""
    label = yes_label or _CONFIRMS[action][1]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=label, callback_data=yes_data(action, arg)),
                InlineKeyboardButton(text=NO_LABEL, callback_data=CANCEL_CALLBACK),
            ]
        ]
    )


# --- the live state a section may show -----------------------------------


@dataclass(frozen=True)
class MenuView:
    """Everything a section shows that is not a `Settings` value --
    gathered by the router (app/tg/router.py's `_menu_view`) right
    before a render, never cached, and only what the section being shown
    needs. Defaults are a fresh install: bot active, nothing switched
    on, nothing connected, nothing counted -- so a test or a caller that
    has no session can omit it.

    `now` is the clock's UTC now, used only to tell a live quiet period
    from one that already ran out; with `now=None` quiet always reads as
    off. `library_read=None` means "no live Claude connection at all"
    (as opposed to a connection with the library switched off),
    matching `oauth_store.current_connection`'s own `None`.
    `planner_status` is `planner_credential.status` ("active" /
    "revoked"), or None if the planner was never linked.

    `settings_state`/`knowledge_roots` (8f) mirror
    `NotesOverview.settings`/`.knowledge_roots` (app/vault/status.py):
    the manifest's report on `Anchor/settings.md`, fetched -- like the
    counts app/tg/vault.py's own notes line shows -- only while notes
    consent is on. `settings_state=None` means "not fetched" (consent
    off, or the manifest did not answer), same shape as
    `format_notes_line`'s own `notes=None`; `knowledge_notes`/
    `personal_notes` are that same overview's counts, None alike.

    The menu redesign's counts, each None when not fetched: today's
    research use as `(study, manual, read)` (app/research/jobs.py's
    `used_today`), the lens's note count, whether Claude Code may log in
    (`lens.code_access`: None when the role is missing) and today's lens
    reads, the open Grok grants and the live web sessions. Counts and
    switches only -- never a title, a note or a token.
    """

    now: datetime.datetime | None = None
    timezone: str = "UTC"
    persona_active: bool = True
    focus_on: bool = False
    intensity: int = 3
    quiet_until: datetime.datetime | None = None
    due_action: str | None = None
    notes_consent: bool = False
    library_read: bool | None = None
    library_write: bool = False
    planner_status: str | None = None
    planner_enabled: bool = False
    settings_state: str | None = None
    knowledge_roots: tuple[str, ...] = ()
    knowledge_notes: int | None = None
    personal_notes: int | None = None
    research_used: tuple[int, int, int] | None = None
    lens_size: int | None = None
    lens_code: bool | None = None
    lens_today_reads: int | None = None
    grok_grants: int | None = None
    web_sessions: int | None = None


def _tz(view: MenuView) -> ZoneInfo:
    try:
        return ZoneInfo(view.timezone)
    except Exception:  # noqa: BLE001 - set_timezone validates; a bad row must not break the menu
        return ZoneInfo("UTC")


def quiet_active(view: MenuView) -> bool:
    return view.quiet_until is not None and view.now is not None and view.quiet_until > view.now


def _quiet_value(view: MenuView) -> str:
    if not quiet_active(view):
        return "нет"
    tz = _tz(view)
    local = view.quiet_until.astimezone(tz)
    today = view.now.astimezone(tz).date()
    return "до " + local.strftime("%H:%M" if local.date() == today else "%d.%m %H:%M")


def _on_off(value: bool) -> str:
    return "вкл" if value else "выкл"


def _hhmm(value: datetime.time) -> str:
    return value.strftime("%H:%M")


def _ru_plural(n: int, forms: tuple[str, str, str]) -> str:
    """app/tg/vault.py's `_ru_plural`, duplicated for the same reason as
    `_table` below."""
    if n % 100 in range(11, 15):
        return forms[2]
    last = n % 10
    if last == 1:
        return forms[0]
    if last in (2, 3, 4):
        return forms[1]
    return forms[2]


# The main action is the user's own text; the hub shows it as a reminder,
# not in full -- /state has room for the whole thing.
_DUE_PREVIEW_MAX = 40


def _preview(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _due_preview(text: str) -> str:
    return _preview(text, _DUE_PREVIEW_MAX)


# --- the section spec: one shape, two renderers --------------------------


@dataclass(frozen=True)
class Btn:
    """One button, renderer-agnostic. `style` is Bot API 9.4's optional
    accent (see `_SUCCESS`/`_DANGER` above); both `InlineKeyboardButton`
    and `RichMessageButton` accept it under the same name."""

    label: str
    callback_data: str
    style: str | None = None


@dataclass(frozen=True)
class Section:
    """The neutral spec both `render` and `render_rich` draw from.

    `table_rows`, when present, is a status table -- (label, value)
    pairs, plain strings only (no live rich-text entities). `rows` is
    the button grid, outer list = rows, inner list = the buttons in that
    row; the rich renderer turns each inner list into one
    `InputRichBlockButtons` block (Bot API 10.1: "a block containing a
    list of buttons that are shown in one row"), so this list-of-lists
    shape is not just an `InlineKeyboardMarkup` convenience carried over
    -- it is what the rich API itself expects. Every builder ends `rows`
    with `_nav_row`, so navigation is never a builder's afterthought.
    """

    title: str
    hint: str | None
    table_rows: list[tuple[str, str]] | None
    rows: list[list[Btn]]
    show_footer: bool = False


def _action_btn(action: str, label: str | None = None, *, origin: str | None = None) -> Btn:
    """The button for `action`, carrying the prefix its kind answers
    to: `mn:i:` asks for text, `mn:a:` runs (or, for a `confirm` key,
    sends the card). `origin` rides along only on quiet's buttons."""
    spec = ACTIONS[action]
    if spec.kind == PROMPT:
        data = f"{INPUT_PREFIX}{action}"
    else:
        data = f"{ACTION_PREFIX}{action}" + (f":{origin}" if origin else "")
    return Btn(label=label or spec.label, callback_data=data, style=spec.style)


def _section_btn(section: str, label: str, *, origin: str | None = None) -> Btn:
    return Btn(
        label=label, callback_data=f"{SECTION_PREFIX}{section}" + (f":{origin}" if origin else "")
    )


_CLOSE_BTN = Btn(label="✕ Закрыть", callback_data=CLOSE_CALLBACK)

# Where each section's "‹" goes. Quiet's entry is its default; opened
# from settings it goes back there instead (`QUIET_ORIGINS`).
_PARENT = {
    "today": MAIN_SECTION,
    "deals": MAIN_SECTION,
    "mem": MAIN_SECTION,
    "research": MAIN_SECTION,
    "knowledge": MAIN_SECTION,
    "links": MAIN_SECTION,
    "settings": MAIN_SECTION,
    "data": MAIN_SECTION,
    "quiet": MAIN_SECTION,
    "order_cadence": "deals",
    "order_weekday": "order_cadence",
    "packet_interest": "research",
    "packet_study": "research",
    "lens": "knowledge",
    "planner": "links",
    "claude": "links",
}

# The back button's word for each parent. A label that sometimes lies is
# worse than one more tap, so every back button names where it goes.
_BACK_LABELS = {
    MAIN_SECTION: "Меню",
    "deals": "Договорённости",
    "research": "Исследования",
    "knowledge": "Знания",
    "links": "Подключения",
    "settings": "Настройки",
    "order_cadence": "Как часто",
}

QUIET_ORIGINS = frozenset({MAIN_SECTION, "settings"})


def parent_of(section: str, origin: str | None = None) -> str | None:
    if section == "quiet" and origin in QUIET_ORIGINS:
        return origin
    return _PARENT.get(section)


def _nav_row(section: str, origin: str | None = None) -> list[Btn]:
    parent = parent_of(section, origin)
    back = Btn(label=f"‹ {_BACK_LABELS[parent]}", callback_data=f"{SECTION_PREFIX}{parent}")
    return [back, _CLOSE_BTN]


def _pairs(buttons: list[Btn]) -> list[list[Btn]]:
    return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]


def _row(settings: Settings, web: bool, *keys: str) -> list[Btn]:
    """The buttons for `keys` that pass `action_available`, in order --
    the one way a builder below draws an action, so none can draw one
    the gate refuses."""
    return [_action_btn(key) for key in keys if action_available(key, settings, web=web)]


def _add(rows: list[list[Btn]], row: list[Btn]) -> None:
    if row:
        rows.append(row)


MAIN_TITLE = "Меню"
TODAY_TITLE = "🎯 Сегодня"
DEALS_TITLE = "🤝 Договорённости и долги"
ORDER_CADENCE_TITLE = "Как часто?"
ORDER_WEEKDAY_TITLE = "В какой день?"
MEM_TITLE = "🧠 Память"
RESEARCH_TITLE = "🔎 Исследования"
PACKET_TITLE = "Какой пакет сайтов?"
KNOWLEDGE_TITLE = "📚 Хранилище и знания"
LENS_TITLE = "🔭 Линза"
LINKS_TITLE = "🔌 Подключения"
PLANNER_TITLE = "🗓 Планер"
CLAUDE_TITLE = "🤖 Claude"
SETTINGS_TITLE = "⚙️ Настройки"
QUIET_TITLE = "🔕 Тишина"
DATA_TITLE = "🔒 Данные"

QUIET_HINT = "Тишина отменяет запланированные сообщения и не даёт планировать новые."
PACKET_INTEREST_HINT = "Новая тема для фонового поиска."
PACKET_STUDY_HINT = "Поиск карточек по теме."

# Shown in a status table next to "нет" when there is no Claude
# connection at all -- nothing for a library switch to act on yet.
_LIB_NO_CONNECTION = "нет подключения"

# 8f: the knowledge-roots hint, shown under the knowledge section's
# status line while notes consent is on. Same wording app/tg/vault.py's
# own `format_settings_line` gives `/vault` -- duplicated, not imported,
# matching this module's own `_table` (state_view.py's twin): the two
# UI modules stay independently pure.
_ROOTS_LIST = "Корни знаний: {roots}"
_ROOTS_EMPTY = "Корни знаний: не заданы — добавь knowledge_folders в Anchor/settings.md"
_ROOTS_MISSING = "Anchor/settings.md не найден — корней знаний нет"
_ROOTS_WRONG_CASE = "Файл настроек называется не так: переименуй его в Anchor/settings.md (регистр важен)"
_ROOTS_INVALID = "Anchor/settings.md с ошибкой — ни одна заметка не читается"
_ROOTS_SHOWN = 5


def _settings_hint(state: str | None, roots: tuple[str, ...]) -> str | None:
    """None when there is nothing to say yet (notes consent off, or the
    manifest never answered)."""
    if state is None:
        return None
    if state == "invalid":
        return _ROOTS_INVALID
    if state == "missing":
        return _ROOTS_MISSING
    if state == "wrong_case":
        return _ROOTS_WRONG_CASE
    if not roots:
        return _ROOTS_EMPTY
    shown = roots[:_ROOTS_SHOWN]
    text = ", ".join(shown)
    if len(roots) > _ROOTS_SHOWN:
        text += f" и ещё {len(roots) - _ROOTS_SHOWN}"
    return _ROOTS_LIST.format(roots=text)


def _lib_value(read: bool | None, write: bool) -> str:
    if read is None:
        return _LIB_NO_CONNECTION
    if not read:
        return "выкл"
    return "чтение+запись" if write else "чтение"


def _planner_link_value(view: MenuView) -> str:
    if view.planner_status is None:
        return "не подключён"
    if view.planner_status == "active":
        return "подключён"
    return "нужно переподключить"


def _joined(parts: list[str]) -> str | None:
    return " · ".join(parts) or None


def _lines(*parts: str | None) -> str | None:
    text = "\n".join(part for part in parts if part)
    return text or None


# --- the builders ---------------------------------------------------------


def _render_main(settings: Settings, *, web: bool, view: MenuView, origin: str | None) -> Section:
    table_rows = [
        ("Бот", "в роли" if view.persona_active else "на паузе"),
        ("Тишина", _quiet_value(view)),
        ("Фокус", _on_off(view.focus_on)),
        ("Интенсивность", str(view.intensity)),
    ]
    if view.due_action:
        table_rows.append(("Главное", _due_preview(view.due_action)))

    rows = [[_action_btn("checkin"), _action_btn("state")]]
    entries = [
        ("today", "🎯 Сегодня ›"),
        ("deals", "🤝 Договорённости ›"),
        ("mem", "🧠 Память ›"),
        ("research", "🔎 Исследования ›"),
        ("knowledge", "📚 Знания ›"),
        ("links", "🔌 Подключения ›"),
        ("settings", "⚙️ Настройки ›"),
        ("data", "🔒 Данные ›"),
    ]
    buttons = [
        _section_btn(section, label)
        for section, label in entries
        if section_exists(section, settings, web=web)
    ]
    buttons += [_section_btn("quiet", "🔕 Тишина ›"), _CLOSE_BTN]
    rows += _pairs(buttons)
    return Section(title=MAIN_TITLE, hint=None, table_rows=table_rows, rows=rows, show_footer=True)


def _render_today(settings: Settings, *, web: bool, view: MenuView, origin: str | None) -> Section:
    due = _due_preview(view.due_action) if view.due_action else "не задано"
    hint = f"Главное: {due} · Фокус: {_on_off(view.focus_on)}"
    rows: list[list[Btn]] = []
    _add(rows, _row(settings, web, "checkin", "plan"))
    _add(rows, _row(settings, web, "due_set", *(("due_clear",) if view.due_action else ())))
    _add(rows, _row(settings, web, "focus_off" if view.focus_on else "focus_on"))
    _add(rows, _row(settings, web, "task", "event"))
    _add(rows, _row(settings, web, "done"))
    rows.append(_nav_row("today"))
    return Section(title=TODAY_TITLE, hint=hint, table_rows=None, rows=rows)


def _render_deals(settings: Settings, *, web: bool, view: MenuView, origin: str | None) -> Section:
    rows = [
        [_action_btn("orders"), _section_btn("order_cadence", "✏️ Новая договорённость ›")],
        _row(settings, web, "paid", "review"),
        _nav_row("deals"),
    ]
    return Section(title=DEALS_TITLE, hint=None, table_rows=None, rows=rows)


def _render_order_cadence(
    settings: Settings, *, web: bool, view: MenuView, origin: str | None
) -> Section:
    rows = [
        _row(settings, web, "order_daily", "order_weekdays"),
        [_section_btn("order_weekday", "Раз в неделю ›"), _action_btn("order_once")],
        _nav_row("order_cadence"),
    ]
    return Section(title=ORDER_CADENCE_TITLE, hint=None, table_rows=None, rows=rows)


def _render_order_weekday(
    settings: Settings, *, web: bool, view: MenuView, origin: str | None
) -> Section:
    rows = [
        _row(settings, web, *(f"order_w{n}" for n in range(1, 8))),
        _nav_row("order_weekday"),
    ]
    return Section(title=ORDER_WEEKDAY_TITLE, hint=None, table_rows=None, rows=rows)


def _render_mem(settings: Settings, *, web: bool, view: MenuView, origin: str | None) -> Section:
    rows: list[list[Btn]] = []
    for keys in (
        ("memories", "remember"),
        ("pin", "unpin"),
        ("forget",),
        ("mind", "mind_add"),
        ("amendments",),
        ("digest", "digest_7d"),
    ):
        _add(rows, _row(settings, web, *keys))
    rows.append(_nav_row("mem"))
    return Section(title=MEM_TITLE, hint=None, table_rows=None, rows=rows)


def _render_research(settings: Settings, *, web: bool, view: MenuView, origin: str | None) -> Section:
    hint = None
    if view.research_used is not None:
        study, manual, read = view.research_used
        hint = (
            f"Сегодня: /study {study}/{settings.RESEARCH_JOBS_PER_DAY} · "
            f"ручной поиск {manual}/{settings.RESEARCH_MANUAL_PER_DAY} · "
            f"/read {read}/{settings.RESEARCH_READS_PER_DAY}"
        )
    has_packets = section_exists("packet_interest", settings, web=web)
    rows: list[list[Btn]] = []
    first = _row(settings, web, "interests")
    if has_packets:
        first.append(_section_btn("packet_interest", "✏️ Новая тема ›"))
    _add(rows, first)
    _add(rows, _row(settings, web, "interests_run", "interests_run_id"))
    _add(rows, _row(settings, web, "interests_suggest"))
    study_row = [_section_btn("packet_study", "✏️ Найти по теме ›")] if has_packets else []
    _add(rows, study_row + _row(settings, web, "read"))
    _add(rows, _row(settings, web, "notes", "card"))
    rows.append(_nav_row("research"))
    return Section(title=RESEARCH_TITLE, hint=hint, table_rows=None, rows=rows)


def _render_packets(prefix: str, section: str, hint: str):
    def build(settings: Settings, *, web: bool, view: MenuView, origin: str | None) -> Section:
        rows: list[list[Btn]] = []
        _add(rows, _row(settings, web, *(f"{prefix}{p}" for p in PACKETS)))
        rows.append(_nav_row(section))
        return Section(title=PACKET_TITLE, hint=hint, table_rows=None, rows=rows)

    return build


def _knowledge_count(enabled: bool, count: int | None) -> str:
    if not enabled:
        return "выкл"
    return str(count) if count is not None else "вкл"


def _render_knowledge(
    settings: Settings, *, web: bool, view: MenuView, origin: str | None
) -> Section:
    """The vault's status line plus its switches. Claude's library
    switches moved to the Claude section (links › Claude), next to the
    connection they act on."""
    status = _joined(
        [
            f"Режим: {settings.VAULT_MODE}",
            f"Знания: {_knowledge_count(settings.VAULT_KNOWLEDGE_ENABLED, view.knowledge_notes)}",
            f"Личные: {_knowledge_count(settings.VAULT_PERSONAL_ENABLED, view.personal_notes)}",
            f"Заметки: {_on_off(view.notes_consent)}",
        ]
    )
    rows: list[list[Btn]] = []
    _add(rows, _row(settings, web, "notes_off" if view.notes_consent else "notes_on"))
    _add(rows, _row(settings, web, "vault"))
    if section_exists("lens", settings, web=web):
        rows.append([_section_btn("lens", "🔭 Линза ›")])
    _add(rows, _row(settings, web, "graph", "graph_undo"))
    rows.append(_nav_row("knowledge"))
    hint = _lines(status, _settings_hint(view.settings_state, view.knowledge_roots))
    return Section(title=KNOWLEDGE_TITLE, hint=hint, table_rows=None, rows=rows)


def _render_lens(settings: Settings, *, web: bool, view: MenuView, origin: str | None) -> Section:
    """The /lens status, as a table of counts and switches -- never a
    title or a note (app/tg/lens.py's own "Counts only")."""
    if view.lens_code is None:
        code_value = "роли нет"
    else:
        code_value = "открыт" if view.lens_code else "закрыт"
    table_rows = []
    if view.lens_size is not None:
        table_rows.append(("Заметок в линзе", str(view.lens_size)))
    table_rows.append(("Доступ Claude Code", code_value))
    if view.lens_today_reads is not None:
        table_rows.append(("Чтений сегодня", str(view.lens_today_reads)))
    rows: list[list[Btn]] = []
    _add(rows, _row(settings, web, "lens_garden", "lens_research"))
    _add(rows, _row(settings, web, "lens_undo"))
    _add(rows, _row(settings, web, "lens_code_off" if view.lens_code else "lens_code_on"))
    rows.append(_nav_row("lens"))
    return Section(title=LENS_TITLE, hint=None, table_rows=table_rows, rows=rows)


def _render_links(settings: Settings, *, web: bool, view: MenuView, origin: str | None) -> Section:
    parts = []
    if settings.PLANNER_ENABLED:
        parts.append(f"Планер: {_planner_link_value(view)}")
    if not web:
        # The Telegram-only features' status stays where their buttons are.
        if settings.CLAUDE_ACCESS_ENABLED:
            if view.library_read is None:
                claude = "нет"
            elif view.library_read:
                claude = _lib_value(view.library_read, view.library_write)
            else:
                claude = "подключён"
            parts.append(f"Claude: {claude}")
        if settings.GROK_ACCESS_ENABLED and view.grok_grants is not None:
            parts.append("Grok: " + ("открыт" if view.grok_grants else "нет"))
        if settings.WEB_UI_ENABLED and view.web_sessions is not None:
            n = view.web_sessions
            parts.append(f"Веб: {n} {_ru_plural(n, ('сессия', 'сессии', 'сессий'))}")
    subsections = [
        _section_btn(section, label)
        for section, label in (("planner", "🗓 Планер ›"), ("claude", "🤖 Claude ›"))
        if section_exists(section, settings, web=web)
    ]
    rows: list[list[Btn]] = []
    _add(rows, subsections)
    _add(rows, _row(settings, web, "grok_open"))
    _add(rows, _row(settings, web, "weblogout"))
    _add(rows, _row(settings, web, "revoke"))
    rows.append(_nav_row("links"))
    return Section(title=LINKS_TITLE, hint=_joined(parts), table_rows=None, rows=rows)


def _render_planner(settings: Settings, *, web: bool, view: MenuView, origin: str | None) -> Section:
    linked = view.planner_status is not None
    active = view.planner_status == "active"
    table_rows = [("Подключение", _planner_link_value(view))]
    if linked:
        table_rows.append(("Синхронизация", _on_off(view.planner_enabled)))
    rows: list[list[Btn]] = []
    _add(rows, _row(settings, web, "plan", "planner"))
    if linked:
        _add(rows, _row(settings, web, "planner_off" if view.planner_enabled else "planner_on"))
    if not active:
        _add(rows, _row(settings, web, "planner_link"))
    rows.append(_nav_row("planner"))
    return Section(title=PLANNER_TITLE, hint=None, table_rows=table_rows, rows=rows)


def _render_claude(settings: Settings, *, web: bool, view: MenuView, origin: str | None) -> Section:
    """Status table plus state-aware switches: the library's read switch
    and, once reading is on, its write switch -- writing needs reading
    (W2b), so the write button is simply not drawn while reading is off,
    and `library_write` refuses a stale press the same way it refuses a
    typed `/claude library write on`. Undo and disconnect need a live
    connection, so they are drawn only with one."""
    connected = view.library_read is not None
    table_rows = [
        ("Подключение", "есть" if connected else "нет"),
        ("Библиотека", _lib_value(view.library_read, view.library_write)),
    ]
    rows: list[list[Btn]] = []
    _add(rows, _row(settings, web, "claude"))
    _add(rows, _row(settings, web, "claude_connect"))
    if connected:
        _add(rows, _row(settings, web, "lib_read_off" if view.library_read else "lib_read_on"))
    if view.library_read:
        _add(rows, _row(settings, web, "lib_write_off" if view.library_write else "lib_write_on"))
    # The caps are a standing setting, not tied to a connection.
    _add(rows, _row(settings, web, "claude_limits"))
    if connected:
        _add(rows, _row(settings, web, "claude_undo", "claude_undo_all"))
        _add(rows, _row(settings, web, "claude_disconnect"))
    rows.append(_nav_row("claude"))
    return Section(title=CLAUDE_TITLE, hint=None, table_rows=table_rows, rows=rows)


def _intensity_row(view: MenuView) -> list[Btn]:
    """"Мягче → n-1" / "Строже → n+1": each button names the value it
    sets, and carries that value (`int_<n>`), so a stale or doubled
    press lands on the number the button showed instead of stepping
    twice. The end of the scale draws only the one way off it."""
    row = []
    if view.intensity > 1:
        low = view.intensity - 1
        row.append(_action_btn(f"int_{low}", f"🔽 Мягче → {low}"))
    if view.intensity < 5:
        high = view.intensity + 1
        row.append(_action_btn(f"int_{high}", f"🔼 Строже → {high}"))
    return row


def _render_settings(settings: Settings, *, web: bool, view: MenuView, origin: str | None) -> Section:
    hint = (
        f"Интенсивность: {view.intensity} · "
        f"Ночью тихо: {_hhmm(settings.QUIET_START)}–{_hhmm(settings.QUIET_END)} · "
        f"Утро/вечер: {_hhmm(settings.MORNING_TIME)}/{_hhmm(settings.EVENING_TIME)} · "
        f"Пояс: {view.timezone}"
    )
    rows: list[list[Btn]] = []
    _add(rows, _intensity_row(view))
    _add(rows, _row(settings, web, "out" if view.persona_active else "in"))
    rows.append([_section_btn("quiet", "🔕 Тишина ›", origin="settings"), _action_btn("tz")])
    _add(rows, _row(settings, web, "hide_kb"))
    rows.append(_nav_row("settings"))
    return Section(title=SETTINGS_TITLE, hint=hint, table_rows=None, rows=rows)


def _render_quiet(settings: Settings, *, web: bool, view: MenuView, origin: str | None) -> Section:
    # The origin rides on every button here, so a preset's re-render
    # keeps the right back button (module docstring).
    via = origin if origin in QUIET_ORIGINS and origin != MAIN_SECTION else None
    table_rows = [
        ("Сейчас", _quiet_value(view)),
        ("Ночью тихо", f"{_hhmm(settings.QUIET_START)}–{_hhmm(settings.QUIET_END)}"),
    ]
    rows = [
        [_action_btn("quiet_30m", origin=via), _action_btn("quiet_2h", origin=via)],
        [_action_btn("quiet_8h", origin=via), _action_btn("quiet_1d", origin=via)],
    ]
    if quiet_active(view):
        rows.append([_action_btn("quiet_off", origin=via)])
    rows.append(_nav_row("quiet", origin))
    return Section(title=QUIET_TITLE, hint=QUIET_HINT, table_rows=table_rows, rows=rows)


def _render_data(settings: Settings, *, web: bool, view: MenuView, origin: str | None) -> Section:
    rows: list[list[Btn]] = []
    for key in ("privacy", "export", "delete", "revoke"):
        _add(rows, _row(settings, web, key))
    rows.append(_nav_row("data"))
    return Section(title=DATA_TITLE, hint=None, table_rows=None, rows=rows)


_SECTION_BUILDERS = {
    MAIN_SECTION: _render_main,
    "today": _render_today,
    "deals": _render_deals,
    "order_cadence": _render_order_cadence,
    "order_weekday": _render_order_weekday,
    "mem": _render_mem,
    "research": _render_research,
    "packet_interest": _render_packets("interests_add_", "packet_interest", PACKET_INTEREST_HINT),
    "packet_study": _render_packets("study_", "packet_study", PACKET_STUDY_HINT),
    "knowledge": _render_knowledge,
    "lens": _render_lens,
    "links": _render_links,
    "planner": _render_planner,
    "claude": _render_claude,
    "settings": _render_settings,
    "quiet": _render_quiet,
    "data": _render_data,
}

SECTIONS = frozenset(_SECTION_BUILDERS)

# A section the web sink never shows: everything in it is Telegram only.
_TELEGRAM_ONLY_SECTIONS = frozenset({"lens", "claude"})

_SECTION_GATES: dict[str, Callable[[Settings], bool]] = {
    "research": _research,
    "packet_interest": _any_packet,
    "packet_study": _any_packet,
    "knowledge": _vault,
    "lens": _lens,
    "planner": _planner,
    "claude": _claude,
}


def section_exists(section: str, settings: Settings, *, web: bool = False) -> bool:
    """Whether `section` can be shown under these settings and on this
    sink: a name this build knows, whose feature is not switched off at
    deploy level, and -- on the web -- not one whose every button is
    Telegram only."""
    if section not in _SECTION_BUILDERS:
        return False
    if web and section in _TELEGRAM_ONLY_SECTIONS:
        return False
    gate = _SECTION_GATES.get(section)
    return gate is None or gate(settings)


def _build_section(
    section: str,
    settings: Settings,
    *,
    web: bool,
    view: MenuView | None,
    origin: str | None = None,
) -> Section | None:
    if not section_exists(section, settings, web=web):
        return None
    return _SECTION_BUILDERS[section](settings, web=web, view=view or MenuView(), origin=origin)


def _ikb(btn: Btn) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=btn.label, callback_data=btn.callback_data, style=btn.style)


def _rich_btn(btn: Btn) -> RichMessageButton:
    return RichMessageButton(text=btn.label, style=btn.style, callback_data=btn.callback_data)


def _cell(text: str, *, header: bool = False) -> RichBlockTableCell:
    return RichBlockTableCell(align="left", valign="middle", text=text, is_header=header or None)


def _table_row(label: str, value: str) -> list[RichBlockTableCell]:
    return [_cell(label, header=True), _cell(value)]


def _table(rows: list[tuple[str, str]]) -> InputRichBlockTable:
    """Same compact, striped shape state_view.py's own `_table` uses for
    /state's tables -- duplicated rather than imported, since the two
    modules stay independently pure and neither is the other's
    dependency."""
    return InputRichBlockTable(
        cells=[_table_row(label, value) for label, value in rows], is_striped=True, is_compact=True
    )


def _plain_text(spec: Section) -> str:
    lines = [spec.title]
    if spec.hint:
        lines.append(spec.hint)
    if spec.table_rows:
        lines.extend(f"{label}: {value}" for label, value in spec.table_rows)
    if spec.show_footer:
        lines.append(FOOTER_TEXT)
    return "\n".join(lines)


def render(
    section: str,
    settings: Settings,
    *,
    web: bool,
    view: MenuView | None = None,
    origin: str | None = None,
) -> tuple[str, InlineKeyboardMarkup] | None:
    """The text and keyboard for `section`, or None if it does not exist
    (unknown name, a feature section switched off at deploy level, or a
    Telegram-only section on the web -- see `section_exists`).

    Every action button this ever emits satisfies `action_available` for
    the same `settings`/`web` -- each section builder above draws its
    actions through `_row` (or `_action_btn` for a key that is
    unconditional by construction: quiet, intensity, tz, orders), rather
    than duplicating the condition, so the two cannot drift apart. Stays
    the plain Telegram-API shape every version before Bot API 10.1 sent:
    still what the web sink gets (app/web/sink.py never learned rich
    messages) and what a real Telegram client falls back to if
    `render_rich` is ever rejected.
    """
    spec = _build_section(section, settings, web=web, view=view, origin=origin)
    if spec is None:
        return None
    markup = InlineKeyboardMarkup(inline_keyboard=[[_ikb(btn) for btn in row] for row in spec.rows])
    return _plain_text(spec), markup


def render_rich(
    section: str,
    settings: Settings,
    *,
    web: bool,
    view: MenuView | None = None,
    origin: str | None = None,
) -> InputRichMessage | None:
    """The same section as `render`, laid out as Bot API 10.1 blocks
    with the buttons *inside* the message body (`InputRichBlockButtons`/
    `RichMessageButton`) rather than a separate `reply_markup` --
    matching how /state's own rich view (app/tg/state_view.py) already
    replaced its `InlineKeyboardMarkup`, except /state keeps one button
    outside the body (its refresh keyboard) because that button's job
    is to *edit* the message, not act on it.

    Built from the exact same `Section` spec `render` reads -- see that
    function's own docstring for why the two can never draw a different
    picture of a section.
    """
    spec = _build_section(section, settings, web=web, view=view, origin=origin)
    if spec is None:
        return None
    blocks: list = [InputRichBlockSectionHeading(text=spec.title, size=2)]
    if spec.hint:
        blocks.append(InputRichBlockParagraph(text=spec.hint))
    if spec.table_rows:
        blocks.append(_table(spec.table_rows))
    for row in spec.rows:
        blocks.append(InputRichBlockButtons(buttons=[_rich_btn(btn) for btn in row]))
    if spec.show_footer:
        blocks.append(InputRichBlockFooter(text=FOOTER_TEXT))
    return InputRichMessage(blocks=blocks)


def closed_rich() -> InputRichMessage:
    """The rich equivalent of `CLOSED_TEXT`, for editing a real Telegram
    client's menu message closed -- see `render_rich`'s own docstring
    for why buttons live in the body now: closing removes that body
    entirely rather than clearing a separate `reply_markup`."""
    return InputRichMessage(blocks=[InputRichBlockParagraph(text=CLOSED_TEXT)])


@dataclass(frozen=True)
class Press:
    """A parsed `mn:` callback. `kind` is "section", "action", "input",
    "yes", "cancel" or "close"; `value` the section or action key; `arg`
    the section's or action's origin (quiet), or a confirmation's
    argument (the id or code typed before it)."""

    kind: str
    value: str
    arg: str | None = None


def parse_callback(data: str) -> Press | None:
    """The `Press` for `data`, or None if it is not shaped like one of
    ours. The router only ever hands this function data already matched
    by `F.data.startswith("mn:")`, so None should not occur in practice
    -- it exists so a malformed press answers "stale" instead of raising.
    """
    if data == CLOSE_CALLBACK:
        return Press("close", "")
    if data == CANCEL_CALLBACK:
        return Press("cancel", "")
    for prefix, kind in (
        (SECTION_PREFIX, "section"),
        (ACTION_PREFIX, "action"),
        (INPUT_PREFIX, "input"),
        (YES_PREFIX, "yes"),
    ):
        if data.startswith(prefix):
            value, sep, arg = data[len(prefix):].partition(":")
            if not value:
                return None
            return Press(kind, value, arg if sep else None)
    return None

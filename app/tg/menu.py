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
   (which button to draw) and the router's `mn:a:` callback handler
   (whether a press does anything) call, so the two can never disagree
   about which actions exist. That second call is what makes a forged
   `mn:a:export` from a tampered web client harmless -- see this
   module's own docstring on `ACTIONS` for why `/export`, `/delete`,
   `/grok` and friends are not in it at all, forged or not.

   `render()` returns the plain `(text, InlineKeyboardMarkup)` pair
   every version of `/menu` before Bot API 10.1 sent -- still the only
   shape the web sink understands (app/web/sink.py), and the fallback
   every real Telegram client gets if a rich message is ever rejected.
   `render_rich()` returns an `InputRichMessage` with the buttons laid
   out *in the message body* (`InputRichBlockButtons`/
   `RichMessageButton`), the same upgrade app/tg/state_view.py already
   gave `/state`.

The tree (every section is one tap from main, and its last row is
always "‹ Меню" plus "✕ Закрыть", so no section is a dead end and the
way back always goes to the same place):

    main          status table (bot, quiet, focus, intensity, main
                  action) + check-in, /state, today's plan
      settings    status table + the switches: intensity, focus, pause
      quiet       presets, and "off" only while quiet is on
      mem         memory, notes, style amendments, research, background
      deals       orders, debts, weekly review
      vault       vault & knowledge: notes consent, Claude's library
      planner     link status + sync on/off
      data        privacy, Claude, revoke, the typed-only commands

Switches are *state-aware*: only the button that changes the current
state is drawn ("Включить фокус" while focus is off, never both), and
the router re-renders the section the press came from once the command
behind it has run (`REFRESH_SECTION`), so the card never shows a stale
value next to a button that no longer means anything.

Callback data, prefix `mn:` (distinct from every other prefix already in
this router's callback_query table -- `m:k:`/`m:p:` (memory), `c:`
(check-in), `d:` (delete), `so:`/`ob:`/`am:`/`p:`/`pa:`/`pl:`/`r:`/`g:`/
`cl:`/`it:`/`idle:`/`w:`/`nb:x:`/`st:`: aiogram matches with
`F.data.startswith`, and none of those is a prefix of `mn:` or vice
versa, so registration order between this module's handlers and theirs
never matters):

    mn:s:<section>   show a section, editing the menu message in place
                     ("mn:s:main" is the hub itself)
    mn:a:<action>    run an action
    mn:x             close the menu (edit to a fixed "closed" text, no
                     keyboard)

Every one of these stays far inside Telegram's 64-byte callback_data
limit -- the longest, "mn:a:lib_write_off", is 18 bytes.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
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

MENU_BUTTON_TEXT = "☰ Меню"

SECTION_PREFIX = "mn:s:"
ACTION_PREFIX = "mn:a:"
CLOSE_CALLBACK = "mn:x"

MAIN_SECTION = "main"

CLOSED_TEXT = "Меню закрыто. Открыть снова — /menu."

# Shown after the main section's own buttons, in the plain view as a
# trailing line and in the rich view as an `InputRichBlockFooter` --
# the one place the hub reminds the reader that typing still works.
FOOTER_TEXT = "Писать можно и просто так."


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
#
# Every key here is a leaf button the hub can show, dispatched by the
# router to the existing slash-command handler of the same name, or to
# one with a fixed argument (`quiet_*` -> /quiet, `focus_*` -> /focus,
# `int_<n>` -> /intensity <n>, `digest_7d` -> /digest 7d, `notes_*` ->
# /vault notes, `lib_read_*`/`lib_write_*` -> /claude library [write],
# `planner_*` -> /planner on|off). Deliberately never here, and so
# always refused by `action_available` below even if a forged
# `mn:a:<key>` arrives from a tampered web client:
#
#   export, delete   -- Telegram-only, blocked a second, independent way
#                        at app/web/ingress.py's BLOCKED_COMMANDS; a menu
#                        callback that dispatched them would be a second
#                        route around that block, defeating the reason
#                        it exists. The data section names them as typed
#                        commands instead.
#   grok             -- opens read access to everything the bot holds.
#                        That must stay a typed, deliberate act
#                        (app/tg/grok.py's own docstring), not a button a
#                        stray tap can reach from a menu two levels deep.
#   planner_link     -- sends a live OAuth authorize URL down the same
#                        chat; same Telegram-only reasoning as grok. The
#                        planner section names it instead.
#   due, remember,
#   forget, tz       -- every one of these takes a typed argument the
#                        menu has nowhere to collect, and `/due` with no
#                        argument does not "show the current one" -- it
#                        *clears* it (app/core/commands.py's `set_due`),
#                        closing the focus debt with it. A bare
#                        `mn:a:due` would silently wipe the main action
#                        the first time someone tapped it expecting to
#                        see it. The settings section shows the current
#                        values and the command to change each.
#   claude undo,
#   claude disconnect -- restoring vault files and ending a connection
#                        are rare, deliberate acts; /revoke already
#                        covers "close everything" from the data section.
#   anything else    -- every command that takes free text (/order,
#                        /task, /event, /read, /study, /mind add, /remember
#                        itself, ...) is out for the same "nowhere to type
#                        it" reason.
#
# The fixed-argument keys are the exception to "no typed argument": each
# is one value of a small closed set (on/off, a preset duration, a digit
# 1-5), not free text, so the button *is* the whole argument.
ACTIONS: dict[str, str] = {
    # main
    "checkin": "✅ Чек-ин",
    "state": "📊 Состояние",
    "plan": "📋 План на сегодня",
    # settings
    "int_1": "Интенсивность 1",
    "int_2": "Интенсивность 2",
    "int_3": "Интенсивность 3",
    "int_4": "Интенсивность 4",
    "int_5": "Интенсивность 5",
    "focus_on": "🎯 Включить фокус",
    "focus_off": "🎯 Выключить фокус",
    "out": "⏸ Пауза",
    "in": "▶️ Вернуться",
    # quiet
    "quiet_30m": "30 мин",
    "quiet_2h": "2 ч",
    "quiet_8h": "8 ч",
    "quiet_1d": "1 день",
    "quiet_off": "🔔 Снять тишину",
    # mem
    "memories": "Что я помню",
    "mind": "Заметки Echo",
    "amendments": "Поправки к стилю",
    "notes": "Карточки исследований",
    "interests": "Темы для поиска",
    "digest": "🌙 Фон за сутки",
    "digest_7d": "🌙 За неделю",
    # deals
    "orders": "Договорённости",
    "paid": "Долги",
    "review": "Итоги недели",
    # vault
    "vault": "Статус хранилища",
    "notes_on": "Заметки: включить",
    "notes_off": "Заметки: выключить",
    "lib_read_on": "Claude: открыть библиотеку",
    "lib_read_off": "Claude: закрыть библиотеку",
    "lib_write_on": "Claude: разрешить запись",
    "lib_write_off": "Claude: запретить запись",
    "claude_limits": "Claude: лимиты записи",
    # planner
    "planner": "Статус планера",
    "planner_on": "Синхронизация: включить",
    "planner_off": "Синхронизация: выключить",
    # data
    "privacy": "Приватность",
    "claude": "Claude",
    "revoke": "Закрыть доступ",
    "hide_kb": "Убрать кнопку меню",
}

# Bot API 9.4's `style` field, used sparingly (the spec's own word): a
# check-in is the one affirmative, everyday action worth a green accent,
# closing access or turning a knowledge switch off is destructive and
# worth a red one, and turning such a switch on is the affirmative
# counterpart worth green. Every other button -- the everyday settings
# switches included, which are routine rather than affirmative or
# destructive -- stays the plain, unstyled default.
_STYLES: dict[str, str] = {
    "checkin": "success",
    "revoke": "danger",
    "notes_on": "success",
    "notes_off": "danger",
    "lib_read_on": "success",
    "lib_read_off": "danger",
    "lib_write_on": "success",
    "lib_write_off": "danger",
}

# After which actions the router re-renders the menu card in place, and
# as which section: each switch lives in exactly one section, and a
# press should leave that section on screen showing the new state.
# Actions not listed (lists, reports, /state, ...) leave the card as it
# was -- their answer is a message of its own below it.
REFRESH_SECTION: dict[str, str] = {
    **{f"int_{n}": "settings" for n in range(1, 6)},
    "focus_on": "settings",
    "focus_off": "settings",
    "out": "settings",
    "in": "settings",
    **{key: "quiet" for key in ("quiet_30m", "quiet_2h", "quiet_8h", "quiet_1d", "quiet_off")},
    "notes_on": "vault",
    "notes_off": "vault",
    "lib_read_on": "vault",
    "lib_read_off": "vault",
    "lib_write_on": "vault",
    "lib_write_off": "vault",
    "planner_on": "planner",
    "planner_off": "planner",
}

_PLANNER_ACTIONS = frozenset({"plan", "planner", "planner_on", "planner_off"})
_VAULT_ACTIONS = frozenset({"vault", "notes_on", "notes_off"})
_CLAUDE_ACTIONS = frozenset(
    {"claude", "lib_read_on", "lib_read_off", "lib_write_on", "lib_write_off", "claude_limits"}
)


def _vault_section_visible(settings: Settings) -> bool:
    """Mirrors /vault itself (app/tg/vault.py's format_vault): "off" is
    the only mode with nothing to show, and "status"/"mirror"/"sync" all
    mean the feature is at least partly live. Shared by the main-menu
    button that opens the vault section, the section itself, and the
    `vault`/`notes_on`/`notes_off` actions that live inside it -- none of
    those can show while there is nothing behind them.
    """
    return settings.VAULT_MODE != "off"


def action_available(action: str, settings: Settings, *, web: bool) -> bool:
    """Whether `action` is shown in the menu AND whether a press on it
    is honoured -- the single source of truth for both, so the section
    builders below and the router's `mn:a:` handler can never drift
    apart on what exists. A key not in ACTIONS at all -- forged,
    mistyped, or one of the ones deliberately left out (see ACTIONS' own
    docstring) -- is always False here, regardless of `web`.

    This is the *settings*-level gate only. Which of a switch's two
    buttons is drawn depends on live state (`MenuView`) this function
    never sees -- the router calls it with only `settings`/`web` -- so
    a stale press on the other one is handled by the command behind it,
    each of which is idempotent (`set_focus`, `set_notes_consent`,
    `set_enabled`, ...) or refuses on its own (`library_write` with
    reading off, `/planner on` never linked).
    """
    if action not in ACTIONS:
        return False
    if action in _PLANNER_ACTIONS:
        return settings.PLANNER_ENABLED
    if action == "notes":
        return settings.RESEARCH_ENABLED
    if action in _VAULT_ACTIONS:
        return _vault_section_visible(settings)
    if action in _CLAUDE_ACTIONS:
        # Telegram-only, like /claude itself (app/tg/router.py's own
        # is_web_sink guard on claude_command): a stolen web session
        # must not be able to reach the code-approval flow, or the
        # library switches that flow gates.
        return settings.CLAUDE_ACCESS_ENABLED and not web
    if action == "hide_kb":
        # Removing a reply keyboard is meaningless through the web sink,
        # which never had one to begin with (app/web/sink.py only ever
        # renders InlineKeyboardMarkup).
        return not web
    return True


# --- the live state a section may show -----------------------------------


@dataclass(frozen=True)
class MenuView:
    """Everything a section shows that is not a `Settings` value --
    gathered by the router (app/tg/router.py's `_menu_view`) right
    before a render, never cached. Defaults are a fresh install: bot
    active, nothing switched on, nothing connected -- so a test or a
    caller that has no session can omit it.

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
    `format_notes_line`'s own `notes=None`.
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


# The main action is the user's own text; the hub shows it as a reminder,
# not in full -- /state has room for the whole thing.
_DUE_PREVIEW_MAX = 40


def _due_preview(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= _DUE_PREVIEW_MAX:
        return text
    return text[: _DUE_PREVIEW_MAX - 1].rstrip() + "…"


# --- the section spec: one shape, two renderers --------------------------


@dataclass(frozen=True)
class Btn:
    """One button, renderer-agnostic. `style` is Bot API 9.4's optional
    accent (see `_STYLES` above); both `InlineKeyboardButton` and
    `RichMessageButton` accept it under the same name."""

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


def _action_btn(action: str, label: str | None = None) -> Btn:
    return Btn(
        label=label or ACTIONS[action],
        callback_data=f"{ACTION_PREFIX}{action}",
        style=_STYLES.get(action),
    )


def _section_btn(section: str, label: str) -> Btn:
    return Btn(label=label, callback_data=f"{SECTION_PREFIX}{section}")


_CLOSE_BTN = Btn(label="✕ Закрыть", callback_data=CLOSE_CALLBACK)

# Every section hangs off main and is reachable from main alone -- a
# section reachable from two places would need a "back" that knows which
# one it came from, and a label that sometimes lies is worse than one
# more tap. So the way back is always the same button.
_BACK_BTN = Btn(label="‹ Меню", callback_data=f"{SECTION_PREFIX}{MAIN_SECTION}")


def _nav_row() -> list[Btn]:
    return [_BACK_BTN, _CLOSE_BTN]


MAIN_TITLE = "Меню"
SETTINGS_TITLE = "⚙️ Настройки"
QUIET_TITLE = "🔕 Тишина"
MEM_TITLE = "🧠 Память и заметки"
DEALS_TITLE = "🤝 Договорённости и долги"
VAULT_TITLE = "📚 Хранилище и знания"
PLANNER_TITLE = "🗓 Планер"
DATA_TITLE = "🔒 Данные и доступ"

SETTINGS_HINT = "Часовой пояс — /tz Europe/Paris, главное действие — /due и текст."
QUIET_HINT = "Тишина отменяет запланированные сообщения и не даёт планировать новые."
DATA_HINT = "Только командой: /export — выгрузить всё, /delete — удалить всё, /grok — доступ для Grok."
PLANNER_LINK_HINT = "Подключить планер — /planner_link."

# The hint shown in the vault status table next to "выкл" when there is
# no Claude connection at all -- nothing for a button to switch yet.
_LIB_NO_CONNECTION = "нет подключения"

# 8f: the knowledge-roots hint, shown right under the vault section's
# title while notes consent is on. Same wording app/tg/vault.py's own
# `format_settings_line` gives `/vault` -- duplicated, not imported,
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
    manifest never answered) -- same as the vault section simply having
    no hint before 8f."""
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


def _state_rows(view: MenuView) -> list[tuple[str, str]]:
    """The rows main and settings share, in the same order and words."""
    return [
        ("Бот", "активен" if view.persona_active else "на паузе"),
        ("Тишина", _quiet_value(view)),
        ("Фокус", _on_off(view.focus_on)),
        ("Интенсивность", f"{view.intensity}/5"),
    ]


def _render_main(settings: Settings, *, web: bool, view: MenuView) -> Section:
    table_rows = _state_rows(view)
    if view.due_action:
        table_rows.append(("Главное", _due_preview(view.due_action)))

    rows = [[_action_btn("checkin"), _action_btn("state")]]
    if action_available("plan", settings, web=web):
        rows.append([_action_btn("plan")])
    rows.append([_section_btn("settings", "⚙️ Настройки ›"), _section_btn("quiet", "🔕 Тишина ›")])
    rows.append([_section_btn("mem", "🧠 Память ›"), _section_btn("deals", "🤝 Договорённости ›")])
    connections = []
    if _vault_section_visible(settings):
        connections.append(_section_btn("vault", "📚 Хранилище ›"))
    if settings.PLANNER_ENABLED:
        connections.append(_section_btn("planner", "🗓 Планер ›"))
    if connections:
        rows.append(connections)
    rows.append([_section_btn("data", "🔒 Данные и доступ ›")])
    rows.append([_CLOSE_BTN])
    return Section(title=MAIN_TITLE, hint=None, table_rows=table_rows, rows=rows, show_footer=True)


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


def _render_settings(settings: Settings, *, web: bool, view: MenuView) -> Section:
    table_rows = _state_rows(view)
    table_rows += [
        ("Ночью тихо", f"{_hhmm(settings.QUIET_START)}–{_hhmm(settings.QUIET_END)}"),
        ("Утро / вечер", f"{_hhmm(settings.MORNING_TIME)} / {_hhmm(settings.EVENING_TIME)}"),
        ("Часовой пояс", view.timezone),
    ]
    rows = [
        _intensity_row(view),
        [_action_btn("focus_off" if view.focus_on else "focus_on")],
        [_action_btn("out" if view.persona_active else "in")],
        _nav_row(),
    ]
    return Section(title=SETTINGS_TITLE, hint=SETTINGS_HINT, table_rows=table_rows, rows=rows)


def _render_quiet(settings: Settings, *, web: bool, view: MenuView) -> Section:
    table_rows = [
        ("Сейчас", _quiet_value(view)),
        ("Ночью тихо", f"{_hhmm(settings.QUIET_START)}–{_hhmm(settings.QUIET_END)}"),
    ]
    rows = [
        [_action_btn("quiet_30m"), _action_btn("quiet_2h")],
        [_action_btn("quiet_8h"), _action_btn("quiet_1d")],
    ]
    if quiet_active(view):
        rows.append([_action_btn("quiet_off")])
    rows.append(_nav_row())
    return Section(title=QUIET_TITLE, hint=QUIET_HINT, table_rows=table_rows, rows=rows)


def _render_mem(settings: Settings, *, web: bool, view: MenuView) -> Section:
    rows = [[_action_btn("memories"), _action_btn("mind")], [_action_btn("amendments")]]
    if action_available("notes", settings, web=web):
        rows.append([_action_btn("notes"), _action_btn("interests")])
    else:
        rows.append([_action_btn("interests")])
    rows.append([_action_btn("digest"), _action_btn("digest_7d")])
    rows.append(_nav_row())
    return Section(title=MEM_TITLE, hint=None, table_rows=None, rows=rows)


def _render_deals(settings: Settings, *, web: bool, view: MenuView) -> Section:
    rows = [[_action_btn("orders"), _action_btn("paid")], [_action_btn("review")]]
    rows.append(_nav_row())
    return Section(title=DEALS_TITLE, hint=None, table_rows=None, rows=rows)


def _render_vault(settings: Settings, *, web: bool, view: MenuView) -> Section:
    """Status table plus state-aware switches: notes consent, and --
    Telegram only, with a live Claude connection -- the library's read
    switch and, once reading is on, its write switch. Writing needs
    reading (W2b), so the write button is simply not drawn while
    reading is off; `library_write` refuses a forged press the same way
    it refuses a typed `/claude library write on`.
    """
    table_rows = [
        ("Режим", settings.VAULT_MODE),
        ("Знания", _on_off(settings.VAULT_KNOWLEDGE_ENABLED)),
        ("Личные", _on_off(settings.VAULT_PERSONAL_ENABLED)),
        ("Заметки", _on_off(view.notes_consent)),
    ]
    rows: list[list[Btn]] = [[_action_btn("notes_off" if view.notes_consent else "notes_on")]]

    if action_available("lib_read_on", settings, web=web):
        table_rows.append(("Claude, библиотека", _lib_value(view.library_read, view.library_write)))
        if view.library_read is not None:
            rows.append([_action_btn("lib_read_off" if view.library_read else "lib_read_on")])
        if view.library_read:
            rows.append([_action_btn("lib_write_off" if view.library_write else "lib_write_on")])
        # The caps are a standing setting, not tied to a connection:
        # shown whenever the Claude switches are, answered by
        # `/claude limits` and its own +/- keyboard.
        rows.append([_action_btn("claude_limits")])

    rows.append([_action_btn("vault")])
    rows.append(_nav_row())
    hint = _settings_hint(view.settings_state, view.knowledge_roots)
    return Section(title=VAULT_TITLE, hint=hint, table_rows=table_rows, rows=rows)


def _render_planner(settings: Settings, *, web: bool, view: MenuView) -> Section:
    linked = view.planner_status is not None
    active = view.planner_status == "active"
    if not linked:
        link_value = "не подключён"
    elif active:
        link_value = "подключён"
    else:
        link_value = "нужно переподключить"
    table_rows = [("Подключение", link_value)]
    if linked:
        table_rows.append(("Синхронизация", _on_off(view.planner_enabled)))

    rows = [[_action_btn("plan")]]
    if linked:
        rows.append([_action_btn("planner_off" if view.planner_enabled else "planner_on")])
    rows.append([_action_btn("planner")])
    rows.append(_nav_row())
    hint = None if active else PLANNER_LINK_HINT
    return Section(title=PLANNER_TITLE, hint=hint, table_rows=table_rows, rows=rows)


def _render_data(settings: Settings, *, web: bool, view: MenuView) -> Section:
    rows = [[_action_btn("privacy")]]
    if action_available("claude", settings, web=web):
        rows.append([_action_btn("claude")])
    rows.append([_action_btn("revoke")])
    if action_available("hide_kb", settings, web=web):
        rows.append([_action_btn("hide_kb")])
    rows.append(_nav_row())
    return Section(title=DATA_TITLE, hint=DATA_HINT, table_rows=None, rows=rows)


_SECTION_BUILDERS = {
    MAIN_SECTION: _render_main,
    "settings": _render_settings,
    "quiet": _render_quiet,
    "mem": _render_mem,
    "deals": _render_deals,
    "vault": _render_vault,
    "planner": _render_planner,
    "data": _render_data,
}

SECTIONS = frozenset(_SECTION_BUILDERS)


def section_exists(section: str, settings: Settings) -> bool:
    """Whether `section` can be shown under these settings: a name this
    build knows, whose feature is not switched off at deploy level."""
    if section not in _SECTION_BUILDERS:
        return False
    if section == "vault":
        return _vault_section_visible(settings)
    if section == "planner":
        return settings.PLANNER_ENABLED
    return True


def _build_section(
    section: str, settings: Settings, *, web: bool, view: MenuView | None
) -> Section | None:
    if not section_exists(section, settings):
        return None
    return _SECTION_BUILDERS[section](settings, web=web, view=view or MenuView())


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
    section: str, settings: Settings, *, web: bool, view: MenuView | None = None
) -> tuple[str, InlineKeyboardMarkup] | None:
    """The text and keyboard for `section`, or None if it does not exist
    (unknown name, or a feature section switched off at deploy level --
    see `section_exists`).

    Every action button this ever emits satisfies `action_available` for
    the same `settings`/`web` -- each section builder above calls
    `action_available` (directly, or through ACTIONS' unconditional
    default) before appending a button, rather than duplicating the
    condition, so the two cannot drift apart. Stays the plain
    Telegram-API shape every version before Bot API 10.1 sent: still
    what the web sink gets (app/web/sink.py never learned rich
    messages) and what a real Telegram client falls back to if
    `render_rich` is ever rejected.
    """
    spec = _build_section(section, settings, web=web, view=view)
    if spec is None:
        return None
    markup = InlineKeyboardMarkup(inline_keyboard=[[_ikb(btn) for btn in row] for row in spec.rows])
    return _plain_text(spec), markup


def render_rich(
    section: str, settings: Settings, *, web: bool, view: MenuView | None = None
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
    spec = _build_section(section, settings, web=web, view=view)
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


def parse_callback(data: str) -> tuple[str, str] | None:
    """`("section", <name>)` / `("action", <name>)` / `("close", "")`,
    or None if `data` is not shaped like one of ours. The router only
    ever hands this function data already matched by
    `F.data.startswith("mn:")`, so None should not occur in practice --
    it exists so a malformed press answers "stale" instead of raising.
    """
    if data == CLOSE_CALLBACK:
        return "close", ""
    if data.startswith(SECTION_PREFIX):
        return "section", data[len(SECTION_PREFIX):]
    if data.startswith(ACTION_PREFIX):
        return "action", data[len(ACTION_PREFIX):]
    return None

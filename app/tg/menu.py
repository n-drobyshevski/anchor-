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

2. `render()` / `action_available()` -- the inline-keyboard hub `/menu`
   sends and the button handler edits in place. `render` draws the
   picture; `action_available` is the one gate both `render` (which
   button to draw) and the router's `mn:a:` callback handler (whether a
   press does anything) call, so the two can never disagree about which
   actions exist. That second call is what makes a forged `mn:a:export`
   from a tampered web client harmless -- see this module's own
   docstring on `ACTIONS` for why `/export`, `/delete`, `/grok` and
   friends are not in it at all, forged or not.

Callback data, prefix `mn:` (distinct from every other prefix already in
this router's callback_query table -- `m:k:`/`m:p:` (memory), `c:`
(check-in), `d:` (delete), `so:`/`ob:`/`am:`/`p:`/`pa:`/`pl:`/`r:`/`g:`/
`cl:`/`it:`/`idle:`/`w:`/`nb:x:`: aiogram matches with
`F.data.startswith`, and none of those is a prefix of `mn:` or vice
versa, so registration order between this module's handlers and theirs
never matters):

    mn:s:<section>   show a section, editing the menu message in place
                     ("mn:s:main" is the hub itself)
    mn:a:<action>    run an action
    mn:x             close the menu (edit to a fixed "closed" text, no
                     keyboard)

Every one of these stays far inside Telegram's 64-byte callback_data
limit -- the longest, "mn:a:quiet_30m", is 14 bytes.
"""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.config import Settings

MENU_BUTTON_TEXT = "☰ Меню"

SECTION_PREFIX = "mn:s:"
ACTION_PREFIX = "mn:a:"
CLOSE_CALLBACK = "mn:x"

MAIN_SECTION = "main"

CLOSED_TEXT = "Меню закрыто. Открыть снова — /menu."


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
# router to the existing slash-command handler of the same name (or, for
# the five `quiet_*`/two `focus_*`, to /quiet and /focus with a fixed
# argument). Deliberately never here, and so always refused by
# `action_available` below even if a forged `mn:a:<key>` arrives from a
# tampered web client:
#
#   export, delete   -- Telegram-only, blocked a second, independent way
#                        at app/web/ingress.py's BLOCKED_COMMANDS; a menu
#                        callback that dispatched them would be a second
#                        route around that block, defeating the reason
#                        it exists.
#   grok             -- opens read access to everything the bot holds.
#                        That must stay a typed, deliberate act
#                        (app/tg/grok.py's own docstring), not a button a
#                        stray tap can reach from a menu two levels deep.
#   planner_link     -- sends a live OAuth authorize URL down the same
#                        chat; same Telegram-only reasoning as grok.
#   due, remember,
#   forget, tz       -- every one of these takes a typed argument the
#                        menu has nowhere to collect, and `/due` with no
#                        argument does not "show the current one" -- it
#                        *clears* it (app/core/commands.py's `set_due`).
#                        A bare `mn:a:due` would silently wipe the main
#                        action the first time someone tapped it
#                        expecting to see it.
#   anything else    -- every command that takes free text (/order,
#                        /task, /event, /read, /study, /mind add, /remember
#                        itself, ...) is out for the same "nowhere to type
#                        it" reason.
ACTIONS: dict[str, str] = {
    "checkin": "✅ Чек-ин",
    "state": "📊 Состояние",
    "plan": "📋 План на сегодня",
    "memories": "Что я помню",
    "mind": "Заметки Anchor",
    "amendments": "Поправки к стилю",
    "notes": "Карточки исследований",
    "interests": "Темы для поиска",
    "orders": "Договорённости",
    "paid": "Долги",
    "review": "Итоги недели",
    "quiet_30m": "30 мин",
    "quiet_2h": "2 ч",
    "quiet_8h": "8 ч",
    "quiet_1d": "1 день",
    "quiet_off": "Снять тишину",
    "focus_on": "Фокус вкл",
    "focus_off": "Фокус выкл",
    "out": "⏸ Пауза",
    "in": "▶️ Вернуться",
    "digest": "Фоновая работа",
    "privacy": "Приватность",
    "vault": "Хранилище Obsidian",
    "claude": "Claude",
    "revoke": "Закрыть доступ",
    "hide_kb": "Убрать кнопку меню",
}

# Bot API 9.4's `style` field, used sparingly (the spec's own word): a
# check-in is the one affirmative, everyday action worth a green accent,
# and closing access is the one destructive action worth a red one.
# Every other button stays the plain, unstyled default.
_STYLES: dict[str, str] = {"checkin": "success", "revoke": "danger"}

# Actions whose visibility depends on a setting or on `web` -- every
# other key in ACTIONS is unconditionally available. Named here once so
# `action_available` can check the settings-dependent keys by name and
# fall through to True for the rest, rather than repeating the full key
# list in two places.
_CONDITIONAL_ACTIONS = frozenset({"plan", "notes", "vault", "claude", "hide_kb"})


def action_available(action: str, settings: Settings, *, web: bool) -> bool:
    """Whether `action` is shown in the menu AND whether a press on it
    is honoured -- the single source of truth for both, so `render`
    below and the router's `mn:a:` handler can never drift apart on
    what exists. A key not in ACTIONS at all -- forged, mistyped, or one
    of the ones deliberately left out (see ACTIONS' own docstring) --
    is always False here, regardless of `web`.
    """
    if action not in ACTIONS:
        return False
    if action not in _CONDITIONAL_ACTIONS:
        return True
    if action == "plan":
        return settings.PLANNER_ENABLED
    if action == "notes":
        return settings.RESEARCH_ENABLED
    if action == "vault":
        # Mirrors /vault itself (app/tg/vault.py's format_vault): "off"
        # is the only mode with nothing to show, and "status"/"mirror"/
        # "sync" all mean the feature is at least partly live.
        return settings.VAULT_MODE != "off"
    if action == "claude":
        # Telegram-only, like /claude itself (app/tg/router.py's own
        # is_web_sink guard on claude_command): a stolen web session
        # must not be able to reach the code-approval flow.
        return settings.CLAUDE_ACCESS_ENABLED and not web
    # action == "hide_kb": removing a reply keyboard is meaningless
    # through the web sink, which never had one to begin with (app/web/
    # sink.py only ever renders InlineKeyboardMarkup).
    return not web


def _action_button(action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=ACTIONS[action],
        callback_data=f"{ACTION_PREFIX}{action}",
        style=_STYLES.get(action),
    )


def _section_button(section: str, label: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, callback_data=f"{SECTION_PREFIX}{section}")


_CLOSE_BUTTON = InlineKeyboardButton(text="✕ Закрыть", callback_data=CLOSE_CALLBACK)
_BACK_BUTTON = InlineKeyboardButton(text="‹ Назад", callback_data=f"{SECTION_PREFIX}{MAIN_SECTION}")


def _back_row() -> list[InlineKeyboardButton]:
    return [_BACK_BUTTON]


MAIN_TEXT = "Меню. Писать можно и просто так."
MEM_TEXT = "Память и заметки."
DEALS_TEXT = "Договорённости и долги."
QUIET_TEXT = "Тишина — на сколько?"
MODE_TEXT = "Режим."
DATA_TEXT = "Данные и доступ."


def _render_main(settings: Settings, *, web: bool) -> tuple[str, InlineKeyboardMarkup]:
    rows = [[_action_button("checkin"), _action_button("state")]]
    if action_available("plan", settings, web=web):
        rows.append([_action_button("plan")])
    rows.append(
        [_section_button("mem", "🧠 Память ›"), _section_button("deals", "🤝 Договорённости ›")]
    )
    rows.append(
        [_section_button("quiet", "🔕 Тишина ›"), _section_button("mode", "⚙️ Режим ›")]
    )
    rows.append([_section_button("data", "🔒 Данные и доступ ›")])
    rows.append([_CLOSE_BUTTON])
    return MAIN_TEXT, InlineKeyboardMarkup(inline_keyboard=rows)


def _render_mem(settings: Settings, *, web: bool) -> tuple[str, InlineKeyboardMarkup]:
    rows = [[_action_button("memories")], [_action_button("mind")], [_action_button("amendments")]]
    if action_available("notes", settings, web=web):
        rows.append([_action_button("notes")])
    rows.append([_action_button("interests")])
    rows.append(_back_row())
    return MEM_TEXT, InlineKeyboardMarkup(inline_keyboard=rows)


def _render_deals(settings: Settings, *, web: bool) -> tuple[str, InlineKeyboardMarkup]:
    rows = [[_action_button("orders")], [_action_button("paid")], [_action_button("review")]]
    rows.append(_back_row())
    return DEALS_TEXT, InlineKeyboardMarkup(inline_keyboard=rows)


def _render_quiet(settings: Settings, *, web: bool) -> tuple[str, InlineKeyboardMarkup]:
    rows = [
        [_action_button("quiet_30m"), _action_button("quiet_2h")],
        [_action_button("quiet_8h"), _action_button("quiet_1d")],
        [_action_button("quiet_off")],
    ]
    rows.append(_back_row())
    return QUIET_TEXT, InlineKeyboardMarkup(inline_keyboard=rows)


def _render_mode(settings: Settings, *, web: bool) -> tuple[str, InlineKeyboardMarkup]:
    rows = [
        [_action_button("focus_on"), _action_button("focus_off")],
        [_action_button("out"), _action_button("in")],
        [_action_button("digest")],
    ]
    rows.append(_back_row())
    return MODE_TEXT, InlineKeyboardMarkup(inline_keyboard=rows)


def _render_data(settings: Settings, *, web: bool) -> tuple[str, InlineKeyboardMarkup]:
    rows = [[_action_button("privacy")]]
    if action_available("vault", settings, web=web):
        rows.append([_action_button("vault")])
    if action_available("claude", settings, web=web):
        rows.append([_action_button("claude")])
    rows.append([_action_button("revoke")])
    if action_available("hide_kb", settings, web=web):
        rows.append([_action_button("hide_kb")])
    rows.append(_back_row())
    return DATA_TEXT, InlineKeyboardMarkup(inline_keyboard=rows)


_SECTIONS = {
    MAIN_SECTION: _render_main,
    "mem": _render_mem,
    "deals": _render_deals,
    "quiet": _render_quiet,
    "mode": _render_mode,
    "data": _render_data,
}


def render(
    section: str, settings: Settings, *, web: bool
) -> tuple[str, InlineKeyboardMarkup] | None:
    """The text and keyboard for `section`, or None if it does not exist.

    Every action button this ever emits satisfies `action_available` for
    the same `settings`/`web` -- each section builder above calls
    `action_available` (directly, or through ACTIONS' unconditional
    default) before appending a button, rather than duplicating the
    condition, so the two cannot drift apart.
    """
    builder = _SECTIONS.get(section)
    if builder is None:
        return None
    return builder(settings, web=web)


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

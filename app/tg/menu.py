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

from dataclasses import dataclass

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
# router to the existing slash-command handler of the same name (or, for
# the five `quiet_*`/two `focus_*`/two `notes_*`/two `lib_write_*`, to
# /quiet, /focus, /vault or /claude with a fixed argument). Deliberately
# never here, and so always refused by `action_available` below even if
# a forged `mn:a:<key>` arrives from a tampered web client:
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
#
# `notes_on`/`notes_off` and `lib_write_on`/`lib_write_off` are the one
# exception to "no typed argument": each is a fixed on/off pair, not
# free text, so the button *is* the whole argument -- see the vault
# section builder below.
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
    "notes_on": "Заметки: включить",
    "notes_off": "Заметки: выключить",
    "lib_write_on": "Claude: разрешить запись",
    "lib_write_off": "Claude: запретить запись",
    "revoke": "Закрыть доступ",
    "hide_kb": "Убрать кнопку меню",
}

# Bot API 9.4's `style` field, used sparingly (the spec's own word): a
# check-in is the one affirmative, everyday action worth a green accent,
# turning notes off or closing access is destructive and worth a red
# one, and turning notes on or the write switch on is the affirmative
# counterpart worth green. Every other button stays the plain, unstyled
# default.
_STYLES: dict[str, str] = {
    "checkin": "success",
    "revoke": "danger",
    "notes_on": "success",
    "notes_off": "danger",
    "lib_write_on": "success",
    "lib_write_off": "danger",
}

# Actions whose visibility depends on a setting or on `web` -- every
# other key in ACTIONS is unconditionally available. Named here once so
# `action_available` can check the settings-dependent keys by name and
# fall through to True for the rest, rather than repeating the full key
# list in two places.
_CONDITIONAL_ACTIONS = frozenset(
    {
        "plan",
        "notes",
        "vault",
        "claude",
        "notes_on",
        "notes_off",
        "lib_write_on",
        "lib_write_off",
        "hide_kb",
    }
)


def _vault_section_visible(settings: Settings) -> bool:
    """Mirrors /vault itself (app/tg/vault.py's format_vault): "off" is
    the only mode with nothing to show, and "status"/"mirror"/"sync" all
    mean the feature is at least partly live. Shared by the old `vault`
    status action, the new vault *section* (both the main-menu button
    that opens it and the section itself), and the `notes_on`/
    `notes_off` toggles that live inside it -- none of those can show
    while there is nothing behind them.
    """
    return settings.VAULT_MODE != "off"


def action_available(action: str, settings: Settings, *, web: bool) -> bool:
    """Whether `action` is shown in the menu AND whether a press on it
    is honoured -- the single source of truth for both, so the section
    builders below and the router's `mn:a:` handler can never drift
    apart on what exists. A key not in ACTIONS at all -- forged,
    mistyped, or one of the ones deliberately left out (see ACTIONS' own
    docstring) -- is always False here, regardless of `web`.

    `lib_write_on`/`lib_write_off` stop here at the same two settings-
    only conditions `claude` itself does (CLAUDE_ACCESS_ENABLED, not
    web): whether `library_read` also happens to be on is per-connection
    state this function has no access to (the router calls it with only
    `settings`/`web`, never a session), so that finer condition lives in
    the vault section builder (which button gets *drawn*) and in
    app/tg/claude.py's `library_write` itself (which refuses a forged
    press the same way it refuses a typed `/claude library write on`
    with reading off) -- both layers agree, neither can be skipped.
    """
    if action not in ACTIONS:
        return False
    if action not in _CONDITIONAL_ACTIONS:
        return True
    if action == "plan":
        return settings.PLANNER_ENABLED
    if action == "notes":
        return settings.RESEARCH_ENABLED
    if action in ("vault", "notes_on", "notes_off"):
        return _vault_section_visible(settings)
    if action in ("claude", "lib_write_on", "lib_write_off"):
        # Telegram-only, like /claude itself (app/tg/router.py's own
        # is_web_sink guard on claude_command): a stolen web session
        # must not be able to reach the code-approval flow, or the
        # write switch that flow gates.
        return settings.CLAUDE_ACCESS_ENABLED and not web
    # action == "hide_kb": removing a reply keyboard is meaningless
    # through the web sink, which never had one to begin with (app/web/
    # sink.py only ever renders InlineKeyboardMarkup).
    return not web


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
class VaultMenuView:
    """The one piece of the vault section that needs a live session to
    know -- everything else in that section's status table
    (`VAULT_MODE`, `VAULT_KNOWLEDGE_ENABLED`, `VAULT_PERSONAL_ENABLED`)
    is a `Settings` value, already in hand wherever `render`/
    `render_rich` are called. Defaults are "nothing to show yet" --
    same as a fresh install with no connection -- so a caller rendering
    a section other than "vault" can simply omit it.

    `library_read`/`library_write` mirror `OauthConnection`'s own
    columns (app/web/oauth_store.py); `library_read=None` means "no
    live connection at all" (as opposed to a connection with the
    library switched off), matching `current_connection`'s own `None`
    return for "not connected".

    `settings_state`/`knowledge_roots` (8f) mirror
    `NotesOverview.settings`/`.knowledge_roots` (app/vault/status.py):
    the manifest's report on `Anchor/settings.md`, fetched -- like the
    counts app/tg/vault.py's own notes line shows -- only while notes
    consent is on. `settings_state=None` means "not fetched" (consent
    off, or the manifest did not answer), same shape as
    `format_notes_line`'s own `notes=None`.
    """

    notes_consent: bool = False
    library_read: bool | None = None
    library_write: bool = False
    settings_state: str | None = None
    knowledge_roots: tuple[str, ...] = ()


@dataclass(frozen=True)
class Section:
    """The neutral spec both `render` and `render_rich` draw from.

    `table_rows`, when present, is a status table -- (label, value)
    pairs, plain strings only (no live rich-text entities: the vault
    section is the only user, and every one of its values is already a
    fixed word by the time it gets here). `rows` is the button grid,
    outer list = rows, inner list = the buttons in that row; the rich
    renderer turns each inner list into one `InputRichBlockButtons`
    block (Bot API 10.1: "a block containing a list of buttons that are
    shown in one row"), so this list-of-lists shape is not just an
    `InlineKeyboardMarkup` convenience carried over -- it is what the
    rich API itself expects.
    """

    title: str
    hint: str | None
    table_rows: list[tuple[str, str]] | None
    rows: list[list[Btn]]
    show_footer: bool = False


def _action_btn(action: str) -> Btn:
    return Btn(label=ACTIONS[action], callback_data=f"{ACTION_PREFIX}{action}", style=_STYLES.get(action))


def _section_btn(section: str, label: str) -> Btn:
    return Btn(label=label, callback_data=f"{SECTION_PREFIX}{section}")


_CLOSE_BTN = Btn(label="✕ Закрыть", callback_data=CLOSE_CALLBACK)
_BACK_BTN = Btn(label="‹ Назад", callback_data=f"{SECTION_PREFIX}{MAIN_SECTION}")


def _back_row() -> list[Btn]:
    return [_BACK_BTN]


MAIN_TITLE = "Меню"
MEM_TEXT = "Память и заметки."
DEALS_TEXT = "Договорённости и долги."
QUIET_TEXT = "Тишина — на сколько?"
MODE_TEXT = "Режим."
DATA_TEXT = "Данные и доступ."
VAULT_TEXT = "📚 Хранилище и знания."

# The hint shown in the vault status table, in place of a drawn
# lib_write button, when CLAUDE_ACCESS_ENABLED and Telegram but reading
# is off -- W2b's own read-then-refuse order, spelled out instead of
# tapped: `/claude library write on` would refuse with exactly this
# same instruction (app/tg/claude.py's LIBRARY_WRITE_NEEDS_READ).
_LIB_WRITE_NEEDS_READ_HINT = " (/claude library on)"

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
    if not read:
        return "выкл"
    return "чтение+запись" if write else "чтение"


def _render_main(settings: Settings, *, web: bool, vault: VaultMenuView) -> Section:
    del vault  # main only decides whether to *show* the vault section, not its contents.
    rows = [[_action_btn("checkin"), _action_btn("state")]]
    if action_available("plan", settings, web=web):
        rows.append([_action_btn("plan")])
    rows.append(
        [_section_btn("mem", "🧠 Память ›"), _section_btn("deals", "🤝 Договорённости ›")]
    )
    rows.append(
        [_section_btn("quiet", "🔕 Тишина ›"), _section_btn("mode", "⚙️ Режим ›")]
    )
    if _vault_section_visible(settings):
        rows.append([_section_btn("vault", "📚 Хранилище и знания ›")])
    rows.append([_section_btn("data", "🔒 Данные и доступ ›")])
    rows.append([_CLOSE_BTN])
    return Section(title=MAIN_TITLE, hint=None, table_rows=None, rows=rows, show_footer=True)


def _render_mem(settings: Settings, *, web: bool, vault: VaultMenuView) -> Section:
    del vault
    rows = [[_action_btn("memories")], [_action_btn("mind")], [_action_btn("amendments")]]
    if action_available("notes", settings, web=web):
        rows.append([_action_btn("notes")])
    rows.append([_action_btn("interests")])
    rows.append(_back_row())
    return Section(title=MEM_TEXT, hint=None, table_rows=None, rows=rows)


def _render_deals(settings: Settings, *, web: bool, vault: VaultMenuView) -> Section:
    del settings, web, vault
    rows = [[_action_btn("orders")], [_action_btn("paid")], [_action_btn("review")]]
    rows.append(_back_row())
    return Section(title=DEALS_TEXT, hint=None, table_rows=None, rows=rows)


def _render_quiet(settings: Settings, *, web: bool, vault: VaultMenuView) -> Section:
    del settings, web, vault
    rows = [
        [_action_btn("quiet_30m"), _action_btn("quiet_2h")],
        [_action_btn("quiet_8h"), _action_btn("quiet_1d")],
        [_action_btn("quiet_off")],
    ]
    rows.append(_back_row())
    return Section(title=QUIET_TEXT, hint=None, table_rows=None, rows=rows)


def _render_mode(settings: Settings, *, web: bool, vault: VaultMenuView) -> Section:
    del settings, web, vault
    rows = [
        [_action_btn("focus_on"), _action_btn("focus_off")],
        [_action_btn("out"), _action_btn("in")],
        [_action_btn("digest")],
    ]
    rows.append(_back_row())
    return Section(title=MODE_TEXT, hint=None, table_rows=None, rows=rows)


def _render_data(settings: Settings, *, web: bool, vault: VaultMenuView) -> Section:
    del vault
    # The `vault` status action moved to the new vault section below --
    # this section keeps everything else "Данные и доступ" held before.
    rows = [[_action_btn("privacy")]]
    if action_available("claude", settings, web=web):
        rows.append([_action_btn("claude")])
    rows.append([_action_btn("revoke")])
    if action_available("hide_kb", settings, web=web):
        rows.append([_action_btn("hide_kb")])
    rows.append(_back_row())
    return Section(title=DATA_TEXT, hint=None, table_rows=None, rows=rows)


def _render_vault(settings: Settings, *, web: bool, vault: VaultMenuView) -> Section:
    """The status table plus state-dependent toggles (plan section 2):
    only the button matching the *current* state is drawn (never both
    "on" and "off" for the same switch), so a tap always reads as "do
    the thing", not "pick a side that might already be true". A stale
    press -- e.g. `notes_on` arriving after consent was already flipped
    on by a typed `/vault notes on` in between -- is still harmless: the
    underlying commands (`set_notes_consent`, `library_write`) are
    idempotent.
    """
    table_rows = [
        ("Режим", settings.VAULT_MODE),
        ("Знания", "вкл" if settings.VAULT_KNOWLEDGE_ENABLED else "выкл"),
        ("Личные", "вкл" if settings.VAULT_PERSONAL_ENABLED else "выкл"),
        ("Заметки", "вкл" if vault.notes_consent else "выкл"),
    ]
    rows: list[list[Btn]] = [[_action_btn("notes_off" if vault.notes_consent else "notes_on")]]

    claude_row_shown = settings.CLAUDE_ACCESS_ENABLED and not web
    if claude_row_shown:
        value = _lib_value(vault.library_read, vault.library_write)
        if not vault.library_read:
            value += _LIB_WRITE_NEEDS_READ_HINT
        table_rows.append(("Claude, библиотека", value))
        if vault.library_read:
            rows.append(
                [_action_btn("lib_write_off" if vault.library_write else "lib_write_on")]
            )

    rows.append([_action_btn("vault")])
    rows.append(_back_row())
    hint = _settings_hint(vault.settings_state, vault.knowledge_roots)
    return Section(title=VAULT_TEXT, hint=hint, table_rows=table_rows, rows=rows)


_SECTION_BUILDERS = {
    MAIN_SECTION: _render_main,
    "mem": _render_mem,
    "deals": _render_deals,
    "quiet": _render_quiet,
    "mode": _render_mode,
    "data": _render_data,
    "vault": _render_vault,
}


def _build_section(
    section: str, settings: Settings, *, web: bool, vault: VaultMenuView | None
) -> Section | None:
    if section == "vault" and not _vault_section_visible(settings):
        return None
    builder = _SECTION_BUILDERS.get(section)
    if builder is None:
        return None
    return builder(settings, web=web, vault=vault or VaultMenuView())


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
    section: str, settings: Settings, *, web: bool, vault: VaultMenuView | None = None
) -> tuple[str, InlineKeyboardMarkup] | None:
    """The text and keyboard for `section`, or None if it does not exist
    (unknown name, or -- "vault" only -- `VAULT_MODE=off`).

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
    spec = _build_section(section, settings, web=web, vault=vault)
    if spec is None:
        return None
    markup = InlineKeyboardMarkup(inline_keyboard=[[_ikb(btn) for btn in row] for row in spec.rows])
    return _plain_text(spec), markup


def render_rich(
    section: str, settings: Settings, *, web: bool, vault: VaultMenuView | None = None
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
    spec = _build_section(section, settings, web=web, vault=vault)
    if spec is None:
        return None
    # Section titles are sentences in the plain renderer ("Память и
    # заметки."); a heading reads wrong with the trailing period.
    blocks: list = [InputRichBlockSectionHeading(text=spec.title.rstrip("."), size=2)]
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

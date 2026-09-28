"""Button menus (the redesigned hub, 2026-09-28): `/menu`'s rich hub with
in-body buttons (Bot API 10.1), `/start`'s persistent `☰ Меню` button, the
`mn:` callbacks, and the new section tree (main, settings, quiet, mem,
deals, vault, planner, data -- "mode" is gone, and every non-main section
now hangs directly off main, "‹ Меню" being the only way back).

`app/tg/menu.py` is pure -- no DB, no I/O -- so most of it is tested
directly, the same way tests/test_quiet_tz.py tests app/core/quiet.py's
parser before ever touching the router. The router-level tests below
follow that file's own Dispatcher pattern (also tests/test_interests_
commands.py's `_callback_update`), and mirror tests/test_state_view.py's
own router group for the rich-message/fallback/web-sink coverage.
"""

from __future__ import annotations

import datetime
import itertools

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, SendRichMessage, TelegramMethod
from aiogram.types import InlineKeyboardMarkup, InputRichMessage, ReplyKeyboardMarkup, Update
from sqlalchemy import select

from app.config import Settings
from app.db.models import Message, OauthConnection, PlannerCredential, TelegramUpdate, UserState
from app.planner import auth as planner_auth
from app.tg import menu
from app.tg import planner as planner_ui
from app.tg.router import BOT_COMMANDS, HIDE_KB_REPLY, START_TEXT, build_router
from conftest import FakeLLMProvider, FakeSession, flatten_rich_message, make_bot, rich_callback_data

CHAT_ID = 555
TIMEZONE = "Europe/Paris"

# Every non-main section a fully-open deploy (planner linked, vault
# visible, research and Claude access on) can show.
ALL_SECTIONS = tuple(menu.SECTIONS)

# Never emitted, forged or not (menu.ACTIONS' own docstring gives the
# reasoning for each).
EXCLUDED_ACTIONS = (
    "export", "delete", "grok", "planner_link", "due", "remember", "forget", "tz",
)


def _flag_combinations():
    """Every combination of the settings that gate a menu entry or a
    whole section, plus web."""
    return itertools.product(
        (False, True),  # PLANNER_ENABLED
        (False, True),  # RESEARCH_ENABLED
        ("off", "status", "mirror", "sync"),  # VAULT_MODE
        (False, True),  # CLAUDE_ACCESS_ENABLED
        (False, True),  # web
    )


def _settings(planner: bool, research: bool, vault_mode: str, claude: bool) -> Settings:
    return Settings(
        _env_file=None,
        PLANNER_ENABLED=planner,
        RESEARCH_ENABLED=research,
        VAULT_MODE=vault_mode,
        VAULT_API_TOKEN="x" * 32,
        CLAUDE_ACCESS_ENABLED=claude,
    )


def _view_samples() -> tuple[menu.MenuView, ...]:
    """A representative sample of MenuView permutations across every
    state-aware switch -- not the full cross product, which would
    explode combinatorially for no extra coverage: `action_available`
    never reads the view (only `settings`/`web`), so the view only ever
    changes *which* of a pair of buttons is drawn, and each such pair is
    already pinned down by its own dedicated test below."""
    now = datetime.datetime(2026, 9, 28, 10, 0, tzinfo=datetime.timezone.utc)
    return (
        menu.MenuView(),
        menu.MenuView(
            now=now, timezone=TIMEZONE, persona_active=False, focus_on=True, intensity=1,
            quiet_until=now + datetime.timedelta(hours=1), due_action="x" * 60,
            notes_consent=True, library_read=True, library_write=True,
            planner_status="active", planner_enabled=True,
        ),
        menu.MenuView(intensity=5, library_read=False, planner_status="revoked", planner_enabled=False),
        menu.MenuView(library_read=None),
    )


def _buttons(markup: InlineKeyboardMarkup) -> list:
    return [button for row in markup.inline_keyboard for button in row]


def _action_buttons(markup: InlineKeyboardMarkup) -> list:
    return [b for b in _buttons(markup) if b.callback_data.startswith(menu.ACTION_PREFIX)]


def _rich_action_data(rich: InputRichMessage) -> list[str]:
    return [d for d in rich_callback_data(rich) if d.startswith(menu.ACTION_PREFIX)]


# --- pure: render / render_rich / action_available -----------------------


def test_every_section_renders_both_ways():
    settings = _settings(True, True, "status", True)
    for section in ALL_SECTIONS:
        for web in (False, True):
            rendered = menu.render(section, settings, web=web)
            assert rendered is not None
            text, markup = rendered
            assert text
            assert isinstance(markup, InlineKeyboardMarkup)
            assert markup.inline_keyboard

            rich = menu.render_rich(section, settings, web=web)
            assert rich is not None
            assert isinstance(rich, InputRichMessage)
            assert rich.blocks


def test_unknown_section_is_none_both_ways():
    settings = Settings(_env_file=None)
    assert menu.render("nope", settings, web=False) is None
    assert menu.render("", settings, web=True) is None
    assert menu.render_rich("nope", settings, web=False) is None
    assert menu.render_rich("", settings, web=True) is None


def test_mode_section_is_gone():
    assert "mode" not in menu.SECTIONS
    assert set(menu.SECTIONS) == {
        "main", "settings", "quiet", "mem", "deals", "vault", "planner", "data",
    }


def test_section_exists_gates_vault_and_planner():
    off = _settings(False, False, "off", False)
    on = _settings(True, False, "status", False)
    assert menu.section_exists("vault", off) is False
    assert menu.section_exists("vault", on) is True
    assert menu.section_exists("planner", off) is False
    assert menu.section_exists("planner", on) is True
    assert menu.section_exists("settings", off) is True
    assert menu.section_exists("nope", on) is False


@pytest.mark.parametrize(
    "section,title",
    [
        ("main", menu.MAIN_TITLE),
        ("settings", menu.SETTINGS_TITLE),
        ("quiet", menu.QUIET_TITLE),
        ("mem", menu.MEM_TITLE),
        ("deals", menu.DEALS_TITLE),
        ("vault", menu.VAULT_TITLE),
        ("planner", menu.PLANNER_TITLE),
        ("data", menu.DATA_TITLE),
    ],
)
def test_section_titles_have_no_trailing_period_and_the_rich_heading_matches_verbatim(section, title):
    assert not title.endswith(".")
    settings = _settings(True, True, "status", True)
    text, _ = menu.render(section, settings, web=False)
    assert text.startswith(title)
    rich = menu.render_rich(section, settings, web=False)
    assert rich.blocks[0].text == title


def test_every_callback_data_is_well_formed_both_ways():
    for planner, research, vault_mode, claude, web in _flag_combinations():
        settings = _settings(planner, research, vault_mode, claude)
        for view in _view_samples():
            for section in ALL_SECTIONS:
                plain = menu.render(section, settings, web=web, view=view)
                rich = menu.render_rich(section, settings, web=web, view=view)
                assert (plain is None) == (rich is None)
                if plain is None:
                    continue
                _, markup = plain
                for button in _buttons(markup):
                    data = button.callback_data
                    assert data.startswith("mn:")
                    assert len(data.encode("utf-8")) <= 64
                for data in rich_callback_data(rich):
                    assert data.startswith("mn:")
                    assert len(data.encode("utf-8")) <= 64
    # The close button too, and the longest single action key.
    assert menu.CLOSE_CALLBACK.startswith("mn:")
    assert len(f"{menu.ACTION_PREFIX}lib_write_off".encode("utf-8")) <= 64


def test_plan_hidden_when_planner_off_shown_when_on():
    off = _settings(False, False, "off", False)
    on = _settings(True, False, "off", False)
    _, main_off = menu.render("main", off, web=False)
    _, main_on = menu.render("main", on, web=False)
    assert f"{menu.ACTION_PREFIX}plan" not in [b.callback_data for b in _action_buttons(main_off)]
    assert f"{menu.ACTION_PREFIX}plan" in [b.callback_data for b in _action_buttons(main_on)]


def test_notes_hidden_when_research_off_shown_when_on():
    off = _settings(False, False, "off", False)
    on = _settings(False, True, "off", False)
    _, mem_off = menu.render("mem", off, web=False)
    _, mem_on = menu.render("mem", on, web=False)
    assert f"{menu.ACTION_PREFIX}notes" not in [b.callback_data for b in _action_buttons(mem_off)]
    assert f"{menu.ACTION_PREFIX}notes" in [b.callback_data for b in _action_buttons(mem_on)]


def test_vault_section_hidden_on_main_and_unreachable_when_mode_off():
    off = _settings(False, False, "off", False)
    on = _settings(False, False, "status", False)
    _, main_off = menu.render("main", off, web=False)
    _, main_on = menu.render("main", on, web=False)
    assert f"{menu.SECTION_PREFIX}vault" not in [b.callback_data for b in _buttons(main_off)]
    assert f"{menu.SECTION_PREFIX}vault" in [b.callback_data for b in _buttons(main_on)]
    assert menu.render("vault", off, web=False) is None
    assert menu.render_rich("vault", off, web=False) is None
    assert menu.render("vault", on, web=False) is not None
    assert menu.render_rich("vault", on, web=False) is not None


def test_planner_section_hidden_on_main_and_unreachable_when_disabled():
    off = _settings(False, False, "off", False)
    on = _settings(True, False, "off", False)
    _, main_off = menu.render("main", off, web=False)
    _, main_on = menu.render("main", on, web=False)
    assert f"{menu.SECTION_PREFIX}planner" not in [b.callback_data for b in _buttons(main_off)]
    assert f"{menu.SECTION_PREFIX}planner" in [b.callback_data for b in _buttons(main_on)]
    assert menu.render("planner", off, web=False) is None
    assert menu.render_rich("planner", off, web=False) is None
    assert menu.render("planner", on, web=False) is not None
    assert menu.render_rich("planner", on, web=False) is not None


def test_vault_status_action_moved_out_of_data_section():
    settings = _settings(False, False, "status", False)
    _, data = menu.render("data", settings, web=False)
    _, vault = menu.render("vault", settings, web=False)
    assert f"{menu.ACTION_PREFIX}vault" not in [b.callback_data for b in _action_buttons(data)]
    assert f"{menu.ACTION_PREFIX}vault" in [b.callback_data for b in _action_buttons(vault)]


def test_claude_hidden_when_flag_off_or_web():
    on = _settings(False, False, "off", True)
    off = _settings(False, False, "off", False)
    _, data_flag_off = menu.render("data", off, web=False)
    _, data_flag_on_tg = menu.render("data", on, web=False)
    _, data_flag_on_web = menu.render("data", on, web=True)
    keys = lambda m: [b.callback_data for b in _action_buttons(m)]  # noqa: E731
    assert f"{menu.ACTION_PREFIX}claude" not in keys(data_flag_off)
    assert f"{menu.ACTION_PREFIX}claude" in keys(data_flag_on_tg)
    assert f"{menu.ACTION_PREFIX}claude" not in keys(data_flag_on_web)


def test_hide_kb_hidden_on_web():
    settings = Settings(_env_file=None)
    _, data_tg = menu.render("data", settings, web=False)
    _, data_web = menu.render("data", settings, web=True)
    keys = lambda m: [b.callback_data for b in _action_buttons(m)]  # noqa: E731
    assert f"{menu.ACTION_PREFIX}hide_kb" in keys(data_tg)
    assert f"{menu.ACTION_PREFIX}hide_kb" not in keys(data_web)


def test_data_hint_is_shown():
    settings = Settings(_env_file=None)
    text, _ = menu.render("data", settings, web=False)
    assert menu.DATA_HINT in text


@pytest.mark.parametrize("action", EXCLUDED_ACTIONS + ("nonsense", "", "quiet"))
def test_action_available_is_false_for_excluded_and_garbage(action):
    settings = Settings(_env_file=None)
    assert menu.action_available(action, settings, web=False) is False
    assert menu.action_available(action, settings, web=True) is False


def test_every_rendered_action_button_satisfies_action_available_both_ways():
    for planner, research, vault_mode, claude, web in _flag_combinations():
        settings = _settings(planner, research, vault_mode, claude)
        for view in _view_samples():
            for section in ALL_SECTIONS:
                plain = menu.render(section, settings, web=web, view=view)
                if plain is None:
                    continue
                _, markup = plain
                for button in _action_buttons(markup):
                    _, action = menu.parse_callback(button.callback_data)
                    assert menu.action_available(action, settings, web=web), (
                        f"{section}/{action} rendered for web={web} but action_available said no"
                    )
                rich = menu.render_rich(section, settings, web=web, view=view)
                for data in _rich_action_data(rich):
                    _, action = menu.parse_callback(data)
                    assert menu.action_available(action, settings, web=web), (
                        f"{section}/{action} (rich) rendered for web={web} but action_available said no"
                    )


# --- pure: navigation (every non-main section is one tap from main) ------


def test_every_non_main_section_last_row_is_nav_to_main_both_ways():
    settings = _settings(True, True, "status", True)
    for section in ALL_SECTIONS:
        if section == menu.MAIN_SECTION:
            continue
        _, markup = menu.render(section, settings, web=False)
        last_row = markup.inline_keyboard[-1]
        assert [b.callback_data for b in last_row] == [
            f"{menu.SECTION_PREFIX}{menu.MAIN_SECTION}", menu.CLOSE_CALLBACK,
        ]
        assert [b.text for b in last_row] == ["‹ Меню", "✕ Закрыть"]

        rich = menu.render_rich(section, settings, web=False)
        data = rich_callback_data(rich)
        assert data[-2:] == [f"{menu.SECTION_PREFIX}{menu.MAIN_SECTION}", menu.CLOSE_CALLBACK]


def test_settings_no_longer_links_to_quiet():
    """Design change: settings' own rows are just intensity/focus/pause
    now -- quiet is reachable from main directly, not through settings."""
    settings = Settings(_env_file=None)
    _, markup = menu.render("settings", settings, web=False)
    assert f"{menu.SECTION_PREFIX}quiet" not in [b.callback_data for b in _buttons(markup)]


def test_main_links_to_both_settings_and_quiet_directly():
    settings = Settings(_env_file=None)
    _, markup = menu.render("main", settings, web=False)
    keys = [b.callback_data for b in _buttons(markup)]
    assert f"{menu.SECTION_PREFIX}settings" in keys
    assert f"{menu.SECTION_PREFIX}quiet" in keys


# --- pure: main's status table --------------------------------------------


def test_main_status_table_omits_due_row_when_unset():
    settings = Settings(_env_file=None)
    text, _ = menu.render("main", settings, web=False, view=menu.MenuView())
    assert "Главное" not in text


def test_main_status_table_shows_due_row_untruncated_when_short():
    settings = Settings(_env_file=None)
    view = menu.MenuView(due_action="сдать отчёт")
    text, _ = menu.render("main", settings, web=False, view=view)
    assert "Главное: сдать отчёт" in text


def test_main_status_table_truncates_a_long_due_row_to_40_chars():
    settings = Settings(_env_file=None)
    view = menu.MenuView(due_action="слово " * 20)  # far over 40 chars
    text, _ = menu.render("main", settings, web=False, view=view)
    line = next(row for row in text.splitlines() if row.startswith("Главное:"))
    value = line[len("Главное: "):]
    assert len(value) == 40
    assert value.endswith("…")


# --- pure: settings' intensity row and state-aware toggles ---------------


@pytest.mark.parametrize(
    "intensity,expect_low,expect_high",
    [(1, False, True), (3, True, True), (5, True, False)],
)
def test_intensity_row_draws_only_the_reachable_directions(intensity, expect_low, expect_high):
    settings = Settings(_env_file=None)
    _, markup = menu.render("settings", settings, web=False, view=menu.MenuView(intensity=intensity))
    keys = [b.callback_data for b in _action_buttons(markup)]
    assert (f"{menu.ACTION_PREFIX}int_{intensity - 1}" in keys) is expect_low
    assert (f"{menu.ACTION_PREFIX}int_{intensity + 1}" in keys) is expect_high


def test_intensity_row_middle_buttons_name_the_value_they_set():
    settings = Settings(_env_file=None)
    _, markup = menu.render("settings", settings, web=False, view=menu.MenuView(intensity=3))
    labels = {b.callback_data: b.text for b in _action_buttons(markup)}
    assert labels[f"{menu.ACTION_PREFIX}int_2"] == "🔽 Мягче → 2"
    assert labels[f"{menu.ACTION_PREFIX}int_4"] == "🔼 Строже → 4"


def test_focus_toggle_is_state_aware():
    settings = Settings(_env_file=None)
    _, off = menu.render("settings", settings, web=False, view=menu.MenuView(focus_on=False))
    _, on = menu.render("settings", settings, web=False, view=menu.MenuView(focus_on=True))
    keys_off = [b.callback_data for b in _action_buttons(off)]
    keys_on = [b.callback_data for b in _action_buttons(on)]
    assert f"{menu.ACTION_PREFIX}focus_on" in keys_off and f"{menu.ACTION_PREFIX}focus_off" not in keys_off
    assert f"{menu.ACTION_PREFIX}focus_off" in keys_on and f"{menu.ACTION_PREFIX}focus_on" not in keys_on


def test_pause_toggle_is_state_aware():
    settings = Settings(_env_file=None)
    _, active = menu.render("settings", settings, web=False, view=menu.MenuView(persona_active=True))
    _, paused = menu.render("settings", settings, web=False, view=menu.MenuView(persona_active=False))
    keys_active = [b.callback_data for b in _action_buttons(active)]
    keys_paused = [b.callback_data for b in _action_buttons(paused)]
    assert f"{menu.ACTION_PREFIX}out" in keys_active and f"{menu.ACTION_PREFIX}in" not in keys_active
    assert f"{menu.ACTION_PREFIX}in" in keys_paused and f"{menu.ACTION_PREFIX}out" not in keys_paused


# --- pure: quiet ------------------------------------------------------


def test_quiet_off_button_present_only_while_quiet_is_active():
    settings = Settings(_env_file=None)
    now = datetime.datetime(2026, 9, 28, 10, 0, tzinfo=datetime.timezone.utc)
    inactive = menu.MenuView(now=now, quiet_until=None)
    expired = menu.MenuView(now=now, quiet_until=now - datetime.timedelta(minutes=1))
    active = menu.MenuView(now=now, quiet_until=now + datetime.timedelta(hours=1))
    for view, expect in ((inactive, False), (expired, False), (active, True)):
        _, markup = menu.render("quiet", settings, web=False, view=view)
        present = f"{menu.ACTION_PREFIX}quiet_off" in [b.callback_data for b in _action_buttons(markup)]
        assert present is expect


def test_quiet_value_reads_nothing_when_not_active():
    settings = Settings(_env_file=None)
    text, _ = menu.render("quiet", settings, web=False, view=menu.MenuView())
    assert "Сейчас: нет" in text


def test_quiet_value_same_day_shows_time_only():
    settings = Settings(_env_file=None)
    now = datetime.datetime(2026, 9, 28, 10, 0, tzinfo=datetime.timezone.utc)  # 12:00 Paris
    view = menu.MenuView(now=now, timezone=TIMEZONE, quiet_until=now + datetime.timedelta(hours=3))
    text, _ = menu.render("quiet", settings, web=False, view=view)
    assert "Сейчас: до 15:00" in text


def test_quiet_value_other_day_shows_the_date_too():
    settings = Settings(_env_file=None)
    now = datetime.datetime(2026, 9, 28, 10, 0, tzinfo=datetime.timezone.utc)  # 12:00 Paris, 28th
    view = menu.MenuView(now=now, timezone=TIMEZONE, quiet_until=now + datetime.timedelta(hours=20))
    text, _ = menu.render("quiet", settings, web=False, view=view)
    assert "Сейчас: до 29.09 08:00" in text


# --- pure: the vault section specifically ---------------------------------


def _vault_plain(settings, *, web=False, view=None) -> str:
    return menu.render("vault", settings, web=web, view=view)[0]


def test_notes_toggle_flips_with_consent():
    settings = _settings(False, False, "status", False)
    off = menu.MenuView(notes_consent=False)
    on = menu.MenuView(notes_consent=True)
    _, markup_off = menu.render("vault", settings, web=False, view=off)
    _, markup_on = menu.render("vault", settings, web=False, view=on)
    keys_off = [b.callback_data for b in _action_buttons(markup_off)]
    keys_on = [b.callback_data for b in _action_buttons(markup_on)]
    assert f"{menu.ACTION_PREFIX}notes_on" in keys_off
    assert f"{menu.ACTION_PREFIX}notes_off" not in keys_off
    assert f"{menu.ACTION_PREFIX}notes_off" in keys_on
    assert f"{menu.ACTION_PREFIX}notes_on" not in keys_on
    assert "Заметки: вкл" in _vault_plain(settings, view=on)
    assert "Заметки: выкл" in _vault_plain(settings, view=off)


def test_notes_toggle_available_even_on_web():
    settings = _settings(False, False, "status", False)
    assert menu.action_available("notes_on", settings, web=True) is True
    assert menu.action_available("notes_off", settings, web=True) is True
    _, markup = menu.render("vault", settings, web=True)
    assert f"{menu.ACTION_PREFIX}notes_on" in [b.callback_data for b in _action_buttons(markup)]


def test_lib_read_button_absent_without_a_connection():
    settings = _settings(False, False, "status", True)
    _, markup = menu.render("vault", settings, web=False, view=menu.MenuView(library_read=None))
    keys = [b.callback_data for b in _action_buttons(markup)]
    assert f"{menu.ACTION_PREFIX}lib_read_on" not in keys
    assert f"{menu.ACTION_PREFIX}lib_read_off" not in keys


def test_lib_read_button_present_and_state_dependent_when_connected():
    settings = _settings(False, False, "status", True)
    _, markup_off = menu.render("vault", settings, web=False, view=menu.MenuView(library_read=False))
    _, markup_on = menu.render("vault", settings, web=False, view=menu.MenuView(library_read=True))
    keys_off = [b.callback_data for b in _action_buttons(markup_off)]
    keys_on = [b.callback_data for b in _action_buttons(markup_on)]
    assert f"{menu.ACTION_PREFIX}lib_read_on" in keys_off
    assert f"{menu.ACTION_PREFIX}lib_read_off" not in keys_off
    assert f"{menu.ACTION_PREFIX}lib_read_off" in keys_on
    assert f"{menu.ACTION_PREFIX}lib_read_on" not in keys_on


@pytest.mark.parametrize(
    "claude_enabled,web,read",
    [
        (False, False, True),  # flag off
        (True, True, True),  # web sink
        (True, False, False),  # read off
        (True, False, None),  # never connected
    ],
)
def test_lib_write_button_absent_unless_claude_tg_and_reading(claude_enabled, web, read):
    settings = _settings(False, False, "status", claude_enabled)
    view = menu.MenuView(library_read=read, library_write=False)
    _, markup = menu.render("vault", settings, web=web, view=view)
    keys = [b.callback_data for b in _action_buttons(markup)]
    assert f"{menu.ACTION_PREFIX}lib_write_on" not in keys
    assert f"{menu.ACTION_PREFIX}lib_write_off" not in keys


def test_lib_write_button_present_and_state_dependent_when_reading():
    settings = _settings(False, False, "status", True)
    read_only = menu.MenuView(library_read=True, library_write=False)
    read_write = menu.MenuView(library_read=True, library_write=True)
    _, markup_ro = menu.render("vault", settings, web=False, view=read_only)
    _, markup_rw = menu.render("vault", settings, web=False, view=read_write)
    keys_ro = [b.callback_data for b in _action_buttons(markup_ro)]
    keys_rw = [b.callback_data for b in _action_buttons(markup_rw)]
    assert f"{menu.ACTION_PREFIX}lib_write_on" in keys_ro
    assert f"{menu.ACTION_PREFIX}lib_write_off" not in keys_ro
    assert f"{menu.ACTION_PREFIX}lib_write_off" in keys_rw
    assert f"{menu.ACTION_PREFIX}lib_write_on" not in keys_rw


def test_lib_write_forged_on_web_is_never_available():
    settings = _settings(False, False, "status", True)
    assert menu.action_available("lib_write_on", settings, web=True) is False
    assert menu.action_available("lib_write_off", settings, web=True) is False


@pytest.mark.parametrize(
    "read,write,expected",
    [
        (None, False, "нет подключения"),
        (False, False, "выкл"),
        (True, False, "чтение"),
        (True, True, "чтение+запись"),
    ],
)
def test_claude_library_row_value_states(read, write, expected):
    settings = _settings(False, False, "status", True)
    text = _vault_plain(settings, view=menu.MenuView(library_read=read, library_write=write))
    assert f"Claude, библиотека: {expected}" in text


def test_claude_library_hint_no_longer_names_the_command():
    settings = _settings(False, False, "status", True)
    text = _vault_plain(settings, view=menu.MenuView(library_read=False))
    assert "/claude library on" not in text


def test_claude_library_row_absent_when_flag_off_or_web():
    settings_off = _settings(False, False, "status", False)
    settings_on = _settings(False, False, "status", True)
    assert "Claude, библиотека" not in _vault_plain(settings_off)
    assert "Claude, библиотека" not in _vault_plain(settings_on, web=True)
    assert "Claude, библиотека" in _vault_plain(settings_on, web=False)


def test_vault_status_table_shows_mode_and_flags():
    settings = _settings(False, False, "mirror", False)
    text = _vault_plain(settings)
    assert "Режим: mirror" in text
    assert "Знания: выкл" in text
    assert "Личные: выкл" in text


# --- pure: the planner section specifically -------------------------------


def test_planner_section_not_linked():
    settings = _settings(True, False, "off", False)
    view = menu.MenuView(planner_status=None)
    text, markup = menu.render("planner", settings, web=False, view=view)
    assert "Подключение: не подключён" in text
    assert "Синхронизация" not in text
    keys = [b.callback_data for b in _action_buttons(markup)]
    assert f"{menu.ACTION_PREFIX}planner_on" not in keys
    assert f"{menu.ACTION_PREFIX}planner_off" not in keys
    assert menu.PLANNER_LINK_HINT in text


def test_planner_section_linked_and_active():
    settings = _settings(True, False, "off", False)
    view = menu.MenuView(planner_status="active", planner_enabled=True)
    text, markup = menu.render("planner", settings, web=False, view=view)
    assert "Подключение: подключён" in text
    assert "Синхронизация: вкл" in text
    keys = [b.callback_data for b in _action_buttons(markup)]
    assert f"{menu.ACTION_PREFIX}planner_off" in keys
    assert f"{menu.ACTION_PREFIX}planner_on" not in keys
    assert menu.PLANNER_LINK_HINT not in text


def test_planner_section_linked_but_revoked():
    settings = _settings(True, False, "off", False)
    view = menu.MenuView(planner_status="revoked", planner_enabled=False)
    text, markup = menu.render("planner", settings, web=False, view=view)
    assert "Подключение: нужно переподключить" in text
    assert "Синхронизация: выкл" in text
    keys = [b.callback_data for b in _action_buttons(markup)]
    assert f"{menu.ACTION_PREFIX}planner_on" in keys
    assert f"{menu.ACTION_PREFIX}planner_off" not in keys
    assert menu.PLANNER_LINK_HINT in text


# --- pure: mem --------------------------------------------------------


def test_mem_section_has_digest_and_digest_7d():
    settings = _settings(False, True, "off", False)
    _, markup = menu.render("mem", settings, web=False)
    keys = [b.callback_data for b in _action_buttons(markup)]
    assert f"{menu.ACTION_PREFIX}digest" in keys
    assert f"{menu.ACTION_PREFIX}digest_7d" in keys


# --- other pure bits --------------------------------------------------


def test_parse_callback():
    assert menu.parse_callback("mn:x") == ("close", "")
    assert menu.parse_callback("mn:s:quiet") == ("section", "quiet")
    assert menu.parse_callback("mn:a:checkin") == ("action", "checkin")
    assert menu.parse_callback("m:k:identity:1") is None
    assert menu.parse_callback("garbage") is None


def test_reply_keyboard_has_one_persistent_button():
    kb = menu.reply_keyboard()
    assert isinstance(kb, ReplyKeyboardMarkup)
    assert kb.is_persistent is True
    assert kb.resize_keyboard is True
    assert [b.text for row in kb.keyboard for b in row] == [menu.MENU_BUTTON_TEXT]


def test_menu_is_registered_right_after_start():
    commands = [c.command for c in BOT_COMMANDS]
    assert commands.index("menu") == commands.index("start") + 1


def test_closed_rich_carries_the_closed_text_and_no_buttons():
    rich = menu.closed_rich()
    assert flatten_rich_message(rich) == menu.CLOSED_TEXT
    assert rich_callback_data(rich) == []


# --- router-level ---------------------------------------------------------


def _command_update(update_id: int, text: str) -> dict:
    entities = []
    if text.startswith("/"):
        entities = [{"type": "bot_command", "offset": 0, "length": len(text.split(" ", 1)[0])}]
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Test"},
            "text": text,
            "entities": entities,
        },
    }


def _callback_update(update_id: int, data: str, *, message_id: int) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Test"},
            "chat_instance": "ci",
            "data": data,
            "message": {
                "message_id": message_id,
                "date": 0,
                "chat": {"id": CHAT_ID, "type": "private"},
                "text": "…",
            },
        },
    }


def _build(sessionmaker, settings=None, llm=None, bot=None, fake=None):
    if bot is None:
        bot, fake = make_bot()
    dp = Dispatcher()
    dp.include_router(
        build_router(
            sessionmaker,
            settings or Settings(_env_file=None, TZ_DEFAULT=TIMEZONE),
            llm or FakeLLMProvider(),
        )
    )
    return dp, bot, fake


async def _seed(sessionmaker, *update_ids: int, **state) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE, **state))
        await session.commit()
        for update_id in update_ids:
            session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()


async def _feed(dp, bot, payload: dict) -> None:
    await dp.feed_update(bot, Update.model_validate(payload, context={"bot": bot}))


async def _state(sessionmaker) -> UserState:
    async with sessionmaker() as session:
        return await session.get(UserState, 1)


async def test_start_reply_carries_the_menu_button(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/start"))

    assert fake.sent[0].text == START_TEXT
    markup = fake.sent[0].reply_markup
    assert isinstance(markup, ReplyKeyboardMarkup)
    assert [b.text for row in markup.keyboard for b in row] == [menu.MENU_BUTTON_TEXT]


async def test_menu_command_sends_one_rich_message_with_no_reply_markup(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/menu"))

    assert len(fake.rich) == 1
    assert fake.sent == []
    assert fake.rich[0].reply_markup is None
    assert rich_callback_data(fake.rich[0].rich_message)


async def test_menu_button_text_opens_the_menu_with_no_llm_call_and_no_stored_chat_message(
    sessionmaker,
):
    await _seed(sessionmaker, 1)
    llm = FakeLLMProvider()
    dp, bot, fake = _build(sessionmaker, llm=llm)

    await _feed(dp, bot, _command_update(1, menu.MENU_BUTTON_TEXT))

    assert len(fake.rich) == 1
    assert llm.calls == 0
    async with sessionmaker() as session:
        rows = list((await session.execute(select(Message))).scalars())
    assert not any(row.content == menu.MENU_BUTTON_TEXT for row in rows)


async def test_menu_button_clears_a_pending_checkin_awaiting_step(sessionmaker):
    from app.core import checkin as checkin_core

    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        await checkin_core.set_awaiting_note(session, 1)
    dp, bot, _ = _build(sessionmaker)

    await _feed(dp, bot, _command_update(1, menu.MENU_BUTTON_TEXT))

    state = await _state(sessionmaker)
    assert state.awaiting is None


async def test_section_callback_edits_the_message_in_place_as_rich(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))
    # FakeSession mints message_id 1 for the /menu message just sent.

    await _feed(dp, bot, _callback_update(2, "mn:s:quiet", message_id=1))

    assert len(fake.edits) == 1
    assert fake.edits[0].rich_message is not None
    assert flatten_rich_message(fake.edits[0].rich_message).startswith(menu.QUIET_TITLE)
    assert len(fake.answered) == 1


async def test_unknown_section_answers_stale_and_edits_nothing(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:s:nope", message_id=1))

    assert fake.edits == []
    assert len(fake.answered) == 1
    assert fake.answered[0].text is not None


async def test_close_edits_to_the_closed_rich_text_with_no_buttons(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:x", message_id=1))

    assert fake.edits[-1].rich_message is not None
    assert flatten_rich_message(fake.edits[-1].rich_message) == menu.CLOSED_TEXT
    assert fake.edits[-1].reply_markup is None


async def test_quiet_action_sets_quiet_until_two_hours_ahead(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:quiet_2h", message_id=1))

    state = await _state(sessionmaker)
    assert state.quiet_until is not None
    delta = state.quiet_until - datetime.datetime.now(datetime.timezone.utc)
    assert datetime.timedelta(hours=1, minutes=55) < delta < datetime.timedelta(hours=2, minutes=5)


async def test_quiet_off_action_clears_quiet_until(sessionmaker):
    await _seed(
        sessionmaker,
        1,
        2,
        quiet_until=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5),
    )
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:quiet_off", message_id=1))

    state = await _state(sessionmaker)
    assert state.quiet_until is None


async def test_focus_on_and_off_flip_the_flag(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:focus_on", message_id=1))
    assert (await _state(sessionmaker)).focus_on is True

    await _feed(dp, bot, _callback_update(3, "mn:a:focus_off", message_id=1))
    assert (await _state(sessionmaker)).focus_on is False


async def test_forged_export_and_delete_actions_do_nothing_but_answer(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))
    sent_before = len(fake.sent)
    rich_before = len(fake.rich)

    await _feed(dp, bot, _callback_update(2, "mn:a:export", message_id=1))
    await _feed(dp, bot, _callback_update(3, "mn:a:delete", message_id=1))

    assert fake.documents == []
    assert len(fake.sent) == sent_before
    assert len(fake.rich) == rich_before
    assert len(fake.answered) == 2


async def test_hide_kb_action_sends_the_reply_keyboard_remove(sessionmaker):
    from aiogram.types import ReplyKeyboardRemove

    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:hide_kb", message_id=1))

    assert fake.sent[-1].text == HIDE_KB_REPLY
    assert isinstance(fake.sent[-1].reply_markup, ReplyKeyboardRemove)


async def test_state_action_sends_the_state_message_and_does_not_rerender_the_card(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:state", message_id=1))

    # /state is a rich message (app/tg/state_view.py), the second one
    # sent after /menu's own -- "state" is not in REFRESH_SECTION, so
    # the menu card itself is never edited.
    assert len(fake.rich) == 2
    assert "Персона:" in flatten_rich_message(fake.rich[-1].rich_message)
    assert fake.edits == []


async def test_menu_on_the_web_sink_stays_plain(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    bot, fake = make_bot()
    bot.is_web_sink = True
    dp, bot, fake = _build(sessionmaker, bot=bot, fake=fake)

    await _feed(dp, bot, _command_update(1, "/menu"))
    await _feed(dp, bot, _callback_update(2, "mn:s:quiet", message_id=1))

    assert fake.rich == []
    assert isinstance(fake.sent[0].reply_markup, InlineKeyboardMarkup)
    assert len(fake.edits) == 1
    assert fake.edits[0].rich_message is None
    assert fake.edits[0].text.startswith(menu.QUIET_TITLE)


class _RichMessageFailsSession(FakeSession):
    """A FakeSession whose sendRichMessage/rich editMessageText always
    rejects, like a Telegram client old enough to not understand Bot
    API 10.1 would -- lifted from tests/test_state_view.py's own."""

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):
        if isinstance(method, SendRichMessage):
            raise TelegramBadRequest(method=method, message="Bad Request: RICH_MESSAGE_INVALID")
        if isinstance(method, EditMessageText) and method.rich_message is not None:
            raise TelegramBadRequest(method=method, message="Bad Request: RICH_MESSAGE_INVALID")
        return await super().make_request(bot, method, timeout)


async def test_menu_send_falls_back_to_plain_when_rich_is_rejected(sessionmaker):
    await _seed(sessionmaker, 1)
    fake = _RichMessageFailsSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)
    dp, bot, fake = _build(sessionmaker, bot=bot, fake=fake)

    await _feed(dp, bot, _command_update(1, "/menu"))

    assert fake.rich == []
    assert len(fake.sent) == 1
    assert isinstance(fake.sent[0].reply_markup, InlineKeyboardMarkup)


async def test_menu_section_edit_falls_back_to_plain_when_rich_is_rejected(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    fake = _RichMessageFailsSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)
    dp, bot, fake = _build(sessionmaker, bot=bot, fake=fake)
    # The first send also falls back to plain, minting message_id 1 the
    # same way FakeSession's SendMessage branch does.
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:s:quiet", message_id=1))

    assert fake.edits[-1].rich_message is None
    assert fake.edits[-1].text.startswith(menu.QUIET_TITLE)


async def test_vault_section_shows_status_table_and_notes_toggle(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    settings = Settings(_env_file=None, VAULT_MODE="status", VAULT_API_TOKEN="x" * 32, TZ_DEFAULT=TIMEZONE)
    dp, bot, fake = _build(sessionmaker, settings=settings)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:s:vault", message_id=1))

    flat = flatten_rich_message(fake.edits[-1].rich_message)
    assert "Режим: status" in flat
    assert "Заметки: выкл" in flat
    assert f"{menu.ACTION_PREFIX}notes_on" in rich_callback_data(fake.edits[-1].rich_message)


async def test_notes_on_action_sets_consent_and_rerenders_the_vault_card(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    settings = Settings(_env_file=None, VAULT_MODE="status", VAULT_API_TOKEN="x" * 32, TZ_DEFAULT=TIMEZONE)
    dp, bot, fake = _build(sessionmaker, settings=settings)
    await _feed(dp, bot, _command_update(1, "/menu"))
    await _feed(dp, bot, _callback_update(2, "mn:s:vault", message_id=1))

    await _feed(dp, bot, _callback_update(3, "mn:a:notes_on", message_id=1))

    state = await _state(sessionmaker)
    assert state.notes_consent is True
    # The vault card re-renders in place (same message_id=1), on top of
    # /vault's own confirmation reply.
    assert any(m.text for m in fake.sent)  # the confirmation reply
    last_edit = fake.edits[-1]
    assert last_edit.rich_message is not None
    flat = flatten_rich_message(last_edit.rich_message)
    assert "Заметки: вкл" in flat
    assert f"{menu.ACTION_PREFIX}notes_off" in rich_callback_data(last_edit.rich_message)


async def test_notes_off_action_clears_consent_and_rerenders(sessionmaker):
    await _seed(sessionmaker, 1, 2, notes_consent=True)
    settings = Settings(_env_file=None, VAULT_MODE="status", VAULT_API_TOKEN="x" * 32, TZ_DEFAULT=TIMEZONE)
    dp, bot, fake = _build(sessionmaker, settings=settings)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:notes_off", message_id=1))

    state = await _state(sessionmaker)
    assert state.notes_consent is False
    flat = flatten_rich_message(fake.edits[-1].rich_message)
    assert "Заметки: выкл" in flat


async def test_lib_write_on_action_sets_the_write_switch_and_rerenders(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    async with sessionmaker() as session:
        now = datetime.datetime.now(datetime.timezone.utc)
        session.add(
            OauthConnection(
                client_id="test",
                created_at=now,
                expires_at=now + datetime.timedelta(days=30),
                library_read=True,
            )
        )
        await session.commit()
    settings = Settings(
        _env_file=None,
        VAULT_MODE="status",
        VAULT_API_TOKEN="x" * 32,
        CLAUDE_ACCESS_ENABLED=True,
        TZ_DEFAULT=TIMEZONE,
    )
    dp, bot, fake = _build(sessionmaker, settings=settings)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:lib_write_on", message_id=1))

    async with sessionmaker() as session:
        connection = await session.get(OauthConnection, 1)
    assert connection.library_write is True
    flat = flatten_rich_message(fake.edits[-1].rich_message)
    assert "чтение+запись" in flat
    assert f"{menu.ACTION_PREFIX}lib_write_off" in rich_callback_data(fake.edits[-1].rich_message)


async def test_lib_read_on_action_sets_read_and_rerenders_the_vault_card(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    async with sessionmaker() as session:
        now = datetime.datetime.now(datetime.timezone.utc)
        session.add(
            OauthConnection(
                client_id="test",
                created_at=now,
                expires_at=now + datetime.timedelta(days=30),
                library_read=False,
            )
        )
        await session.commit()
    settings = Settings(
        _env_file=None,
        VAULT_MODE="status",
        VAULT_API_TOKEN="x" * 32,
        CLAUDE_ACCESS_ENABLED=True,
        TZ_DEFAULT=TIMEZONE,
    )
    dp, bot, fake = _build(sessionmaker, settings=settings)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:lib_read_on", message_id=1))

    async with sessionmaker() as session:
        connection = await session.get(OauthConnection, 1)
    assert connection.library_read is True
    flat = flatten_rich_message(fake.edits[-1].rich_message)
    assert "Claude, библиотека: чтение" in flat
    assert f"{menu.ACTION_PREFIX}lib_read_off" in rich_callback_data(fake.edits[-1].rich_message)


async def test_forged_lib_write_on_web_sink_does_nothing(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    settings = Settings(
        _env_file=None,
        VAULT_MODE="status",
        VAULT_API_TOKEN="x" * 32,
        CLAUDE_ACCESS_ENABLED=True,
        TZ_DEFAULT=TIMEZONE,
    )
    bot, fake = make_bot()
    bot.is_web_sink = True
    dp, bot, fake = _build(sessionmaker, settings=settings, bot=bot, fake=fake)
    await _feed(dp, bot, _command_update(1, "/menu"))
    sent_before = len(fake.sent)

    await _feed(dp, bot, _callback_update(2, "mn:a:lib_write_on", message_id=1))

    async with sessionmaker() as session:
        assert await session.get(OauthConnection, 1) is None
    assert len(fake.sent) == sent_before
    assert fake.answered[-1].text is not None


# --- router-level: the new switches (intensity, focus/pause, planner) ----


async def test_int_4_press_sets_intensity_and_rerenders_settings_in_place(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:int_4", message_id=1))

    assert (await _state(sessionmaker)).intensity == 4
    assert len(fake.edits) == 1
    flat = flatten_rich_message(fake.edits[-1].rich_message)
    assert flat.startswith(menu.SETTINGS_TITLE)
    assert "Интенсивность: 4/5" in flat


async def test_focus_on_press_rerenders_settings_in_place(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:focus_on", message_id=1))

    assert (await _state(sessionmaker)).focus_on is True
    flat = flatten_rich_message(fake.edits[-1].rich_message)
    assert flat.startswith(menu.SETTINGS_TITLE)
    assert "Фокус: вкл" in flat
    assert f"{menu.ACTION_PREFIX}focus_off" in rich_callback_data(fake.edits[-1].rich_message)


async def test_quiet_2h_press_rerenders_quiet_showing_do(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:quiet_2h", message_id=1))

    flat = flatten_rich_message(fake.edits[-1].rich_message)
    assert flat.startswith(menu.QUIET_TITLE)
    assert "Сейчас: до" in flat


async def test_planner_on_press_without_a_credential_replies_not_linked_and_rerenders(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    settings = Settings(_env_file=None, PLANNER_ENABLED=True, TZ_DEFAULT=TIMEZONE)
    dp, bot, fake = _build(sessionmaker, settings=settings)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:planner_on", message_id=1))

    async with sessionmaker() as session:
        assert await session.get(PlannerCredential, 1) is None
    assert fake.sent[-1].text == planner_ui.ON_OFF_NOT_LINKED
    flat = flatten_rich_message(fake.edits[-1].rich_message)
    assert "Подключение: не подключён" in flat


async def test_planner_on_press_with_a_linked_credential_enables_sync_and_rerenders(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    now = datetime.datetime.now(datetime.timezone.utc)
    async with sessionmaker() as session:
        session.add(
            PlannerCredential(
                id=1,
                access_token="x",
                refresh_token="y",
                expires_at=now + datetime.timedelta(days=30),
                status=planner_auth.ACTIVE,
                enabled=False,
            )
        )
        await session.commit()
    settings = Settings(_env_file=None, PLANNER_ENABLED=True, TZ_DEFAULT=TIMEZONE)
    dp, bot, fake = _build(sessionmaker, settings=settings)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:planner_on", message_id=1))

    async with sessionmaker() as session:
        credential = await session.get(PlannerCredential, 1)
    assert credential.enabled is True
    assert fake.sent[-1].text == planner_ui.ON_REPLY
    flat = flatten_rich_message(fake.edits[-1].rich_message)
    assert "Синхронизация: вкл" in flat
    assert f"{menu.ACTION_PREFIX}planner_off" in rich_callback_data(fake.edits[-1].rich_message)

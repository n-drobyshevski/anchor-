"""Button menus (the menu redesign, 2026-10-25): `/menu`'s rich hub with
in-body buttons (Bot API 10.1), `/start`'s persistent `☰ Меню` button, the
`mn:` callbacks, and the section tree.

The tree: `main` -> `today, deals, mem, research, knowledge, links,
settings, data` (plus `quiet`, reachable from main and from settings),
and the subsections `order_cadence` -> `order_weekday`, `packet_interest`
and `packet_study` (under research), `lens` (under knowledge), `planner`
and `claude` (under links). Every command is reachable from some action
(`test_every_bot_command_is_reachable_from_the_menu`), the sensitive ones
as confirmed, Telegram-only buttons, and every action is one of three
kinds: run, prompt (typed text) or confirm.

`app/tg/menu.py` is pure -- no DB, no I/O -- so most of it is tested
directly, the same way tests/test_quiet_tz.py tests app/core/quiet.py's
parser before ever touching the router. The router-level tests at the end
follow that file's own Dispatcher pattern (also tests/test_interests_
commands.py's `_callback_update`), and mirror tests/test_state_view.py's
own router group for the rich-message/fallback/web-sink coverage. The
prompt, confirmation and replay flows are in tests/test_menu_flows.py,
which reuses this file's fixtures.
"""

from __future__ import annotations

import datetime
import itertools
import logging

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
from app.web.ingress import BLOCKED_COMMANDS
from conftest import FakeLLMProvider, FakeSession, flatten_rich_message, make_bot, rich_callback_data
from vault_stub import start_stub

CHAT_ID = 555
TIMEZONE = "Europe/Paris"

NOW = datetime.datetime(2026, 10, 25, 10, 0, tzinfo=datetime.timezone.utc)

# The section tree, as the redesign draws it: every section and where
# its "‹" goes.
TOP_LEVEL = ("today", "deals", "mem", "research", "knowledge", "links", "settings", "data")
PARENT = {
    **{section: menu.MAIN_SECTION for section in TOP_LEVEL},
    "quiet": menu.MAIN_SECTION,
    "order_cadence": "deals",
    "order_weekday": "order_cadence",
    "packet_interest": "research",
    "packet_study": "research",
    "lens": "knowledge",
    "planner": "links",
    "claude": "links",
}
# The back button's word for each parent.
BACK_LABEL = {
    menu.MAIN_SECTION: "Меню",
    "deals": "Договорённости",
    "order_cadence": "Как часто",
    "research": "Исследования",
    "knowledge": "Знания",
    "links": "Подключения",
    "settings": "Настройки",
}
TITLE = {
    "main": menu.MAIN_TITLE,
    "today": menu.TODAY_TITLE,
    "deals": menu.DEALS_TITLE,
    "order_cadence": menu.ORDER_CADENCE_TITLE,
    "order_weekday": menu.ORDER_WEEKDAY_TITLE,
    "mem": menu.MEM_TITLE,
    "research": menu.RESEARCH_TITLE,
    "packet_interest": menu.PACKET_TITLE,
    "packet_study": menu.PACKET_TITLE,
    "knowledge": menu.KNOWLEDGE_TITLE,
    "lens": menu.LENS_TITLE,
    "links": menu.LINKS_TITLE,
    "planner": menu.PLANNER_TITLE,
    "claude": menu.CLAUDE_TITLE,
    "settings": menu.SETTINGS_TITLE,
    "quiet": menu.QUIET_TITLE,
    "data": menu.DATA_TITLE,
}
ALL_SECTIONS = tuple(PARENT) + (menu.MAIN_SECTION,)


def _settings(
    *,
    planner: bool = True,
    research: bool = True,
    vault_mode: str = "sync",
    claude: bool = True,
    lens: bool = True,
    links: bool = True,
    graph: bool = True,
    packets: bool = True,
    **extra,
) -> Settings:
    """A deploy with the given feature flags. The defaults are the fully
    open one: everything on, so `_settings(planner=False)` reads as "all
    but the planner". `links` switches Grok and the web UI together;
    `packets` the three research packets' sites."""
    values = dict(
        _env_file=None,
        PLANNER_ENABLED=planner,
        RESEARCH_ENABLED=research,
        VAULT_MODE=vault_mode,
        VAULT_API_TOKEN="x" * 32,
        VAULT_KNOWLEDGE_ENABLED=True,
        CLAUDE_ACCESS_ENABLED=claude,
        LENS_ENABLED=lens,
        LENS_GARDEN_ENABLED=lens,
        GROK_ACCESS_ENABLED=links,
        WEB_UI_ENABLED=links,
        GRAPH_PRUNE_ENABLED=graph,
        PACKET_FORUMS="reddit.com" if packets else "",
        PACKET_GUIDES="example.com" if packets else "",
        PACKET_REF="ru.wikipedia.org" if packets else "",
    )
    values.update(extra)
    return Settings(**values)


FULL = _settings()


def _flag_combinations():
    """Every combination of the settings that gate a menu entry or a
    whole section, plus web."""
    return itertools.product(
        (False, True),  # PLANNER_ENABLED
        (False, True),  # RESEARCH_ENABLED
        ("off", "status", "sync"),  # VAULT_MODE
        (False, True),  # CLAUDE_ACCESS_ENABLED
        (False, True),  # LENS_ENABLED
        (False, True),  # GROK_ACCESS_ENABLED + WEB_UI_ENABLED
        (False, True),  # GRAPH_PRUNE_ENABLED
        (False, True),  # the packets' sites
        (False, True),  # web
    )


def _combination_settings(planner, research, vault_mode, claude, lens, links, graph, packets) -> Settings:
    return _settings(
        planner=planner, research=research, vault_mode=vault_mode, claude=claude, lens=lens,
        links=links, graph=graph, packets=packets,
    )


def _view_samples() -> tuple[menu.MenuView, ...]:
    """A representative sample of MenuView permutations across every
    state-aware switch -- not the full cross product, which would
    explode combinatorially for no extra coverage: `action_available`
    never reads the view (only `settings`/`web`), so the view only ever
    changes *which* of a pair of buttons is drawn, and each such pair is
    already pinned down by its own dedicated test below."""
    return (
        menu.MenuView(),
        menu.MenuView(
            now=NOW, timezone=TIMEZONE, persona_active=False, focus_on=True, intensity=2,
            quiet_until=NOW + datetime.timedelta(hours=1), due_action="x" * 60,
            notes_consent=True, library_read=True, library_write=True,
            planner_status="active", planner_enabled=True, lens_code=True, lens_size=9,
            lens_today_reads=2, research_used=(1, 2, 3), grok_grants=1, web_sessions=2,
            knowledge_notes=5, personal_notes=3,
        ),
        menu.MenuView(
            intensity=5, library_read=False, planner_status="revoked", planner_enabled=False,
            lens_code=False,
        ),
        menu.MenuView(intensity=4, library_read=True, library_write=False, lens_code=None),
        menu.MenuView(library_read=None),
    )


def _buttons(markup: InlineKeyboardMarkup) -> list:
    return [button for row in markup.inline_keyboard for button in row]


def _data(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.callback_data for b in _buttons(markup)]


def _keys(markup: InlineKeyboardMarkup) -> list[str]:
    """The action keys a keyboard offers (`mn:a:` and `mn:i:` buttons)."""
    keys = []
    for data in _data(markup):
        press = menu.parse_callback(data)
        if press is not None and press.kind in ("action", "input"):
            keys.append(press.value)
    return keys


def _render(section: str, settings: Settings = FULL, *, web: bool = False, view=None, origin=None):
    return menu.render(section, settings, web=web, view=view, origin=origin)


def _markup(section: str, settings: Settings = FULL, **kwargs) -> InlineKeyboardMarkup:
    return _render(section, settings, **kwargs)[1]


def _text(section: str, settings: Settings = FULL, **kwargs) -> str:
    return _render(section, settings, **kwargs)[0]


def _offered(section: str, settings: Settings = FULL, **kwargs) -> list[str]:
    return _keys(_markup(section, settings, **kwargs))


# --- pure: the section tree -----------------------------------------------


def test_the_section_set():
    assert set(menu.SECTIONS) == set(ALL_SECTIONS)
    assert len(menu.SECTIONS) == 17
    assert "mode" not in menu.SECTIONS and "vault" not in menu.SECTIONS


@pytest.mark.parametrize("section,parent", sorted(PARENT.items()))
def test_every_section_has_the_parent_the_redesign_draws(section, parent):
    assert menu.parent_of(section) == parent


def test_main_has_no_parent_and_an_unknown_section_none():
    assert menu.parent_of(menu.MAIN_SECTION) is None
    assert menu.parent_of("nope") is None


def test_quiet_has_two_parents_and_only_those_two():
    assert menu.QUIET_ORIGINS == {menu.MAIN_SECTION, "settings"}
    assert menu.parent_of("quiet") == menu.MAIN_SECTION
    assert menu.parent_of("quiet", "settings") == "settings"
    assert menu.parent_of("quiet", "main") == menu.MAIN_SECTION
    # An origin that is not one of the two is ignored, not followed.
    assert menu.parent_of("quiet", "data") == menu.MAIN_SECTION
    # And no other section takes an origin.
    assert menu.parent_of("data", "settings") == menu.MAIN_SECTION


def test_every_section_renders_both_ways():
    for section in ALL_SECTIONS:
        for web in (False, True):
            if not menu.section_exists(section, FULL, web=web):
                assert section in ("lens", "claude") and web
                continue
            text, markup = menu.render(section, FULL, web=web)
            assert text
            assert isinstance(markup, InlineKeyboardMarkup)
            assert markup.inline_keyboard

            rich = menu.render_rich(section, FULL, web=web)
            assert isinstance(rich, InputRichMessage)
            assert rich.blocks


def test_unknown_section_is_none_both_ways():
    settings = Settings(_env_file=None)
    assert menu.render("nope", settings, web=False) is None
    assert menu.render("", settings, web=True) is None
    assert menu.render_rich("nope", settings, web=False) is None
    assert menu.render_rich("", settings, web=True) is None


def test_section_exists_follows_the_feature_flags():
    off = _settings(planner=False, research=False, vault_mode="off", claude=False, lens=False)
    assert menu.section_exists("research", off) is False
    assert menu.section_exists("packet_interest", off) is False
    assert menu.section_exists("packet_study", off) is False
    assert menu.section_exists("knowledge", off) is False
    assert menu.section_exists("lens", off) is False
    assert menu.section_exists("planner", off) is False
    assert menu.section_exists("claude", off) is False
    for section in ("main", "today", "deals", "order_cadence", "order_weekday", "mem", "links",
                    "settings", "quiet", "data"):
        assert menu.section_exists(section, off) is True, section
    assert menu.section_exists("nope", FULL) is False

    assert menu.section_exists("research", _settings(research=True)) is True
    assert menu.section_exists("knowledge", _settings(vault_mode="status")) is True
    assert menu.section_exists("planner", _settings(planner=True)) is True
    assert menu.section_exists("claude", _settings(claude=True)) is True
    # The lens needs the vault *and* its own flag.
    assert menu.section_exists("lens", _settings(lens=True, vault_mode="status")) is True
    assert menu.section_exists("lens", _settings(lens=True, vault_mode="off")) is False
    assert menu.section_exists("lens", _settings(lens=False, vault_mode="sync")) is False


def test_telegram_only_sections_are_not_shown_on_the_web():
    for section in ("lens", "claude"):
        assert menu.section_exists(section, FULL, web=False) is True
        assert menu.section_exists(section, FULL, web=True) is False
        assert menu.render(section, FULL, web=True) is None
        assert menu.render_rich(section, FULL, web=True) is None


def test_packet_sections_exist_only_while_a_packet_has_sites():
    assert menu.section_exists("packet_interest", FULL) is True
    assert menu.section_exists("packet_study", FULL) is True
    none = _settings(packets=False)
    assert menu.section_exists("packet_interest", none) is False
    assert menu.section_exists("packet_study", none) is False
    # Research off hides them too, whatever the packets say.
    assert menu.section_exists("packet_study", _settings(research=False)) is False


@pytest.mark.parametrize("section", ALL_SECTIONS)
def test_section_titles_have_no_trailing_period_and_the_rich_heading_matches_verbatim(section):
    title = TITLE[section]
    assert not title.endswith(".")
    text, _ = _render(section)
    assert text.startswith(title)
    assert menu.render_rich(section, FULL, web=False).blocks[0].text == title


# --- pure: callbacks, gating and navigation across every flag combination --


def test_every_callback_data_is_well_formed_both_ways():
    for *flags, web in _flag_combinations():
        settings = _combination_settings(*flags)
        for view in _view_samples()[:2]:
            for section in ALL_SECTIONS:
                plain = menu.render(section, settings, web=web, view=view)
                rich = menu.render_rich(section, settings, web=web, view=view)
                assert (plain is None) == (rich is None)
                if plain is None:
                    continue
                datas = _data(plain[1])
                assert datas == rich_callback_data(rich), f"{section}: plain and rich disagree"
                for data in datas:
                    assert data.startswith("mn:")
                    assert len(data.encode("utf-8")) <= menu.CALLBACK_DATA_MAX
                    assert menu.parse_callback(data) is not None, data


def test_callback_data_with_every_origin_and_view_stays_within_64_bytes():
    for section in ALL_SECTIONS:
        for origin in (None, "main", "settings"):
            for view in _view_samples():
                _, markup = menu.render(section, FULL, web=False, view=view, origin=origin)
                for data in _data(markup):
                    assert len(data.encode("utf-8")) <= 64, data
    # Every action's own button, with the longest origin, and the «Да» of each.
    for key in menu.ACTIONS:
        assert menu.fits_callback(f"{menu.ACTION_PREFIX}{key}:settings"), key
        assert menu.fits_callback(menu.yes_data(key)), key
    assert menu.fits_callback("mn:a:quiet_off:settings")


def test_every_rendered_button_is_available_and_resolves_for_every_flag_and_view():
    """Each action button passes `action_available` for the settings and
    sink it was drawn for, carries the prefix its kind answers to, and
    every section button leads to a section that exists there too."""
    for *flags, web in _flag_combinations():
        settings = _combination_settings(*flags)
        for view in _view_samples()[:2]:
            for section in ALL_SECTIONS:
                plain = menu.render(section, settings, web=web, view=view)
                if plain is None:
                    continue
                for data in _data(plain[1]):
                    press = menu.parse_callback(data)
                    if press.kind in ("action", "input"):
                        assert menu.action_available(press.value, settings, web=web), (
                            f"{section}/{press.value} rendered for web={web} but action_available said no"
                        )
                        kind = menu.action_kind(press.value)
                        assert (press.kind == "input") == (kind == menu.PROMPT), data
                    elif press.kind == "section":
                        assert menu.section_exists(press.value, settings, web=web), (
                            f"{section} links to {press.value}, which does not exist "
                            f"(web={web}, flags={flags})"
                        )
                    else:
                        assert press.kind == "close", data


def test_every_action_is_available_on_a_fully_open_telegram_deploy():
    for key in menu.ACTIONS:
        assert menu.action_available(key, FULL, web=False) is True, key


@pytest.mark.parametrize("action", ("nonsense", "", "quiet", "export_all", "mn:a:checkin", "CHECKIN"))
def test_action_available_is_false_for_garbage(action):
    assert menu.action_available(action, FULL, web=False) is False
    assert menu.action_available(action, FULL, web=True) is False
    assert menu.action_kind(action) is None
    assert menu.confirmable(action) is False


# --- pure: navigation (every non-main section ends with its back row) ----


@pytest.mark.parametrize("web", (False, True))
def test_every_non_main_section_ends_with_its_back_row_both_ways(web):
    for section in ALL_SECTIONS:
        if section == menu.MAIN_SECTION or not menu.section_exists(section, FULL, web=web):
            continue
        parent = PARENT[section]
        want_data = [f"{menu.SECTION_PREFIX}{parent}", menu.CLOSE_CALLBACK]
        want_text = [f"‹ {BACK_LABEL[parent]}", "✕ Закрыть"]

        _, markup = menu.render(section, FULL, web=web)
        last_row = markup.inline_keyboard[-1]
        assert [b.callback_data for b in last_row] == want_data, section
        assert [b.text for b in last_row] == want_text, section

        rich = menu.render_rich(section, FULL, web=web)
        assert rich_callback_data(rich)[-2:] == want_data, section


def test_top_level_sections_go_back_to_the_menu_with_close_beside_it():
    for section in (*TOP_LEVEL, "quiet"):
        last_row = _markup(section).inline_keyboard[-1]
        assert [(b.text, b.callback_data) for b in last_row] == [
            ("‹ Меню", "mn:s:main"),
            ("✕ Закрыть", "mn:x"),
        ], section


def test_subsections_go_back_to_their_parent_by_name():
    expected = {
        "order_cadence": "‹ Договорённости",
        "order_weekday": "‹ Как часто",
        "packet_interest": "‹ Исследования",
        "packet_study": "‹ Исследования",
        "lens": "‹ Знания",
        "planner": "‹ Подключения",
        "claude": "‹ Подключения",
    }
    for section, label in expected.items():
        back = _markup(section).inline_keyboard[-1][0]
        assert back.text == label, section
        assert back.callback_data == f"mn:s:{PARENT[section]}", section


def test_a_back_button_never_leads_to_a_section_that_is_gone():
    """Whenever a section exists, so does its parent (so "‹" is never stale)."""
    for *flags, web in _flag_combinations():
        settings = _combination_settings(*flags)
        for section, parent in PARENT.items():
            if menu.section_exists(section, settings, web=web):
                assert menu.section_exists(parent, settings, web=web), (section, parent)


def test_quiet_opened_from_settings_goes_back_to_settings_and_keeps_that_origin():
    _, from_main = menu.render("quiet", FULL, web=False)
    _, from_settings = menu.render("quiet", FULL, web=False, origin="settings")
    back = from_settings.inline_keyboard[-1]
    assert [(b.text, b.callback_data) for b in back] == [
        ("‹ Настройки", "mn:s:settings"),
        ("✕ Закрыть", "mn:x"),
    ]
    assert from_main.inline_keyboard[-1][0].callback_data == "mn:s:main"
    # Every preset carries the origin, so the re-render after one keeps
    # the right back button; from main they carry none.
    view = menu.MenuView(now=NOW, quiet_until=NOW + datetime.timedelta(hours=1))
    _, active = menu.render("quiet", FULL, web=False, view=view, origin="settings")
    preset = [d for d in _data(active) if d.startswith(menu.ACTION_PREFIX)]
    assert preset == [
        "mn:a:quiet_30m:settings", "mn:a:quiet_2h:settings", "mn:a:quiet_8h:settings",
        "mn:a:quiet_1d:settings", "mn:a:quiet_off:settings",
    ]
    _, active_main = menu.render("quiet", FULL, web=False, view=view)
    assert [d for d in _data(active_main) if d.startswith(menu.ACTION_PREFIX)] == [
        "mn:a:quiet_30m", "mn:a:quiet_2h", "mn:a:quiet_8h", "mn:a:quiet_1d", "mn:a:quiet_off",
    ]
    rich = menu.render_rich("quiet", FULL, web=False, origin="settings")
    assert rich_callback_data(rich)[-2:] == ["mn:s:settings", menu.CLOSE_CALLBACK]


def test_main_and_settings_both_link_to_quiet_with_the_right_origin():
    assert "mn:s:quiet" in _data(_markup("main"))
    assert "mn:s:quiet:settings" in _data(_markup("settings"))


# --- pure: main -------------------------------------------------------------


def test_main_layout():
    text, markup = _render("main")
    rows = [[b.callback_data for b in row] for row in markup.inline_keyboard]
    assert rows == [
        ["mn:a:checkin", "mn:a:state"],
        ["mn:s:today", "mn:s:deals"],
        ["mn:s:mem", "mn:s:research"],
        ["mn:s:knowledge", "mn:s:links"],
        ["mn:s:settings", "mn:s:data"],
        ["mn:s:quiet", "mn:x"],
    ]
    assert text.splitlines()[0] == "Меню"
    assert text.splitlines()[-1] == menu.FOOTER_TEXT == "Писать можно и просто так."
    rich = menu.render_rich("main", FULL, web=False)
    assert flatten_rich_message(rich).splitlines()[-1] == menu.FOOTER_TEXT


def test_main_hides_the_sections_whose_feature_is_off():
    off = _settings(research=False, vault_mode="off")
    data = _data(_markup("main", off))
    assert "mn:s:research" not in data and "mn:s:knowledge" not in data
    for section in ("today", "deals", "mem", "links", "settings", "data", "quiet"):
        assert f"mn:s:{section}" in data
    assert "mn:s:research" in _data(_markup("main", _settings(vault_mode="off")))
    assert "mn:s:knowledge" in _data(_markup("main", _settings(research=False)))


def test_main_status_table_defaults():
    text = _text("main", view=menu.MenuView())
    assert "Бот: в роли" in text
    assert "Тишина: нет" in text
    assert "Фокус: выкл" in text
    assert "Интенсивность: 3" in text
    paused = _text("main", view=menu.MenuView(persona_active=False, focus_on=True, intensity=5))
    assert "Бот: на паузе" in paused and "Фокус: вкл" in paused and "Интенсивность: 5" in paused


def test_main_status_table_omits_due_row_when_unset():
    assert "Главное" not in _text("main", view=menu.MenuView())


def test_main_status_table_shows_due_row_untruncated_when_short():
    text = _text("main", view=menu.MenuView(due_action="сдать отчёт"))
    assert "Главное: сдать отчёт" in text


def test_main_status_table_truncates_a_long_due_row_to_40_chars():
    text = _text("main", view=menu.MenuView(due_action="слово " * 20))  # far over 40 chars
    line = next(row for row in text.splitlines() if row.startswith("Главное:"))
    value = line[len("Главное: "):]
    assert len(value) == 40
    assert value.endswith("…")
    # Exactly 40 stays whole.
    exact = _text("main", view=menu.MenuView(due_action="я" * 40))
    assert "Главное: " + "я" * 40 in exact


def test_main_quiet_row_reads_the_live_quiet_period():
    view = menu.MenuView(now=NOW, timezone=TIMEZONE, quiet_until=NOW + datetime.timedelta(hours=3))
    assert "Тишина: до 14:00" in _text("main", view=view)


# --- pure: today ----------------------------------------------------------


def test_today_layout_with_everything_on():
    view = menu.MenuView(due_action="дописать отчёт", focus_on=False)
    text, markup = _render("today", view=view)
    assert "Главное: дописать отчёт · Фокус: выкл" in text
    rows = [[b.callback_data for b in row] for row in markup.inline_keyboard]
    assert rows == [
        ["mn:a:checkin", "mn:a:plan"],
        ["mn:i:due_set", "mn:a:due_clear"],
        ["mn:a:focus_on"],
        ["mn:i:task", "mn:i:event"],
        ["mn:a:done"],
        ["mn:s:main", "mn:x"],
    ]


def test_today_hint_when_nothing_is_set():
    assert "Главное: не задано · Фокус: выкл" in _text("today", view=menu.MenuView())


def test_today_clear_button_exists_only_while_a_main_action_is_set():
    assert "due_clear" not in _offered("today", view=menu.MenuView(due_action=None))
    assert "due_clear" in _offered("today", view=menu.MenuView(due_action="x"))
    assert "due_set" in _offered("today", view=menu.MenuView(due_action=None))
    # Clearing asks first.
    assert menu.action_kind("due_clear") == menu.CONFIRM
    assert menu.action_kind("due_set") == menu.PROMPT


def test_focus_toggle_is_state_aware():
    off = _offered("today", view=menu.MenuView(focus_on=False))
    on = _offered("today", view=menu.MenuView(focus_on=True))
    assert "focus_on" in off and "focus_off" not in off
    assert "focus_off" in on and "focus_on" not in on
    assert "Фокус: вкл" in _text("today", view=menu.MenuView(focus_on=True))
    # Focus moved out of settings.
    assert not {"focus_on", "focus_off"} & set(_offered("settings"))


def test_today_planner_buttons_exist_only_with_the_planner():
    planner_keys = {"plan", "task", "event", "done"}
    assert planner_keys <= set(_offered("today"))
    off = _offered("today", _settings(planner=False))
    assert not planner_keys & set(off)
    assert {"checkin", "due_set", "focus_on"} <= set(off)


def test_today_works_on_the_web_with_the_same_buttons():
    assert _offered("today", web=True) == _offered("today", web=False)


# --- pure: deals ----------------------------------------------------------


def test_deals_layout():
    _, markup = _render("deals")
    rows = [[b.callback_data for b in row] for row in markup.inline_keyboard]
    assert rows == [
        ["mn:a:orders", "mn:s:order_cadence"],
        ["mn:a:paid", "mn:a:review"],
        ["mn:s:main", "mn:x"],
    ]


def test_order_cadence_picker_then_weekday_picker():
    _, cadence = _render("order_cadence")
    assert [[b.callback_data for b in row] for row in cadence.inline_keyboard] == [
        ["mn:i:order_daily", "mn:i:order_weekdays"],
        ["mn:s:order_weekday", "mn:i:order_once"],
        ["mn:s:deals", "mn:x"],
    ]
    _, weekday = _render("order_weekday")
    days = [[b.text for b in row] for row in weekday.inline_keyboard][0]
    assert days == ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    assert _keys(weekday) == [f"order_w{n}" for n in range(1, 8)]
    assert weekday.inline_keyboard[-1][0].callback_data == "mn:s:order_cadence"


def test_every_cadence_button_is_a_prompt_for_an_order():
    for key in ("order_daily", "order_weekdays", "order_once", *(f"order_w{n}" for n in range(1, 8))):
        action = menu.ACTIONS[key]
        assert action.kind == menu.PROMPT
        assert action.commands == ("order",)
        assert action.args


# --- pure: mem ------------------------------------------------------------


def test_mem_layout():
    _, markup = _render("mem")
    rows = [[b.callback_data for b in row] for row in markup.inline_keyboard]
    assert rows == [
        ["mn:a:memories", "mn:i:remember"],
        ["mn:i:pin", "mn:i:unpin"],
        ["mn:i:forget"],
        ["mn:a:mind", "mn:i:mind_add"],
        ["mn:a:amendments"],
        ["mn:a:digest", "mn:a:digest_7d"],
        ["mn:s:main", "mn:x"],
    ]


def test_forget_is_a_prompt_that_leads_to_a_confirmation_card():
    assert menu.action_kind("forget") == menu.PROMPT
    assert "forget" in menu.CONFIRM_AFTER_INPUT
    assert menu.confirmable("forget") is True
    assert menu.ACTIONS["forget"].label.startswith("⚠️")


def test_mem_section_has_digest_and_digest_7d():
    keys = _offered("mem", _settings(research=False))
    assert "digest" in keys and "digest_7d" in keys


# --- pure: research -------------------------------------------------------


def test_research_layout():
    view = menu.MenuView(research_used=(0, 1, 0))
    text, markup = _render("research", view=view)
    assert "Сегодня: /study 0/1 · ручной поиск 1/3 · /read 0/3" in text
    rows = [[b.callback_data for b in row] for row in markup.inline_keyboard]
    assert rows == [
        ["mn:a:interests", "mn:s:packet_interest"],
        ["mn:a:interests_run", "mn:i:interests_run_id"],
        ["mn:a:interests_suggest"],
        ["mn:s:packet_study", "mn:i:read"],
        ["mn:a:notes", "mn:i:card"],
        ["mn:s:main", "mn:x"],
    ]


def test_research_quota_hint_absent_until_the_counts_are_read():
    assert "Сегодня:" not in _text("research", view=menu.MenuView())


def test_research_hides_the_packet_buttons_when_no_packet_has_sites():
    keys = _data(_markup("research", _settings(packets=False)))
    assert "mn:s:packet_interest" not in keys and "mn:s:packet_study" not in keys
    assert "mn:a:interests" in keys and "mn:i:read" in keys


def test_suggest_is_telegram_only_and_hidden_on_the_web():
    assert "interests_suggest" in _offered("research", web=False)
    assert "interests_suggest" not in _offered("research", web=True)


def test_packet_pickers_list_only_packets_with_sites():
    # By default PACKET_GUIDES is empty (app/config.py).
    default = Settings(_env_file=None, RESEARCH_ENABLED=True)
    assert _offered("packet_interest", default) == ["interests_add_forums", "interests_add_ref"]
    assert _offered("packet_study", default) == ["study_forums", "study_ref"]
    assert _offered("packet_interest") == [
        "interests_add_forums", "interests_add_guides", "interests_add_ref",
    ]
    assert _offered("packet_study") == ["study_forums", "study_guides", "study_ref"]
    one = _settings(PACKET_FORUMS="", PACKET_GUIDES="", PACKET_REF="ru.wikipedia.org")
    assert _offered("packet_study", one) == ["study_ref"]


def test_packet_actions_are_prompts_for_the_matching_command():
    for packet in menu.PACKETS:
        add = menu.ACTIONS[f"interests_add_{packet}"]
        study = menu.ACTIONS[f"study_{packet}"]
        assert (add.kind, add.commands, add.args) == (menu.PROMPT, ("interests",), f"add {packet}")
        assert (study.kind, study.commands, study.args) == (menu.PROMPT, ("study",), packet)


def test_research_actions_vanish_with_the_research_flag():
    off = _settings(research=False)
    for key in ("interests_run", "interests_run_id", "interests_suggest", "read", "notes", "card",
                "interests_add_forums", "study_forums"):
        assert menu.action_available(key, off, web=False) is False, key
    # /interests (the list) is not gated here: the command answers itself.
    assert menu.action_available("interests", off, web=False) is True


def test_card_reaches_adopt_and_reject_through_its_own_buttons():
    assert menu.ACTIONS["card"].commands == ("card", "adopt", "reject")


# --- pure: knowledge ------------------------------------------------------


def _knowledge_text(settings=None, *, web=False, view=None) -> str:
    return _text("knowledge", settings or _settings(claude=False), web=web, view=view)


def test_knowledge_layout():
    _, markup = _render("knowledge")
    rows = [[b.callback_data for b in row] for row in markup.inline_keyboard]
    assert rows == [
        ["mn:a:notes_on"],
        ["mn:a:vault"],
        ["mn:s:lens"],
        ["mn:a:graph", "mn:a:graph_undo"],
        ["mn:s:main", "mn:x"],
    ]


def test_notes_toggle_flips_with_consent():
    settings = _settings(claude=False)
    off = menu.MenuView(notes_consent=False)
    on = menu.MenuView(notes_consent=True)
    keys_off = _offered("knowledge", settings, view=off)
    keys_on = _offered("knowledge", settings, view=on)
    assert "notes_on" in keys_off and "notes_off" not in keys_off
    assert "notes_off" in keys_on and "notes_on" not in keys_on
    assert "Заметки: вкл" in _knowledge_text(settings, view=on)
    assert "Заметки: выкл" in _knowledge_text(settings, view=off)


def test_notes_toggle_and_status_are_available_even_on_the_web():
    for key in ("notes_on", "notes_off", "vault"):
        assert menu.action_available(key, FULL, web=True) is True
    keys = _offered("knowledge", web=True)
    assert "notes_on" in keys and "vault" in keys


def test_knowledge_telegram_only_rows_are_not_drawn_on_the_web():
    web_data = _data(_markup("knowledge", web=True))
    assert "mn:s:lens" not in web_data
    keys = _offered("knowledge", web=True)
    assert "graph" not in keys and "graph_undo" not in keys
    tg_keys = _offered("knowledge", web=False)
    assert "graph" in tg_keys and "graph_undo" in tg_keys


def test_knowledge_graph_rows_follow_their_own_flag():
    assert not {"graph", "graph_undo"} & set(_offered("knowledge", _settings(graph=False)))


def test_knowledge_lens_entry_follows_the_lens_flag():
    assert "mn:s:lens" in _data(_markup("knowledge", _settings(lens=True)))
    assert "mn:s:lens" not in _data(_markup("knowledge", _settings(lens=False)))


def test_knowledge_status_table_shows_mode_counts_and_flags():
    settings = _settings(vault_mode="mirror", claude=False)
    text = _knowledge_text(settings)
    assert "Режим: mirror" in text
    assert "Знания: вкл" in text
    assert "Личные: выкл" in text
    view = menu.MenuView(knowledge_notes=214, personal_notes=38, notes_consent=True)
    text = _knowledge_text(settings, view=view)
    assert "Режим: mirror · Знания: 214 · Личные: выкл · Заметки: вкл" in text
    both = _settings(vault_mode="sync", claude=False, VAULT_PERSONAL_ENABLED=True)
    assert "Знания: 214 · Личные: 38" in _knowledge_text(both, view=view)


def test_knowledge_no_longer_carries_the_claude_library_switches():
    view = menu.MenuView(library_read=True, library_write=True)
    keys = _offered("knowledge", view=view)
    assert not {"lib_read_on", "lib_read_off", "lib_write_on", "lib_write_off"} & set(keys)
    assert "Claude, библиотека" not in _text("knowledge", view=view)


# --- 8f: the knowledge-roots hint in the "Хранилище и знания" screen ------


def test_roots_hint_absent_when_not_fetched():
    """`settings_state=None` -- notes consent off, or the manifest never
    answered -- shows nothing."""
    text = _knowledge_text(view=menu.MenuView(notes_consent=False))
    assert "Корни знаний" not in text
    assert "Anchor/settings.md" not in text


def test_roots_hint_valid_with_roots():
    view = menu.MenuView(notes_consent=True, settings_state="valid", knowledge_roots=("Library",))
    assert "Корни знаний: Library" in _knowledge_text(view=view)


def test_roots_hint_valid_with_no_roots():
    view = menu.MenuView(notes_consent=True, settings_state="valid", knowledge_roots=())
    text = _knowledge_text(view=view)
    assert "Корни знаний: не заданы — добавь knowledge_folders в Anchor/settings.md" in text


def test_roots_hint_missing():
    view = menu.MenuView(notes_consent=True, settings_state="missing")
    assert "Anchor/settings.md не найден — корней знаний нет" in _knowledge_text(view=view)


def test_roots_hint_wrong_case():
    view = menu.MenuView(notes_consent=True, settings_state="wrong_case")
    text = _knowledge_text(view=view)
    assert "переименуй его в Anchor/settings.md (регистр важен)" in text


def test_roots_hint_invalid():
    view = menu.MenuView(notes_consent=True, settings_state="invalid")
    assert "Anchor/settings.md с ошибкой — ни одна заметка не читается" in _knowledge_text(view=view)


def test_roots_hint_truncates_past_five():
    roots = tuple(f"Root{i}" for i in range(7))
    view = menu.MenuView(notes_consent=True, settings_state="valid", knowledge_roots=roots)
    text = _knowledge_text(view=view)
    assert "Корни знаний: Root0, Root1, Root2, Root3, Root4 и ещё 2" in text


def test_roots_hint_sits_on_its_own_line_under_the_status():
    view = menu.MenuView(notes_consent=True, settings_state="valid", knowledge_roots=("Library",))
    lines = _knowledge_text(view=view).splitlines()
    assert lines[1].startswith("Режим:")
    assert lines[2] == "Корни знаний: Library"


# --- pure: lens -----------------------------------------------------------


def test_lens_table_shows_counts_and_the_door_never_a_title():
    view = menu.MenuView(lens_size=12, lens_code=False, lens_today_reads=3)
    text = _text("lens", view=view)
    assert "Заметок в линзе: 12" in text
    assert "Доступ Claude Code: закрыт" in text
    assert "Чтений сегодня: 3" in text
    assert "Доступ Claude Code: открыт" in _text("lens", view=menu.MenuView(lens_code=True))
    assert "Доступ Claude Code: роли нет" in _text("lens", view=menu.MenuView(lens_code=None))
    bare = _text("lens", view=menu.MenuView())
    assert "Заметок в линзе" not in bare and "Чтений сегодня" not in bare


def test_lens_code_switch_is_state_aware():
    closed = _offered("lens", view=menu.MenuView(lens_code=False))
    opened = _offered("lens", view=menu.MenuView(lens_code=True))
    assert "lens_code_on" in closed and "lens_code_off" not in closed
    assert "lens_code_off" in opened and "lens_code_on" not in opened
    # Opening asks first; closing does not.
    assert menu.action_kind("lens_code_on") == menu.CONFIRM
    assert menu.action_kind("lens_code_off") == menu.RUN
    assert menu.ACTIONS["lens_code_off"].refresh == "lens"


def test_lens_rows():
    _, markup = _render("lens", view=menu.MenuView(lens_code=False))
    rows = [[b.callback_data for b in row] for row in markup.inline_keyboard]
    assert rows == [
        ["mn:a:lens_garden", "mn:a:lens_research"],
        ["mn:a:lens_undo"],
        ["mn:a:lens_code_on"],
        ["mn:s:knowledge", "mn:x"],
    ]


def test_lens_garden_and_research_follow_their_switches():
    no_garden = Settings(
        _env_file=None, VAULT_MODE="sync", VAULT_API_TOKEN="x" * 32, LENS_ENABLED=True,
        RESEARCH_ENABLED=True, LENS_GARDEN_ENABLED=False, VAULT_KNOWLEDGE_ENABLED=True,
    )
    keys = _offered("lens", no_garden)
    assert "lens_garden" not in keys and "lens_research" not in keys
    assert "lens_undo" in keys
    no_research = _settings(research=False)
    keys = _offered("lens", no_research)
    assert "lens_garden" in keys and "lens_research" not in keys
    status = _settings(vault_mode="status")
    assert "lens_garden" not in _offered("lens", status)


def test_lens_undo_asks_first_and_graph_undo_too():
    assert menu.action_kind("lens_undo") == menu.CONFIRM
    assert menu.action_kind("graph_undo") == menu.CONFIRM
    assert menu.action_kind("graph") == menu.RUN


# --- pure: links ----------------------------------------------------------


def test_links_layout_and_status_line():
    view = menu.MenuView(
        planner_status="active", library_read=True, library_write=False, grok_grants=0,
        web_sessions=1,
    )
    text, markup = _render("links", view=view)
    assert "Планер: подключён · Claude: чтение · Grok: нет · Веб: 1 сессия" in text
    rows = [[b.callback_data for b in row] for row in markup.inline_keyboard]
    assert rows == [
        ["mn:s:planner", "mn:s:claude"],
        ["mn:a:grok_open"],
        ["mn:a:weblogout"],
        ["mn:a:revoke"],
        ["mn:s:main", "mn:x"],
    ]
    assert "Grok: открыт" in _text("links", view=menu.MenuView(grok_grants=2))
    assert "Веб: 2 сессии" in _text("links", view=menu.MenuView(web_sessions=2))
    assert "Веб: 5 сессий" in _text("links", view=menu.MenuView(web_sessions=5))


def test_links_claude_status_values():
    assert "Claude: нет" in _text("links", view=menu.MenuView(library_read=None))
    assert "Claude: подключён" in _text("links", view=menu.MenuView(library_read=False))
    assert "Claude: чтение+запись" in _text(
        "links", view=menu.MenuView(library_read=True, library_write=True)
    )


def test_links_entries_follow_their_flags():
    only_revoke = _settings(planner=False, claude=False, links=False)
    assert _data(_markup("links", only_revoke)) == ["mn:a:revoke", "mn:s:main", "mn:x"]
    assert "mn:s:planner" in _data(_markup("links", _settings(claude=False, links=False)))
    assert "mn:s:claude" in _data(_markup("links", _settings(planner=False, links=False)))


def test_links_on_the_web_offers_only_what_the_web_may_do():
    web = _data(_markup("links", web=True))
    assert web == ["mn:s:planner", "mn:a:revoke", "mn:s:main", "mn:x"]
    # The Telegram-only features' status stays where their buttons are.
    text = _text("links", web=True, view=menu.MenuView(library_read=True, grok_grants=1, web_sessions=3))
    assert "Claude" not in text and "Grok" not in text and "Веб" not in text


# --- pure: planner --------------------------------------------------------


def test_planner_section_not_linked():
    view = menu.MenuView(planner_status=None)
    text, markup = _render("planner", view=view)
    assert "Подключение: не подключён" in text
    assert "Синхронизация" not in text
    keys = _keys(markup)
    assert "planner_on" not in keys and "planner_off" not in keys
    assert "planner_link" in keys
    assert menu.action_kind("planner_link") == menu.CONFIRM


def test_planner_section_linked_and_active():
    view = menu.MenuView(planner_status="active", planner_enabled=True)
    text, markup = _render("planner", view=view)
    assert "Подключение: подключён" in text
    assert "Синхронизация: вкл" in text
    keys = _keys(markup)
    assert "planner_off" in keys and "planner_on" not in keys
    assert "planner_link" not in keys
    assert {"plan", "planner"} <= set(keys)


def test_planner_section_linked_but_revoked():
    view = menu.MenuView(planner_status="revoked", planner_enabled=False)
    text, markup = _render("planner", view=view)
    assert "Подключение: нужно переподключить" in text
    assert "Синхронизация: выкл" in text
    keys = _keys(markup)
    assert "planner_on" in keys and "planner_off" not in keys
    assert "planner_link" in keys


def test_planner_link_is_telegram_only_so_the_web_card_has_no_link_button():
    assert "planner_link" not in _offered("planner", web=True, view=menu.MenuView())
    assert "planner_link" in _offered("planner", web=False, view=menu.MenuView())


# --- pure: claude ---------------------------------------------------------


def test_claude_without_a_connection():
    text, markup = _render("claude", view=menu.MenuView(library_read=None))
    assert "Подключение: нет" in text and "Библиотека: нет подключения" in text
    assert _keys(markup) == ["claude", "claude_connect", "claude_limits"]


def test_claude_library_read_switch_is_state_aware():
    off = _offered("claude", view=menu.MenuView(library_read=False))
    on = _offered("claude", view=menu.MenuView(library_read=True))
    assert "lib_read_on" in off and "lib_read_off" not in off
    assert "lib_read_off" in on and "lib_read_on" not in on


@pytest.mark.parametrize("read", (None, False))
def test_claude_library_write_switch_is_absent_unless_reading(read):
    keys = _offered("claude", view=menu.MenuView(library_read=read, library_write=False))
    assert "lib_write_on" not in keys and "lib_write_off" not in keys


def test_claude_library_write_switch_is_state_aware_when_reading():
    ro = _offered("claude", view=menu.MenuView(library_read=True, library_write=False))
    rw = _offered("claude", view=menu.MenuView(library_read=True, library_write=True))
    assert "lib_write_on" in ro and "lib_write_off" not in ro
    assert "lib_write_off" in rw and "lib_write_on" not in rw


def test_claude_undo_and_disconnect_need_a_connection_and_ask_first():
    none = _offered("claude", view=menu.MenuView(library_read=None))
    some = _offered("claude", view=menu.MenuView(library_read=False))
    assert not {"claude_undo", "claude_undo_all", "claude_disconnect"} & set(none)
    assert {"claude_undo", "claude_undo_all", "claude_disconnect"} <= set(some)
    for key in ("claude_undo", "claude_undo_all", "claude_disconnect"):
        assert menu.action_kind(key) == menu.CONFIRM
    # Connecting is typed, then confirmed.
    assert menu.action_kind("claude_connect") == menu.PROMPT
    assert "claude_connect" in menu.CONFIRM_AFTER_INPUT


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
    text = _text("claude", view=menu.MenuView(library_read=read, library_write=write))
    assert f"Библиотека: {expected}" in text
    assert "/claude library on" not in text


def test_claude_switches_are_never_available_on_the_web():
    for key in ("lib_read_on", "lib_read_off", "lib_write_on", "lib_write_off", "claude_connect",
                "claude_limits", "claude_undo", "claude_disconnect"):
        assert menu.action_available(key, FULL, web=True) is False, key
    assert menu.render("claude", FULL, web=True) is None


def test_claude_hidden_when_the_flag_is_off():
    off = _settings(claude=False)
    assert menu.section_exists("claude", off) is False
    for key in ("claude", "claude_connect", "lib_read_on", "lib_write_on", "claude_disconnect"):
        assert menu.action_available(key, off, web=False) is False, key


# --- pure: settings, quiet, data ------------------------------------------


@pytest.mark.parametrize(
    "intensity,expect_low,expect_high",
    [(1, False, True), (3, True, True), (5, True, False)],
)
def test_intensity_row_draws_only_the_reachable_directions(intensity, expect_low, expect_high):
    keys = _offered("settings", view=menu.MenuView(intensity=intensity))
    assert (f"int_{intensity - 1}" in keys) is expect_low
    assert (f"int_{intensity + 1}" in keys) is expect_high
    assert f"int_{intensity}" not in keys


def test_intensity_row_buttons_name_the_value_they_set():
    labels = {
        menu.parse_callback(b.callback_data).value: b.text
        for b in _buttons(_markup("settings", view=menu.MenuView(intensity=3)))
    }
    assert labels["int_2"] == "🔽 Мягче → 2"
    assert labels["int_4"] == "🔼 Строже → 4"
    ends = {
        menu.parse_callback(b.callback_data).value: b.text
        for b in _buttons(_markup("settings", view=menu.MenuView(intensity=1)))
    }
    assert ends["int_2"] == "🔼 Строже → 2"


def test_pause_toggle_is_state_aware():
    active = _offered("settings", view=menu.MenuView(persona_active=True))
    paused = _offered("settings", view=menu.MenuView(persona_active=False))
    assert "out" in active and "in" not in active
    assert "in" in paused and "out" not in paused


def test_settings_layout_and_status_line():
    view = menu.MenuView(intensity=3, timezone=TIMEZONE)
    text, markup = _render("settings", view=view)
    assert "Интенсивность: 3 · Ночью тихо: 22:30–08:00 · Утро/вечер: 09:00/22:00 · Пояс: Europe/Paris" in text
    rows = [[b.callback_data for b in row] for row in markup.inline_keyboard]
    assert rows == [
        ["mn:a:int_2", "mn:a:int_4"],
        ["mn:a:out"],
        ["mn:s:quiet:settings", "mn:i:tz"],
        ["mn:a:hide_kb"],
        ["mn:s:main", "mn:x"],
    ]


def test_hide_kb_hidden_on_web():
    assert "hide_kb" in _offered("settings", web=False)
    assert "hide_kb" not in _offered("settings", web=True)


def test_quiet_off_button_present_only_while_quiet_is_active():
    inactive = menu.MenuView(now=NOW, quiet_until=None)
    expired = menu.MenuView(now=NOW, quiet_until=NOW - datetime.timedelta(minutes=1))
    active = menu.MenuView(now=NOW, quiet_until=NOW + datetime.timedelta(hours=1))
    for view, expect in ((inactive, False), (expired, False), (active, True)):
        assert ("quiet_off" in _offered("quiet", view=view)) is expect


def test_quiet_presets_are_always_drawn():
    assert _offered("quiet") == ["quiet_30m", "quiet_2h", "quiet_8h", "quiet_1d"]
    assert menu.QUIET_HINT in _text("quiet")


def test_quiet_value_reads_nothing_when_not_active():
    assert "Сейчас: нет" in _text("quiet", view=menu.MenuView())


def test_quiet_value_same_day_shows_time_only():
    view = menu.MenuView(now=NOW, timezone=TIMEZONE, quiet_until=NOW + datetime.timedelta(hours=3))
    assert "Сейчас: до 14:00" in _text("quiet", view=view)  # NOW is 11:00 in Paris


def test_quiet_value_other_day_shows_the_date_too():
    view = menu.MenuView(now=NOW, timezone=TIMEZONE, quiet_until=NOW + datetime.timedelta(hours=20))
    assert "Сейчас: до 26.10 07:00" in _text("quiet", view=view)


def test_data_layout_and_the_sensitive_acts_are_telegram_only():
    _, tg = _render("data", web=False)
    assert [[b.callback_data for b in row] for row in tg.inline_keyboard] == [
        ["mn:a:privacy"],
        ["mn:a:export"],
        ["mn:a:delete"],
        ["mn:a:revoke"],
        ["mn:s:main", "mn:x"],
    ]
    _, web = _render("data", web=True)
    assert _keys(web) == ["privacy", "revoke"]
    assert menu.action_kind("export") == menu.CONFIRM
    # /delete has its own two-step card, so it is dispatched at once.
    assert menu.action_kind("delete") == menu.RUN


# --- pure: the action table -----------------------------------------------


def test_action_count_and_kinds():
    assert len(menu.ACTIONS) == 90
    assert menu.KINDS == (menu.RUN, menu.PROMPT, menu.CONFIRM)
    for key, action in menu.ACTIONS.items():
        assert action.kind in menu.KINDS, key
        assert action.label, key
        assert action.style in (None, "success", "danger"), key


def test_every_prompt_key_has_a_prompt_text_and_every_confirm_key_a_card():
    for key, action in menu.ACTIONS.items():
        if action.kind == menu.PROMPT:
            assert menu.prompt_text(key), key
        elif action.kind == menu.CONFIRM:
            assert "?" in menu.confirm_text(key), key
            markup = menu.confirm_markup(key)
            yes, no = markup.inline_keyboard[0]
            assert yes.callback_data == f"mn:y:{key}"
            assert yes.text.startswith("Да")
            assert (no.text, no.callback_data) == ("Нет", "mn:c")
    assert "каждый день" in menu.prompt_text("order_daily")
    assert "по будням" in menu.prompt_text("order_weekdays")
    assert "один раз" in menu.prompt_text("order_once")
    assert "forums" in menu.prompt_text("interests_add_forums")
    assert "ref" in menu.prompt_text("study_ref")


def test_prompt_markup_is_one_cancel_button():
    markup = menu.prompt_markup()
    assert [(b.text, b.callback_data) for b in _buttons(markup)] == [("✕ Отмена", "mn:c")]


def test_confirm_after_input_keys_are_prompts_and_carry_their_argument():
    assert menu.CONFIRM_AFTER_INPUT == {"forget", "claude_connect"}
    for key in menu.CONFIRM_AFTER_INPUT:
        assert menu.action_kind(key) == menu.PROMPT
        assert menu.confirmable(key) is True
    assert menu.confirmable("remember") is False  # a prompt without a card
    assert menu.confirmable("checkin") is False  # a run action
    card = menu.confirm_markup("forget", arg="12", yes_label=menu.FORGET_YES)
    assert card.inline_keyboard[0][0].callback_data == "mn:y:forget:12"
    assert card.inline_keyboard[0][0].text == "Да, забыть"


def test_forget_card_quotes_the_record_and_bounds_it():
    assert menu.forget_confirm_text(12, "не пью кофе") == "Забыть #12 «не пью кофе»?"
    long = menu.forget_confirm_text(3, "слово " * 100)
    assert len(long) < 160 and "…" in long
    assert menu.forget_confirm_text(4, "две\nстроки") == "Забыть #4 «две строки»?"


def test_claude_connect_data_accepts_only_code_shaped_text():
    assert menu.claude_connect_data("ABCD-1234") == "mn:y:claude_connect:ABCD-1234"
    assert menu.claude_connect_data("  abc123  ") == "mn:y:claude_connect:abc123"
    for bad in ("", "   ", "two words", "a" * 17, "кириллица", "code;drop", "a:b"):
        assert menu.claude_connect_data(bad) is None, bad
    assert menu.claude_connect_confirm_text("ABCD-1234") == "Подключить Claude по коду ABCD-1234?"


def test_fits_callback_counts_bytes_not_characters():
    assert menu.fits_callback("a" * 64) is True
    assert menu.fits_callback("a" * 65) is False
    assert menu.fits_callback("я" * 32) is True
    assert menu.fits_callback("я" * 33) is False


# --- TELEGRAM_ONLY: never drawn on the web, refused when forged ----------


def test_telegram_only_covers_everything_the_web_may_not_type():
    for key, action in menu.ACTIONS.items():
        if set(action.commands) & BLOCKED_COMMANDS:
            assert key in menu.TELEGRAM_ONLY, key
    named = {
        "export", "delete", "grok_open", "weblogout", "planner_link", "lens_code_on",
        "claude_connect", "interests_suggest", "hide_kb",
    }
    assert named <= menu.TELEGRAM_ONLY
    assert menu.TELEGRAM_ONLY <= set(menu.ACTIONS)
    # And nothing the web chat legitimately does is in it.
    for key in ("checkin", "state", "remember", "forget", "notes_on", "vault", "quiet_2h", "tz",
                "privacy", "revoke", "interests", "read", "card"):
        assert key not in menu.TELEGRAM_ONLY, key


def test_telegram_only_keys_are_refused_on_the_web_and_allowed_on_telegram():
    for key in menu.TELEGRAM_ONLY:
        assert menu.action_available(key, FULL, web=True) is False, key
        assert menu.action_available(key, FULL, web=False) is True, key


def test_telegram_only_keys_are_never_drawn_on_the_web_for_any_deploy():
    for *flags, web in _flag_combinations():
        if not web:
            continue  # the combinations come in both sinks; this test is the web's
        settings = _combination_settings(*flags)
        for view in _view_samples()[:2]:
            for section in ALL_SECTIONS:
                rendered = menu.render(section, settings, web=True, view=view)
                if rendered is None:
                    continue
                drawn = set(_keys(rendered[1]))
                assert not drawn & menu.TELEGRAM_ONLY, (section, drawn & menu.TELEGRAM_ONLY)
                rich = menu.render_rich(section, settings, web=True, view=view)
                for data in rich_callback_data(rich):
                    press = menu.parse_callback(data)
                    if press.kind in ("action", "input"):
                        assert press.value not in menu.TELEGRAM_ONLY


def test_every_action_is_drawn_somewhere_on_telegram_and_all_but_telegram_only_on_the_web():
    def drawn(web: bool) -> set[str]:
        found: set[str] = set()
        for view in _view_samples():
            for section in ALL_SECTIONS:
                for origin in (None, "settings"):
                    rendered = menu.render(section, FULL, web=web, view=view, origin=origin)
                    if rendered is not None:
                        found |= set(_keys(rendered[1]))
        return found

    assert drawn(web=False) == set(menu.ACTIONS)
    assert drawn(web=True) == set(menu.ACTIONS) - menu.TELEGRAM_ONLY


# --- the coverage test: no command can be forgotten again -----------------


def test_every_bot_command_is_reachable_from_the_menu():
    reachable = {command for action in menu.ACTIONS.values() for command in action.commands}
    commands = {c.command for c in BOT_COMMANDS}
    entry_points = {"start", "menu"}
    missing = commands - entry_points - reachable
    assert not missing, f"no menu action reaches /{', /'.join(sorted(missing))}"
    # /weblogout is not in BOT_COMMANDS (it is added only with the web UI).
    assert "weblogout" in reachable
    # Entry points are entry points, not menu items.
    assert not entry_points & reachable
    # Every command an action names is a real one.
    assert reachable <= commands | {"weblogout"}


def test_every_bot_command_is_reachable_by_a_drawn_button():
    """Not just named by an action: the action is drawn on some section of
    a fully open deploy."""
    drawn_keys: set[str] = set()
    for view in _view_samples():
        for section in ALL_SECTIONS:
            drawn_keys |= set(_keys(_markup(section, view=view)))
    reachable = {c for key in drawn_keys for c in menu.ACTIONS[key].commands}
    commands = {c.command for c in BOT_COMMANDS} - {"start", "menu"}
    assert commands <= reachable


# --- callbacks: parse_callback and Press ------------------------------------


def test_parse_callback():
    assert menu.parse_callback("mn:x") == menu.Press("close", "")
    assert menu.parse_callback("mn:c") == menu.Press("cancel", "")
    assert menu.parse_callback("mn:s:quiet") == menu.Press("section", "quiet")
    assert menu.parse_callback("mn:s:quiet:settings") == menu.Press("section", "quiet", "settings")
    assert menu.parse_callback("mn:a:checkin") == menu.Press("action", "checkin")
    assert menu.parse_callback("mn:a:quiet_2h:settings") == menu.Press("action", "quiet_2h", "settings")
    assert menu.parse_callback("mn:i:remember") == menu.Press("input", "remember")
    assert menu.parse_callback("mn:y:export") == menu.Press("yes", "export")
    assert menu.parse_callback("mn:y:forget:12") == menu.Press("yes", "forget", "12")
    assert menu.parse_callback("mn:y:claude_connect:AB-12") == menu.Press(
        "yes", "claude_connect", "AB-12"
    )


@pytest.mark.parametrize(
    "data", ["m:k:identity:1", "garbage", "", "mn:", "mn:s:", "mn:a:", "mn:y:", "mn:z:foo", "x:mn:a:checkin"]
)
def test_parse_callback_rejects_what_is_not_ours(data):
    assert menu.parse_callback(data) is None


def test_press_is_a_plain_value():
    press = menu.Press("action", "checkin")
    assert (press.kind, press.value, press.arg) == ("action", "checkin", None)
    assert press == menu.Press("action", "checkin", None)
    assert press != menu.Press("input", "checkin")


def test_yes_data_round_trips_through_parse_callback():
    assert menu.yes_data("export") == "mn:y:export"
    assert menu.parse_callback(menu.yes_data("forget", "42")) == menu.Press("yes", "forget", "42")


def test_every_rendered_callback_round_trips_to_the_press_it_names():
    for section in ALL_SECTIONS:
        for data in _data(_markup(section, view=_view_samples()[1])):
            press = menu.parse_callback(data)
            assert press is not None
            if press.kind == "section":
                assert press.value in menu.SECTIONS
            elif press.kind in ("action", "input"):
                assert press.value in menu.ACTIONS


# --- other pure bits --------------------------------------------------------


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


def test_status_texts_the_flows_edit_prompts_to():
    assert menu.DONE_TEXT == "Готово."
    assert menu.CANCELLED_TEXT == "Отменено."


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


def _build(sessionmaker, settings=None, llm=None, bot=None, fake=None, clock=None):
    if bot is None:
        bot, fake = make_bot()
    dp = Dispatcher()
    dp.include_router(
        build_router(
            sessionmaker,
            settings or Settings(_env_file=None, TZ_DEFAULT=TIMEZONE),
            llm or FakeLLMProvider(),
            clock=clock,
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


async def test_every_section_opens_from_a_press_and_its_back_button_returns(sessionmaker):
    settings = _settings(TZ_DEFAULT=TIMEZONE)
    await _seed(sessionmaker, *range(1, 40))
    dp, bot, fake = _build(sessionmaker, settings=settings)
    await _feed(dp, bot, _command_update(1, "/menu"))
    update_id = 2
    for section in ALL_SECTIONS:
        if section == menu.MAIN_SECTION:
            continue
        await _feed(dp, bot, _callback_update(update_id, f"mn:s:{section}", message_id=1))
        update_id += 1
        flat = flatten_rich_message(fake.edits[-1].rich_message)
        assert flat.startswith(TITLE[section]), section
        back = rich_callback_data(fake.edits[-1].rich_message)[-2]
        assert back == f"mn:s:{PARENT[section]}"
    assert len(fake.edits) == len(ALL_SECTIONS) - 1


async def test_unknown_section_answers_stale_and_edits_nothing(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:s:nope", message_id=1))

    assert fake.edits == []
    assert len(fake.answered) == 1
    assert fake.answered[0].text is not None


async def test_a_section_whose_feature_is_off_answers_stale(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build(sessionmaker)  # research, vault, planner all off
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:s:research", message_id=1))
    await _feed(dp, bot, _callback_update(3, "mn:s:knowledge", message_id=1))

    assert fake.edits == []
    assert [a.text for a in fake.answered] == ["Устарело.", "Устарело."]


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
    # sent after /menu's own -- "state" has no `refresh`, so the menu
    # card itself is never edited.
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


async def test_the_web_menu_never_offers_a_telegram_only_button(sessionmaker):
    await _seed(sessionmaker, *range(1, 40))
    bot, fake = make_bot()
    bot.is_web_sink = True
    dp, bot, fake = _build(sessionmaker, settings=_settings(TZ_DEFAULT=TIMEZONE), bot=bot, fake=fake)
    await _feed(dp, bot, _command_update(1, "/menu"))
    shown = [fake.sent[0].reply_markup]
    update_id = 2
    on_web = [
        s for s in ALL_SECTIONS
        if s != menu.MAIN_SECTION and menu.section_exists(s, FULL, web=True)
    ]
    for section in ALL_SECTIONS:
        if section == menu.MAIN_SECTION:
            continue
        await _feed(dp, bot, _callback_update(update_id, f"mn:s:{section}", message_id=1))
        update_id += 1
        if fake.edits:
            shown.append(fake.edits[-1].reply_markup)
    keys = {key for markup in shown if markup for key in _keys(markup)}
    assert keys and not keys & menu.TELEGRAM_ONLY
    # lens and claude do not exist on the web: a forged press is stale.
    assert len(fake.edits) == len(on_web)
    assert len(fake.answered) == len(ALL_SECTIONS) - 1
    assert sum(1 for a in fake.answered if a.text == "Устарело.") == 2


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


# --- the knowledge section (ex-vault) through the router ------------------


def _vault_settings(**extra) -> Settings:
    return Settings(
        _env_file=None, VAULT_MODE="status", VAULT_API_TOKEN="x" * 32, TZ_DEFAULT=TIMEZONE, **extra
    )


async def test_knowledge_section_shows_status_table_and_notes_toggle(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build(sessionmaker, settings=_vault_settings())
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:s:knowledge", message_id=1))

    flat = flatten_rich_message(fake.edits[-1].rich_message)
    assert flat.startswith(menu.KNOWLEDGE_TITLE)
    assert "Режим: status" in flat
    assert "Заметки: выкл" in flat
    assert f"{menu.ACTION_PREFIX}notes_on" in rich_callback_data(fake.edits[-1].rich_message)


async def test_notes_on_action_sets_consent_and_rerenders_the_knowledge_card(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build(sessionmaker, settings=_vault_settings())
    await _feed(dp, bot, _command_update(1, "/menu"))
    await _feed(dp, bot, _callback_update(2, "mn:s:knowledge", message_id=1))

    await _feed(dp, bot, _callback_update(3, "mn:a:notes_on", message_id=1))

    state = await _state(sessionmaker)
    assert state.notes_consent is True
    # The card re-renders in place (same message_id=1), on top of
    # /vault's own confirmation reply.
    assert any(m.text for m in fake.sent)  # the confirmation reply
    last_edit = fake.edits[-1]
    assert last_edit.rich_message is not None
    flat = flatten_rich_message(last_edit.rich_message)
    assert flat.startswith(menu.KNOWLEDGE_TITLE)
    assert "Заметки: вкл" in flat
    assert f"{menu.ACTION_PREFIX}notes_off" in rich_callback_data(last_edit.rich_message)


async def test_notes_off_action_clears_consent_and_rerenders(sessionmaker):
    await _seed(sessionmaker, 1, 2, notes_consent=True)
    dp, bot, fake = _build(sessionmaker, settings=_vault_settings())
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:notes_off", message_id=1))

    state = await _state(sessionmaker)
    assert state.notes_consent is False
    flat = flatten_rich_message(fake.edits[-1].rich_message)
    assert "Заметки: выкл" in flat


# --- 8f: the router wiring that fetches the roots hint for the menu -------


async def test_knowledge_card_shows_the_roots_hint_when_notes_consent_is_on(sessionmaker):
    """The router's `_menu_view` fetches vaultd's manifest -- same request
    /vault itself makes -- only while notes consent is on, and hands the
    resulting settings state + roots to the knowledge section."""
    stub, server = await start_stub()
    try:
        stub.respond(
            "GET",
            "/v1/manifest",
            200,
            {"files": [], "summary": {
                "conflict": 0, "legacy_read": 0, "unknown_value": 0,
                "settings": "wrong_case", "knowledge_roots": [],
            }},
        )
        await _seed(sessionmaker, 1, 2, notes_consent=True)
        dp, bot, fake = _build(sessionmaker, settings=_vault_settings(VAULT_URL=stub.url))
        await _feed(dp, bot, _command_update(1, "/menu"))

        await _feed(dp, bot, _callback_update(2, "mn:s:knowledge", message_id=1))

        flat = flatten_rich_message(fake.edits[-1].rich_message)
        assert "переименуй его в Anchor/settings.md (регистр важен)" in flat
        assert ("GET", "/v1/manifest") in stub.calls()
    finally:
        await server.close()


async def test_knowledge_card_roots_never_appear_in_logs(sessionmaker, caplog, monkeypatch):
    for name in ("app.tg.router", "app.vault.status", "app.vault.client"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)
    stub, server = await start_stub()
    try:
        secret_root = "Секретная-папка-с-планами"
        stub.respond(
            "GET",
            "/v1/manifest",
            200,
            {"files": [], "summary": {
                "conflict": 0, "legacy_read": 0, "unknown_value": 0,
                "settings": "valid", "knowledge_roots": [secret_root],
            }},
        )
        await _seed(sessionmaker, 1, 2, notes_consent=True)
        dp, bot, fake = _build(sessionmaker, settings=_vault_settings(VAULT_URL=stub.url))
        await _feed(dp, bot, _command_update(1, "/menu"))

        with caplog.at_level(logging.DEBUG):
            await _feed(dp, bot, _callback_update(2, "mn:s:knowledge", message_id=1))

        flat = flatten_rich_message(fake.edits[-1].rich_message)
        assert secret_root in flat  # the menu card is exactly where it belongs
        blob = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
        assert secret_root not in blob
    finally:
        await server.close()


async def test_knowledge_card_makes_no_manifest_request_when_notes_consent_is_off(sessionmaker):
    stub, server = await start_stub()
    try:
        await _seed(sessionmaker, 1, 2)
        dp, bot, fake = _build(sessionmaker, settings=_vault_settings(VAULT_URL=stub.url))
        await _feed(dp, bot, _command_update(1, "/menu"))

        await _feed(dp, bot, _callback_update(2, "mn:s:knowledge", message_id=1))

        flat = flatten_rich_message(fake.edits[-1].rich_message)
        assert "Корни знаний" not in flat
        assert "Anchor/settings.md" not in flat
        assert ("GET", "/v1/manifest") not in stub.calls()
    finally:
        await server.close()


# --- the Claude section through the router -------------------------------


async def _connect_claude(sessionmaker, *, library_read: bool) -> None:
    async with sessionmaker() as session:
        now = datetime.datetime.now(datetime.timezone.utc)
        session.add(
            OauthConnection(
                client_id="test",
                created_at=now,
                expires_at=now + datetime.timedelta(days=30),
                library_read=library_read,
            )
        )
        await session.commit()


def _claude_settings() -> Settings:
    return _vault_settings(CLAUDE_ACCESS_ENABLED=True)


async def test_lib_write_on_action_sets_the_write_switch_and_rerenders_the_claude_card(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    await _connect_claude(sessionmaker, library_read=True)
    dp, bot, fake = _build(sessionmaker, settings=_claude_settings())
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:lib_write_on", message_id=1))

    async with sessionmaker() as session:
        connection = await session.get(OauthConnection, 1)
    assert connection.library_write is True
    flat = flatten_rich_message(fake.edits[-1].rich_message)
    assert flat.startswith(menu.CLAUDE_TITLE)
    assert "Библиотека: чтение+запись" in flat
    assert f"{menu.ACTION_PREFIX}lib_write_off" in rich_callback_data(fake.edits[-1].rich_message)


async def test_lib_read_on_action_sets_read_and_rerenders_the_claude_card(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    await _connect_claude(sessionmaker, library_read=False)
    dp, bot, fake = _build(sessionmaker, settings=_claude_settings())
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:lib_read_on", message_id=1))

    async with sessionmaker() as session:
        connection = await session.get(OauthConnection, 1)
    assert connection.library_read is True
    flat = flatten_rich_message(fake.edits[-1].rich_message)
    assert "Библиотека: чтение" in flat
    assert f"{menu.ACTION_PREFIX}lib_read_off" in rich_callback_data(fake.edits[-1].rich_message)


async def test_forged_lib_write_on_web_sink_does_nothing(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    bot, fake = make_bot()
    bot.is_web_sink = True
    dp, bot, fake = _build(sessionmaker, settings=_claude_settings(), bot=bot, fake=fake)
    await _feed(dp, bot, _command_update(1, "/menu"))
    sent_before = len(fake.sent)

    await _feed(dp, bot, _callback_update(2, "mn:a:lib_write_on", message_id=1))

    async with sessionmaker() as session:
        assert await session.get(OauthConnection, 1) is None
    assert len(fake.sent) == sent_before
    assert fake.answered[-1].text is not None


# --- router-level: the switches (intensity, focus/pause, quiet, planner) --


async def test_int_4_press_sets_intensity_and_rerenders_settings_in_place(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:int_4", message_id=1))

    assert (await _state(sessionmaker)).intensity == 4
    assert len(fake.edits) == 1
    flat = flatten_rich_message(fake.edits[-1].rich_message)
    assert flat.startswith(menu.SETTINGS_TITLE)
    assert "Интенсивность: 4 ·" in flat


async def test_focus_on_press_rerenders_today_in_place(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:focus_on", message_id=1))

    assert (await _state(sessionmaker)).focus_on is True
    flat = flatten_rich_message(fake.edits[-1].rich_message)
    assert flat.startswith(menu.TODAY_TITLE)
    assert "Фокус: вкл" in flat
    assert f"{menu.ACTION_PREFIX}focus_off" in rich_callback_data(fake.edits[-1].rich_message)


async def test_pause_press_rerenders_settings_with_the_way_back(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:out", message_id=1))

    assert (await _state(sessionmaker)).persona_active is False
    data = rich_callback_data(fake.edits[-1].rich_message)
    assert f"{menu.ACTION_PREFIX}in" in data and f"{menu.ACTION_PREFIX}out" not in data


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
    assert flat.startswith(menu.PLANNER_TITLE)
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

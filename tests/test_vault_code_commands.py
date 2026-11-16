"""`/vault code` in Telegram (anchor-code-window-plan.md section 4.3, CW1).

The status reply, the card (count, roots, lifetime, a root that matched
nothing), its «Открыть» / «Отмена» buttons (stale after CONFIRM_TTL,
replayed, forged), `/vault code off`, and the replies when vaultd is
down, its switch off or Anchor/settings.md unusable -- with a fake
vaultd (tests/code_vault_fake.py), never the real service.
"""

from __future__ import annotations

import datetime

import pytest
from code_vault_fake import FakeCodeVault
from conftest import make_bot

from app.config import Settings
from app.core.clock import FrozenClock
from app.db.models import UserState
from app.tg import vault as vault_ui
from app.tg.data import CONFIRM_TTL
from app.tg.router import BOT_COMMANDS
from app.vault import code_access

NOW = datetime.datetime(2026, 11, 16, 12, 0, tzinfo=datetime.UTC)
ON = Settings(CODE_ACCESS_ENABLED=True, VAULT_MODE="status", CODE_FILES_PER_WINDOW=50)


@pytest.fixture
async def seeded(sessionmaker):
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, timezone="UTC"))
        await session.commit()


async def _command(sessionmaker, args, fake=None, settings=ON, clock=None):
    fake = fake or FakeCodeVault()
    return await vault_ui.code_command(sessionmaker, settings, clock or FrozenClock(NOW), args, fake.factory)


def _buttons(keyboard) -> list[str]:
    return [b.callback_data for row in keyboard.inline_keyboard for b in row]


async def _press(sessionmaker, data, clock, settings=ON):
    bot, tg = make_bot()
    await vault_ui.handle_code_callback(
        sessionmaker, bot, settings, clock, callback_id="cb", chat_id=555, message_id=7, data=data
    )
    await bot.session.close()
    assert len(tg.answered) == 1
    return tg.edits[-1].text


async def test_off_says_so_and_makes_no_request(sessionmaker, seeded):
    fake = FakeCodeVault()
    for args in ("", " on 6h Notes", " off"):
        text, keyboard = await _command(sessionmaker, args, fake, settings=Settings())
        assert (text, keyboard) == (vault_ui.CODE_DISABLED, None)
    assert fake.calls == []


async def test_status_before_any_window(sessionmaker, seeded):
    text, keyboard = await _command(sessionmaker, "")
    assert keyboard is None
    assert text.splitlines() == [
        vault_ui.CODE_ENABLED_LINE, vault_ui.CODE_VAULT_ON_LINE, vault_ui.CODE_NEVER_LINE,
    ]


async def test_status_names_vaultd_switch_and_failures(sessionmaker, seeded):
    fake = FakeCodeVault()
    fake.code_access = False
    assert vault_ui.CODE_VAULT_OFF_LINE in (await _command(sessionmaker, "", fake))[0]
    fake.status_error = "unavailable"
    assert vault_ui.CODE_VAULT_UNREACHABLE_LINE in (await _command(sessionmaker, "", fake))[0]
    text, _ = await _command(sessionmaker, "", fake, settings=Settings(CODE_ACCESS_ENABLED=True))
    assert vault_ui.CODE_VAULT_MODE_OFF_LINE in text


async def test_the_whole_flow(sessionmaker, seeded):
    clock = FrozenClock(NOW)
    fake = FakeCodeVault()
    text, keyboard = await _command(sessionmaker, "on 6h Notes; Lens", fake, clock=clock)
    assert "Заметок откроется: 3." in text
    assert "Папки: Notes; Lens" in text
    assert "Срок: 6 ч" in text
    assert "не нашлось" not in text
    assert fake.calls == [("status",), ("tree", ["Notes", "Lens"])]
    open_data, cancel_data = _buttons(keyboard)
    assert open_data.startswith("vc:o:") and cancel_data.startswith("vc:x:")
    # Nothing is open until «Открыть».
    async with sessionmaker() as session:
        assert await code_access.current(session, clock) is None

    clock.advance(datetime.timedelta(seconds=30))
    edited = await _press(sessionmaker, open_data, clock)
    assert edited == vault_ui.CODE_OPENED.format(until="16.11 18:00", roots="Notes; Lens")
    async with sessionmaker() as session:
        window = await code_access.current(session, clock)
    assert window.roots == ("Notes", "Lens") and window.ttl_s == 6 * 3600
    # A replay opens nothing new.
    assert await _press(sessionmaker, open_data, clock) == vault_ui.CODE_STALE

    status, _ = await _command(sessionmaker, "", fake, clock=clock)
    assert "Окно открыто до 18:00: Notes; Lens. Прочитано файлов: 0 из 50." in status

    assert (await _command(sessionmaker, "off", fake, clock=clock))[0] == vault_ui.CODE_CLOSED
    assert (await _command(sessionmaker, "off", fake, clock=clock))[0] == vault_ui.CODE_NOTHING_OPEN
    status, _ = await _command(sessionmaker, "", fake, clock=clock)
    assert status.splitlines()[-1] == "Окно закрыто. Последнее закрылось 12:00 (командой)."


async def test_default_is_one_hour_and_the_whole_vault(sessionmaker, seeded):
    fake = FakeCodeVault()
    text, _ = await _command(sessionmaker, "on", fake)
    assert f"Заметок откроется: {len(fake.notes)}." in text
    assert "Папки: весь vault (кроме Anchor/ и never)" in text
    assert "Срок: 1 ч" in text
    assert fake.calls[-1] == ("tree", [])


async def test_the_card_says_when_a_root_matched_nothing(sessionmaker, seeded):
    text, _ = await _command(sessionmaker, "on Notes; Заметки; Lens")
    assert vault_ui.CODE_CARD_EMPTY_ROOTS.format(roots="Заметки") in text


async def test_a_truncated_tree_asks_an_empty_looking_root_alone(sessionmaker, seeded):
    fake = FakeCodeVault()
    fake.truncate_at = 1
    text, _ = await _command(sessionmaker, "on Lens; Notes", fake)
    assert "список обрезан" in text
    assert "не нашлось" not in text
    assert ("tree", ["Notes"]) in fake.calls


async def test_cancel(sessionmaker, seeded):
    clock = FrozenClock(NOW)
    _, keyboard = await _command(sessionmaker, "on", clock=clock)
    open_data, cancel_data = _buttons(keyboard)
    assert await _press(sessionmaker, cancel_data, clock) == vault_ui.CODE_CANCELLED
    assert await _press(sessionmaker, open_data, clock) == vault_ui.CODE_STALE
    async with sessionmaker() as session:
        assert await code_access.current(session, clock) is None


async def test_a_button_older_than_confirm_ttl_is_stale(sessionmaker, seeded):
    clock = FrozenClock(NOW)
    _, keyboard = await _command(sessionmaker, "on", clock=clock)
    clock.advance(datetime.timedelta(seconds=CONFIRM_TTL + 1))
    assert await _press(sessionmaker, _buttons(keyboard)[0], clock) == vault_ui.CODE_STALE
    async with sessionmaker() as session:
        assert await code_access.current(session, clock) is None


@pytest.mark.parametrize("data", ["vc:o:1", "vc:o:x:1", "vc:z:1:1", "vc:o:1:99"])
async def test_a_forged_press_opens_nothing(sessionmaker, seeded, data):
    clock = FrozenClock(NOW)
    await _command(sessionmaker, "on", clock=clock)
    assert await _press(sessionmaker, data, clock) == vault_ui.CODE_STALE
    async with sessionmaker() as session:
        assert await code_access.current(session, clock) is None


async def test_a_press_after_the_flag_went_off_opens_nothing(sessionmaker, seeded):
    clock = FrozenClock(NOW)
    _, keyboard = await _command(sessionmaker, "on", clock=clock)
    assert await _press(sessionmaker, _buttons(keyboard)[0], clock, Settings()) == vault_ui.CODE_STALE


async def test_a_second_window_replaces_the_first(sessionmaker, seeded):
    clock = FrozenClock(NOW)
    _, first = await _command(sessionmaker, "on Notes", clock=clock)
    await _press(sessionmaker, _buttons(first)[0], clock)
    clock.advance(datetime.timedelta(seconds=1))
    _, second = await _command(sessionmaker, "on 24h", clock=clock)
    await _press(sessionmaker, _buttons(second)[0], clock)
    async with sessionmaker() as session:
        window = await code_access.current(session, clock)
        last = await code_access.last_closed(session)
    assert window.roots == () and window.ttl_s == 86400
    assert last.closed_reason == "replaced"


@pytest.mark.parametrize(
    "setup,reply",
    [
        (lambda f: setattr(f, "status_error", "unavailable"), vault_ui.CODE_REFUSED_UNREACHABLE),
        (lambda f: setattr(f, "status_error", "unauthorized"), vault_ui.CODE_REFUSED_UNAUTHORIZED),
        (lambda f: setattr(f, "code_access", False), vault_ui.CODE_REFUSED_SWITCH),
        (lambda f: setattr(f, "error", "not_found"), vault_ui.CODE_REFUSED_SWITCH),
        (lambda f: setattr(f, "error", "conflict"), vault_ui.CODE_REFUSED_SETTINGS),
        (lambda f: setattr(f, "error", "unavailable"), vault_ui.CODE_REFUSED_UNREACHABLE),
    ],
)
async def test_no_card_when_vaultd_cannot_serve(sessionmaker, seeded, setup, reply):
    fake = FakeCodeVault()
    setup(fake)
    assert await _command(sessionmaker, "on", fake) == (reply, None)
    async with sessionmaker() as session:
        assert await code_access.pending(session, 1) is None


async def test_no_card_when_the_vault_is_off(sessionmaker, seeded):
    fake = FakeCodeVault()
    reply = await _command(sessionmaker, "on", fake, settings=Settings(CODE_ACCESS_ENABLED=True))
    assert reply == (vault_ui.CODE_REFUSED_VAULT_MODE, None)
    assert fake.calls == []


@pytest.mark.parametrize("args", ["on Anchor", "on 6h anchor/Memory", "on 2h", "off now", "maybe"])
async def test_bad_arguments_make_no_request(sessionmaker, seeded, args):
    fake = FakeCodeVault()
    text, keyboard = await _command(sessionmaker, args, fake)
    assert keyboard is None and text
    assert fake.calls == []


async def test_the_card_never_names_a_note(sessionmaker, seeded):
    text, _ = await _command(sessionmaker, "on")
    for path in FakeCodeVault().notes:
        assert path not in text
        assert path.rsplit("/", 1)[-1].removesuffix(".md") not in text


def test_vault_stays_a_registered_command():
    assert "vault" in {c.command for c in BOT_COMMANDS}
    assert "/vault code" in vault_ui.VAULT_USAGE

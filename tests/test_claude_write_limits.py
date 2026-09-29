"""Claude's write caps as settings: the core, the enforcement, `/claude
limits`, POST /api/state/claude-limits, and the push to vaultd.
"""

from __future__ import annotations

import datetime
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from app.core import claude_write_limits as limits
from app.core.clock import FrozenClock
from app.db.models import ClaudeWriteLimit, VaultStatus
from app.vault import errors
from app.vault.errors import VaultError
from app.web import claude_write
from app.web.claude_write import Refused
from claude_helpers import World, settings
from conftest import make_bot
from tests.claude_write_fake import FakeKnowledgeVault
from tests.test_claude_write import _connection
from tests.test_web_panel_state import _build_app, _get, _log_in, _post, _seed
from tests.test_web_panel_state import _settings as _web_settings

pytestmark = pytest.mark.asyncio

START = datetime.datetime(2026, 9, 29, 10, 0, tzinfo=datetime.timezone.utc)


class _LimitsVault:
    """Just `/v1/limits`: holds vaultd's copy, records every PUT, or fails."""

    def __init__(self, *, down: bool = False) -> None:
        self.sent: list[dict] = []
        self.gets = 0
        self.down = down
        self.held = {k: limits.DEFAULTS.as_dict()[k] for k in limits.VAULT_KEYS}
        self.during_put = None  # an async hook run while a PUT is "in flight"

    def __call__(self, _settings) -> "_LimitsVault":
        return self

    async def get_limits(self) -> dict:
        if self.down:
            raise VaultError(errors.UNAVAILABLE)
        self.gets += 1
        return dict(self.held)

    async def put_limits(self, values: dict) -> dict:
        if self.down:
            raise VaultError(errors.UNAVAILABLE)
        self.sent.append(values)
        if self.during_put is not None:
            hook, self.during_put = self.during_put, None
            await hook()
        self.held = dict(values)
        return dict(values)


async def _pending(sessionmaker) -> bool:
    async with sessionmaker() as session:
        return await limits.push_pending(session)


# --- the core ----------------------------------------------------------------


async def test_effective_is_the_defaults_with_no_rows(sessionmaker):
    async with sessionmaker() as session:
        assert await limits.effective(session) == limits.DEFAULTS
    assert limits.DEFAULTS.creates_per_day == limits.CREATES_PER_DAY


async def test_set_and_reset_one(sessionmaker):
    clock = FrozenClock(START)
    async with sessionmaker() as session:
        new = await limits.set_limit(session, clock, "creates_per_day", 100)
        assert new.creates_per_day == 100
        assert (await limits.effective(session)).creates_per_day == 100
        new = await limits.set_limit(session, clock, "creates_per_day", None)
        assert new.creates_per_day == limits.CREATES_PER_DAY
        assert (await session.execute(select(ClaudeWriteLimit))).first() is None


@pytest.mark.parametrize(
    "key,value,code",
    [
        ("nope", 1, "unknown_key"),
        ("files_per_changeset", 0, "out_of_range"),
        ("files_per_changeset", 201, "out_of_range"),
        ("creates_per_day", -1, "out_of_range"),
        ("creates_per_day", True, "out_of_range"),
    ],
)
async def test_set_limit_refuses_without_writing(sessionmaker, key, value, code):
    async with sessionmaker() as session:
        with pytest.raises(limits.LimitError) as exc:
            await limits.set_limit(session, FrozenClock(START), key, value)
        assert exc.value.code == code
        assert (await session.execute(select(ClaudeWriteLimit))).first() is None


async def test_an_out_of_bounds_row_is_clamped(sessionmaker):
    async with sessionmaker() as session:
        session.add(ClaudeWriteLimit(name="files_per_changeset", value=10_000, updated_at=START))
        session.add(ClaudeWriteLimit(name="retired_cap", value=3, updated_at=START))
        await session.commit()
        got = await limits.effective(session)
    assert got.files_per_changeset == limits.SPECS["files_per_changeset"].max


async def test_reset_all(sessionmaker):
    clock = FrozenClock(START)
    async with sessionmaker() as session:
        await limits.set_limit(session, clock, "creates_per_day", 1)
        await limits.set_limit(session, clock, "moves_per_day", 1)
        assert await limits.reset_all(session) == limits.DEFAULTS
        assert await limits.effective(session) == limits.DEFAULTS


def test_parse_value():
    assert limits.parse_value("creates_per_day", "12") == 12
    assert limits.parse_value("creates_per_day", "12k") is None
    assert limits.parse_value("bytes_per_day", "512k") == 512 * 1024
    assert limits.parse_value("bytes_per_day", "2M") == 2 * 1024 * 1024
    assert limits.parse_value("bytes_per_day", "-1") is None
    # Typed in KB, like it is shown.
    assert limits.parse_value("bytes_per_day", "512") == 512 * 1024
    # `str.isdigit` accepts these; `int` would not.
    assert limits.parse_value("creates_per_day", "²") is None
    assert limits.parse_value("creates_per_day", "٣") is None


def test_bytes_per_day_must_be_whole_kb():
    limits.check_value("bytes_per_day", 1024)
    with pytest.raises(limits.LimitError):
        limits.check_value("bytes_per_day", 1500)


# --- the push to vaultd ----------------------------------------------------------


async def test_a_vault_cap_change_is_pushed_in_full(sessionmaker):
    vault = _LimitsVault()
    cfg = settings(VAULT_MODE="mirror")
    async with sessionmaker() as session:
        _new, pushed = await limits.set_and_push(
            session, cfg, FrozenClock(START), "moves_per_day", 7, vault
        )
    assert pushed is True
    assert vault.sent == [{**{k: limits.DEFAULTS.as_dict()[k] for k in limits.VAULT_KEYS}, "moves_per_day": 7}]
    assert not await _pending(sessionmaker)


async def test_a_bot_only_cap_is_not_pushed(sessionmaker):
    vault = _LimitsVault()
    async with sessionmaker() as session:
        _new, pushed = await limits.set_and_push(
            session, settings(VAULT_MODE="mirror"), FrozenClock(START), "creates_per_day", 7, vault
        )
    assert pushed is None
    assert vault.sent == []
    assert not await _pending(sessionmaker)


async def test_a_failed_push_stays_pending_until_the_sync_pass(sessionmaker):
    vault = _LimitsVault(down=True)
    async with sessionmaker() as session:
        _new, pushed = await limits.set_and_push(
            session, settings(VAULT_MODE="mirror"), FrozenClock(START), "undos_per_hour", 1, vault
        )
    assert pushed is False
    assert await _pending(sessionmaker)
    vault.down = False
    async with sessionmaker() as session:
        assert await limits.push(session, vault) is True
    assert vault.sent[-1]["undos_per_hour"] == 1
    assert not await _pending(sessionmaker)


async def test_vault_off_saves_and_leaves_the_push_pending(sessionmaker):
    vault = _LimitsVault()
    async with sessionmaker() as session:
        _new, pushed = await limits.set_and_push(
            session, settings(VAULT_MODE="off"), FrozenClock(START), "undos_per_hour", 1, vault
        )
    assert pushed is None
    assert vault.sent == []
    assert await _pending(sessionmaker)


async def _seed_state(sessionmaker, *, pending: bool = False) -> None:
    from app.db.models import UserState

    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=1, timezone="Europe/Paris"))
        session.add(VaultStatus(id=1, limits_push_pending=pending))
        await session.commit()


async def test_the_vault_sync_pass_retries_a_pending_push(sessionmaker):
    from app.vault.sync import run_vault_sync
    from tests.vault_fake import FakeVault

    await _seed_state(sessionmaker, pending=True)
    async with sessionmaker() as session:
        session.add(ClaudeWriteLimit(name="moves_per_day", value=9, updated_at=START))
        await session.commit()
    fake = FakeVault()
    async with sessionmaker() as session:
        await run_vault_sync(session, settings(VAULT_MODE="mirror"), FrozenClock(START), fake)
    assert fake.limit_puts and fake.limit_puts[-1]["moves_per_day"] == 9
    assert not await _pending(sessionmaker)


async def test_the_sync_pass_heals_a_vault_that_lost_its_copy(sessionmaker):
    """Nothing pending, but vaultd reads as the defaults (a lost or
    unreadable limits.json, or a /v1/purge that ran after the change):
    the sync pass notices the difference and pushes again."""
    from app.vault.sync import run_vault_sync
    from tests.vault_fake import FakeVault

    await _seed_state(sessionmaker, pending=False)
    async with sessionmaker() as session:
        session.add(ClaudeWriteLimit(name="undos_per_hour", value=2, updated_at=START))
        await session.commit()
    fake = FakeVault()
    async with sessionmaker() as session:
        await run_vault_sync(session, settings(VAULT_MODE="mirror"), FrozenClock(START), fake)
    assert fake.limits["undos_per_hour"] == 2


async def test_reconcile_sends_nothing_when_the_copies_match(sessionmaker):
    vault = _LimitsVault()
    async with sessionmaker() as session:
        assert await limits.reconcile(session, vault) is True
    assert vault.sent == [] and vault.gets == 1


async def test_a_change_during_a_push_stays_pending(sessionmaker):
    """The review's race: a push of the old values lands after the user
    set a newer one. The pending mark must survive, and the next
    reconcile must send the newer value."""
    clock = FrozenClock(START)
    vault = _LimitsVault()
    async with sessionmaker() as session:
        await limits.set_limit(session, clock, "moves_per_day", 5)

    async def change_meanwhile():
        async with sessionmaker() as other:
            await limits.set_limit(other, clock, "moves_per_day", 6)

    vault.during_put = change_meanwhile
    async with sessionmaker() as session:
        assert await limits.push(session, vault) is True
    assert vault.held["moves_per_day"] == 5
    assert await _pending(sessionmaker)
    async with sessionmaker() as session:
        assert await limits.reconcile(session, vault) is True
    assert vault.held["moves_per_day"] == 6
    assert not await _pending(sessionmaker)


async def test_status_mode_probe_retries_a_pending_push(sessionmaker):
    """The sync pass does not run in `status` mode, so the probe that
    /state and /vault make retries a pending change instead."""
    from app.vault import status as vault_status
    from tests.vault_fake import FakeVault

    await _seed_state(sessionmaker, pending=True)
    async with sessionmaker() as session:
        session.add(ClaudeWriteLimit(name="folders_per_day", value=1, updated_at=START))
        await session.commit()
    fake = FakeVault()
    async with sessionmaker() as session:
        await vault_status.probe(session, settings(VAULT_MODE="status"), FrozenClock(START), fake)
    assert fake.limits["folders_per_day"] == 1
    assert not await _pending(sessionmaker)


# --- enforcement uses the overrides ---------------------------------------------


async def test_a_lowered_creates_cap_refuses_sooner(sessionmaker):
    clock = FrozenClock(START)
    connection = await _connection(sessionmaker)
    vault = FakeKnowledgeVault()
    async with sessionmaker() as session:
        await limits.set_limit(session, clock, "creates_per_day", 1)
        await claude_write.create_note(session, clock, vault, connection.id, "Library", "A", "x")
        with pytest.raises(Refused) as exc:
            await claude_write.create_note(session, clock, vault, connection.id, "Library", "B", "x")
    assert exc.value.code == "cap_creates"


async def test_a_raised_files_cap_allows_more_than_the_default(sessionmaker):
    clock = FrozenClock(START)
    connection = await _connection(sessionmaker)
    vault = FakeKnowledgeVault()
    async with sessionmaker() as session:
        await limits.set_limit(session, clock, "files_per_changeset", limits.FILES_PER_CHANGESET + 2)
        await limits.set_limit(session, clock, "creates_per_day", 100)
        for i in range(limits.FILES_PER_CHANGESET + 2):
            await claude_write.create_note(session, clock, vault, connection.id, "Library", f"N{i}", "x")
        with pytest.raises(Refused) as exc:
            await claude_write.create_note(session, clock, vault, connection.id, "Library", "Over", "x")
    assert exc.value.code == "cap_files"


async def test_zero_undos_per_hour_refuses_undo(sessionmaker):
    clock = FrozenClock(START)
    connection = await _connection(sessionmaker)
    vault = FakeKnowledgeVault()
    async with sessionmaker() as session:
        result = await claude_write.create_note(session, clock, vault, connection.id, "Library", "A", "x")
        await limits.set_limit(session, clock, "undos_per_hour", 0)
        with pytest.raises(Refused) as exc:
            await claude_write.undo_changeset(session, clock, vault, connection.id, result["changeset_id"])
    assert exc.value.code == "cap_undos"


# --- Telegram: /claude limits ----------------------------------------------------


async def test_claude_limits_lists_every_cap(sessionmaker):
    world = World(sessionmaker, settings())
    await world.seed()
    text = await world.command("/claude limits")
    for key in limits.SPECS:
        assert key in text
    assert "512 КБ" in text


async def test_claude_limits_set_reset_and_refuse(sessionmaker):
    world = World(sessionmaker, settings())
    await world.seed()
    assert "теперь 60" in await world.command("/claude limits creates_per_day 60")
    assert "по умолчанию 40" in await world.command("/claude limits")
    assert "теперь 1024 КБ" in await world.command("/claude limits bytes_per_day 1m")
    assert "допустимо от 1 до 200" in await world.command("/claude limits files_per_changeset 0")
    assert "Нет такого лимита" in await world.command("/claude limits nope 3")
    assert "снова 40" in await world.command("/claude limits creates_per_day reset")
    await world.command("/claude limits moves_per_day 1")
    assert "по умолчанию" in await world.command("/claude limits Reset")
    assert "допустимо" in await world.command("/claude limits creates_per_day ²")
    async with sessionmaker() as session:
        assert await limits.effective(session) == limits.DEFAULTS


# --- Telegram: the +/- keyboard -------------------------------------------------


def _buttons(markup) -> dict[str, str]:
    return {b.callback_data: b.text for row in markup.inline_keyboard for b in row}


async def test_claude_limits_carries_a_keyboard(sessionmaker):
    world = World(sessionmaker, settings())
    await world.seed()
    await world.command("/claude limits")
    buttons = _buttons(world.tg_fake.sent[-1].reply_markup)
    assert buttons["cw:s:creates_per_day:50"] == "➕ 50"
    assert buttons["cw:s:creates_per_day:30"] == "➖ 30"
    assert buttons["cw:s:bytes_per_day:655360"] == "➕ 640"
    # At the bottom of its range a cap has no ➖ (files: 1..200, default 20 -> 15 is fine).
    assert "cw:s:files_per_changeset:15" in buttons
    # Nothing overridden yet: no reset button.
    assert "cw:r" not in buttons


async def test_a_press_sets_the_cap_and_redraws(sessionmaker):
    world = World(sessionmaker, settings())
    await world.seed()
    await world.press("cw:s:creates_per_day:50")
    async with sessionmaker() as session:
        assert (await limits.effective(session)).creates_per_day == 50
    edit = world.tg_fake.edits[-1]
    assert "по умолчанию 40" in edit.text
    buttons = _buttons(edit.reply_markup)
    assert buttons["cw:s:creates_per_day:60"] == "➕ 60"
    assert "cw:r" in buttons
    await world.press("cw:r")
    async with sessionmaker() as session:
        assert await limits.effective(session) == limits.DEFAULTS


@pytest.mark.parametrize(
    "data",
    ["cw:s:nope:1", "cw:s:files_per_changeset:999", "cw:s:files_per_changeset:x", "cw:s:bytes_per_day:1500", "cw:zz"],
)
async def test_a_forged_press_changes_nothing(sessionmaker, data):
    world = World(sessionmaker, settings())
    await world.seed()
    await world.press(data)
    async with sessionmaker() as session:
        assert (await session.execute(select(ClaudeWriteLimit))).first() is None
    assert world.tg_fake.answered[-1].text == "Устарело."


async def test_the_info_button_names_the_range(sessionmaker):
    world = World(sessionmaker, settings())
    await world.seed()
    await world.press("cw:i:bytes_per_day")
    assert world.tg_fake.answered[-1].text == "Объём текста в день: 0 КБ–8192 КБ"


def test_the_keyboard_is_blocked_from_the_web_chat():
    from app.web import ingress

    assert "cw:s:creates_per_day:50".startswith(ingress.BLOCKED_CALLBACK_PREFIX)


def test_the_menu_entry_is_telegram_only_and_needs_claude():
    from app.tg import menu

    assert menu.action_available("claude_limits", settings(), web=False)
    assert not menu.action_available("claude_limits", settings(), web=True)
    assert not menu.action_available("claude_limits", settings(CLAUDE_ACCESS_ENABLED=False), web=False)


# --- web: POST /api/state/claude-limits --------------------------------------------


async def _web(sessionmaker, **overrides):
    await _seed(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(
        _web_settings(**{"CLAUDE_ACCESS_ENABLED": True, "MODE": "webhook", **overrides}),
        sessionmaker, FrozenClock(START), bot,
    )
    return app, fake


async def test_web_state_carries_the_caps(sessionmaker):
    app, fake = await _web(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        body = await (await _get(client, "/api/state", cookies=cookies)).json()
    rows = {row["key"]: row for row in body["claude_write_limits"]}
    assert set(rows) == set(limits.SPECS)
    assert rows["bytes_per_day"]["unit"] == "bytes"
    assert rows["creates_per_day"] == {
        "key": "creates_per_day", "label": limits.SPECS["creates_per_day"].label,
        "value": 40, "default": 40, "min": 0, "max": 500, "unit": "count",
    }


async def test_web_state_hides_the_caps_with_claude_off(sessionmaker):
    app, fake = await _web(sessionmaker, CLAUDE_ACCESS_ENABLED=False)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        body = await (await _get(client, "/api/state", cookies=cookies)).json()
        resp = await _post(client, "/api/state/claude-limits", {"key": "creates_per_day", "value": 5}, cookies)
    assert body["claude_write_limits"] is None
    assert resp.status == 404


async def test_web_can_raise_lower_and_reset(sessionmaker):
    app, fake = await _web(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/state/claude-limits", {"key": "creates_per_day", "value": 200}, cookies)
        assert resp.status == 200
        rows = {r["key"]: r for r in (await resp.json())["state"]["claude_write_limits"]}
        assert rows["creates_per_day"]["value"] == 200
        resp = await _post(client, "/api/state/claude-limits", {"key": "creates_per_day", "value": 3}, cookies)
        assert resp.status == 200
        resp = await _post(client, "/api/state/claude-limits", {"key": "creates_per_day", "value": None}, cookies)
        rows = {r["key"]: r for r in (await resp.json())["state"]["claude_write_limits"]}
        assert rows["creates_per_day"]["value"] == 40


async def test_web_needs_a_session(sessionmaker):
    app, _fake = await _web(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(client, "/api/state/claude-limits", {"key": "creates_per_day", "value": 5})
    assert resp.status == 401


@pytest.mark.parametrize(
    "body,status,detail",
    [
        ({"key": "creates_per_day"}, 400, None),
        ({"key": "creates_per_day", "value": "5"}, 400, None),
        ({"key": "creates_per_day", "value": True}, 400, None),
        ({"key": "creates_per_day", "value": 501}, 422, "out_of_range"),
        ({"key": "nope", "value": 1}, 422, "unknown_key"),
        ({"key": "nope", "value": None}, 422, "unknown_key"),
    ],
)
async def test_web_refuses_bad_bodies(sessionmaker, body, status, detail):
    app, fake = await _web(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/state/claude-limits", body, cookies)
        assert resp.status == status
        if detail:
            assert (await resp.json())["detail"] == detail
    async with sessionmaker() as session:
        assert (await session.execute(select(ClaudeWriteLimit))).first() is None


async def test_web_write_is_silent_in_telegram_and_logs_no_value(sessionmaker, caplog):
    app, fake = await _web(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        before = len(fake.sent)
        resp = await _post(client, "/api/state/claude-limits", {"key": "moves_per_day", "value": 123}, cookies)
        assert resp.status == 200
        assert len(fake.sent) == before
    assert "123" not in json.dumps([r.getMessage() for r in caplog.records])

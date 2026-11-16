"""The code window's storage, roots, digest and bookkeeping (anchor-code-window-plan.md
sections 4.2, 4.3 and 4.5, CW1).

Window lifecycle against the throwaway database: pending -> open ->
expired / off / replaced, one open at a time (also as a constraint),
and the `pending` row a card's buttons name. Roots and paths by
segments, NFC-normalised, `Anchor` refused on the first segment in any
case. The ledger's counts, the digest line and its scheduler trigger,
the debug views (no roots), and who may import the module.
"""

from __future__ import annotations

import ast
import datetime
import pathlib

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.scheduler import CLAUDE_LIBRARY_DIGEST_TIME, maybe_enqueue_library_digest
from app.db.models import CodeRead, CodeWindow, UserState
from app.tg import claude as claude_ui
from app.tg import vault as vault_ui
from app.vault import code_access
from app.web import ingress

ROOT = pathlib.Path(__file__).resolve().parent.parent
NOW = datetime.datetime(2026, 11, 16, 12, 0, tzinfo=datetime.UTC)


async def _state(sessionmaker) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, timezone="UTC"))
        await session.commit()


async def _open(sessionmaker, clock, roots=(), ttl_s=3600) -> code_access.Window:
    async with sessionmaker() as session:
        pending = await code_access.create_pending(session, clock, list(roots), ttl_s)
    async with sessionmaker() as session:
        window = await code_access.open_pending(
            session, clock, pending.id, int(pending.created_at.timestamp())
        )
    assert window is not None
    return window


async def _row(sessionmaker, window_id) -> CodeWindow:
    async with sessionmaker() as session:
        return await session.get(CodeWindow, window_id)


# --- roots and paths -------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Notes", "Notes"),
        (" Notes/Sub/ ", "Notes/Sub"),
        ("Проекты/Работа", "Проекты/Работа"),
        ("Projects/Anchor", "Projects/Anchor"),
        # NFD in, NFC out: what the user typed is compared as vaultd does.
        ("Замейтки", "Замейтки"),
    ],
)
def test_a_well_formed_root_is_normalised(raw, expected):
    assert code_access.parse_root(raw) == expected


@pytest.mark.parametrize("raw", ["", "/Notes", "a//b", "a/../b", "..", ".obsidian", "a\\b", "a\x00b", "./a"])
def test_a_malformed_root_is_refused(raw):
    assert code_access.parse_root(raw) is None


@pytest.mark.parametrize("rel", ["Anchor", "anchor/Memory", "ANCHOR/Journal/x.md", "Anchor/settings.md"])
def test_anchor_is_refused_in_any_case(rel):
    assert code_access.is_anchor(rel)
    assert not code_access.covers((), rel)
    assert not code_access.covers(("Anchor",), rel)


def test_anchor_is_refused_on_the_first_segment_only():
    assert not code_access.is_anchor("Projects/Anchor/x.md")
    assert code_access.covers((), "Projects/Anchor/x.md")


def test_roots_cover_by_whole_segments():
    assert code_access.covers(("Notes",), "Notes/a.md")
    assert code_access.covers(("Notes",), "Notes/sub/a.md")
    assert not code_access.covers(("Notes",), "Notes2/a.md")
    assert not code_access.covers(("Notes/sub",), "Notes/a.md")
    assert code_access.covers(("Notes", "Lens"), "Lens/x.md")
    assert code_access.covers((), "anything/at/all.md")


def test_roots_compare_after_nfc():
    nfd = "Замейтки/a.md"
    assert code_access.covers(("Замейтки",), nfd)


def test_parse_rel_never_rewrites_a_path():
    nfd = "Замейтки/a.md"
    assert code_access.parse_rel(nfd) == nfd
    assert code_access.parse_rel("Notes/a.md/") is None
    assert code_access.parse_rel(" Notes/a.md") == " Notes/a.md"


@pytest.mark.parametrize(
    "rest,expected",
    [
        ("", (3600, [])),
        ("6h", (21600, [])),
        ("24H Notes", (86400, ["Notes"])),
        ("Notes; Проекты/Работа ;", (3600, ["Notes", "Проекты/Работа"])),
        ("1h Мои заметки; Lens", (3600, ["Мои заметки", "Lens"])),
        ("Notes; Notes", (3600, ["Notes"])),
    ],
)
def test_code_on_arguments(rest, expected):
    assert vault_ui.parse_code_on(rest) == expected


@pytest.mark.parametrize(
    "rest,reply",
    [
        ("2h", vault_ui.CODE_BAD_TTL),
        ("Anchor", vault_ui.CODE_BAD_ROOT.format(root="Anchor")),
        ("Notes; anchor/Memory", vault_ui.CODE_BAD_ROOT.format(root="anchor/Memory")),
        ("../x", vault_ui.CODE_BAD_ROOT.format(root="../x")),
        ("; ".join(f"R{i}" for i in range(9)), vault_ui.CODE_TOO_MANY_ROOTS.format(n=8)),
    ],
)
def test_code_on_refuses_bad_arguments(rest, reply):
    assert vault_ui.parse_code_on(rest) == reply


# --- the lifecycle ---------------------------------------------------------------


async def test_pending_then_open_then_expired(sessionmaker):
    clock = FrozenClock(NOW)
    async with sessionmaker() as session:
        pending = await code_access.create_pending(session, clock, ["Notes"], 3600)
        assert pending.status == "pending" and pending.opened_at is None
        assert await code_access.current(session, clock) is None
    window = await _open(sessionmaker, clock, ["Notes"])
    assert window.status == "open"
    assert window.expires_at == NOW + datetime.timedelta(hours=1)
    async with sessionmaker() as session:
        assert (await code_access.current(session, clock)).id == window.id
    clock.advance(datetime.timedelta(hours=1))
    async with sessionmaker() as session:
        assert await code_access.current(session, clock) is None
    row = await _row(sessionmaker, window.id)
    assert (row.status, row.closed_reason, row.closed_at) == ("closed", "expired", window.expires_at)


async def test_off_closes_the_open_window_now(sessionmaker):
    clock = FrozenClock(NOW)
    window = await _open(sessionmaker, clock)
    clock.advance(datetime.timedelta(minutes=5))
    async with sessionmaker() as session:
        closed = await code_access.close_open(session, clock)
    assert closed.id == window.id and closed.closed_reason == "off" and closed.closed_at == clock.now_utc()
    async with sessionmaker() as session:
        assert await code_access.close_open(session, clock) is None
        assert (await code_access.last_closed(session)).id == window.id


async def test_opening_one_replaces_the_other(sessionmaker):
    clock = FrozenClock(NOW)
    first = await _open(sessionmaker, clock)
    clock.advance(datetime.timedelta(minutes=1))
    second = await _open(sessionmaker, clock, ["Lens"], 21600)
    assert (await _row(sessionmaker, first.id)).closed_reason == "replaced"
    async with sessionmaker() as session:
        assert (await code_access.current(session, clock)).id == second.id
        opened = (await session.execute(text("select count(*) from code_window where status = 'open'"))).scalar_one()
    assert opened == 1


async def test_at_most_one_open_window_is_a_constraint(sessionmaker):
    clock = FrozenClock(NOW)
    await _open(sessionmaker, clock)
    async with sessionmaker() as session:
        session.add(
            CodeWindow(
                status="open", roots=[], ttl_s=3600, created_at=NOW, opened_at=NOW,
                expires_at=NOW + datetime.timedelta(hours=1),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_a_pending_row_opens_once_and_only_for_its_own_button(sessionmaker):
    clock = FrozenClock(NOW)
    async with sessionmaker() as session:
        pending = await code_access.create_pending(session, clock, [], 3600)
    issued = int(pending.created_at.timestamp())
    async with sessionmaker() as session:
        assert await code_access.open_pending(session, clock, pending.id, issued + 1) is None
    async with sessionmaker() as session:
        assert await code_access.open_pending(session, clock, pending.id, issued) is not None
    async with sessionmaker() as session:
        assert await code_access.open_pending(session, clock, pending.id, issued) is None
        assert not await code_access.cancel_pending(session, pending.id, issued)


async def test_cancel_drops_the_pending_row(sessionmaker):
    clock = FrozenClock(NOW)
    async with sessionmaker() as session:
        pending = await code_access.create_pending(session, clock, [], 3600)
        assert await code_access.cancel_pending(session, pending.id, int(pending.created_at.timestamp()))
    assert await _row(sessionmaker, pending.id) is None


async def test_a_stale_pending_row_is_dropped_by_the_next_card(sessionmaker):
    clock = FrozenClock(NOW)
    async with sessionmaker() as session:
        old = await code_access.create_pending(session, clock, [], 3600)
    clock.advance(datetime.timedelta(minutes=11))
    async with sessionmaker() as session:
        await code_access.create_pending(session, clock, [], 3600)
    assert await _row(sessionmaker, old.id) is None


async def test_the_table_refuses_a_lifetime_or_roots_outside_the_spec(sessionmaker):
    for bad in (
        dict(ttl_s=7200, roots=[]),
        dict(ttl_s=3600, roots=[f"R{i}" for i in range(9)]),
        dict(ttl_s=3600, roots={"a": 1}),
    ):
        async with sessionmaker() as session:
            session.add(CodeWindow(status="pending", created_at=NOW, **bad))
            with pytest.raises(IntegrityError):
                await session.commit()


# --- the ledger and the digest ---------------------------------------------------


async def test_record_read_reports_the_first_read_once(sessionmaker):
    clock = FrozenClock(NOW)
    window = await _open(sessionmaker, clock)
    async with sessionmaker() as session:
        assert await code_access.record_read(session, clock, window.id, "tree", 5, 300)
        assert not await code_access.record_read(session, clock, window.id, "file", 1, 40)
        assert not await code_access.record_read(session, clock, window.id, "file", 1, 40)
        assert await code_access.files_read(session, window.id) == 2
        assert await code_access.reads_between(session, NOW, NOW + datetime.timedelta(seconds=1)) == (2, 3)
    assert (await _row(sessionmaker, window.id)).notified_at == NOW


@pytest.mark.parametrize(
    "files,requests,line",
    [
        (1, 1, "Claude Code читал vault: 1 файл, 1 запрос."),
        (3, 4, "Claude Code читал vault: 3 файла, 4 запроса."),
        (12, 25, "Claude Code читал vault: 12 файлов, 25 запросов."),
        (0, 2, "Claude Code читал vault: 0 файлов, 2 запроса."),
    ],
)
async def test_the_digest_line(sessionmaker, files, requests, line):
    clock = FrozenClock(NOW)
    window = await _open(sessionmaker, clock)
    async with sessionmaker() as session:
        for i in range(requests):
            route = "file" if i < files else "tree"
            await code_access.record_read(session, clock, window.id, route, 1 if route == "file" else 7, 10)
        assert await vault_ui.code_digest_line(session, NOW, NOW + datetime.timedelta(hours=1)) == line


async def test_no_reads_no_digest_line(sessionmaker):
    clock = FrozenClock(NOW)
    await _open(sessionmaker, clock)
    async with sessionmaker() as session:
        assert await vault_ui.code_digest_line(session, NOW, NOW + datetime.timedelta(days=1)) is None


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:
        self.sent.append(text)


async def test_the_claude_digest_carries_the_code_line(sessionmaker):
    await _state(sessionmaker)
    day = datetime.date(2026, 11, 16)
    evening = datetime.datetime.combine(day, CLAUDE_LIBRARY_DIGEST_TIME, tzinfo=datetime.UTC)
    clock = FrozenClock(evening - datetime.timedelta(hours=2))
    window = await _open(sessionmaker, clock)
    async with sessionmaker() as session:
        await code_access.record_read(session, clock, window.id, "file", 1, 10)
        await code_access.record_read(session, clock, window.id, "tree", 4, 10)
    bot = FakeBot()
    clock.set(evening + datetime.timedelta(minutes=1))
    async with sessionmaker() as session:
        await claude_ui.run_library_digest(
            session, Settings(), clock, bot, {"local_date": day.isoformat()}
        )
    assert bot.sent == ["Claude Code читал vault: 1 файл, 2 запроса."]


async def test_the_digest_is_queued_for_an_open_window_with_every_flag_off(sessionmaker):
    await _state(sessionmaker)
    day = datetime.date(2026, 11, 16)
    evening = datetime.datetime.combine(day, CLAUDE_LIBRARY_DIGEST_TIME, tzinfo=datetime.UTC)
    clock = FrozenClock(evening + datetime.timedelta(minutes=1))
    async with sessionmaker() as session:
        assert not await maybe_enqueue_library_digest(session, Settings(), clock, "UTC")
    clock.set(evening - datetime.timedelta(hours=3))
    await _open(sessionmaker, clock)
    clock.set(evening + datetime.timedelta(minutes=1))
    async with sessionmaker() as session:
        assert await maybe_enqueue_library_digest(session, Settings(), clock, "UTC")


async def test_a_window_that_closed_before_the_digest_window_is_not_a_trigger(sessionmaker):
    clock = FrozenClock(NOW - datetime.timedelta(days=3))
    await _open(sessionmaker, clock)
    async with sessionmaker() as session:
        assert not await code_access.active_between(session, NOW - datetime.timedelta(days=1), NOW)
        assert await code_access.active_between(session, NOW - datetime.timedelta(days=4), NOW)


# --- debug views and bookkeeping -------------------------------------------------


async def test_debug_views_carry_counts_but_never_roots(sessionmaker):
    clock = FrozenClock(NOW)
    window = await _open(sessionmaker, clock, ["Секретная папка", "Notes"])
    async with sessionmaker() as session:
        await code_access.record_read(session, clock, window.id, "file", 1, 99)
    async with sessionmaker() as session:
        columns = {
            row[0]: row[1]
            for row in (
                await session.execute(
                    text(
                        "select table_name, array_agg(column_name::text) from information_schema.columns "
                        "where table_schema = 'debug' and table_name in ('code_window', 'code_read') "
                        "group by table_name"
                    )
                )
            ).all()
        }
    assert "roots" not in columns["code_window"] and "n_roots" in columns["code_window"]
    assert set(columns["code_read"]) == {"id", "window_id", "at", "route", "files", "bytes"}
    async with sessionmaker() as session:
        await session.execute(text("set local role anchor_debug"))
        row = (await session.execute(text("select status, n_roots from debug.code_window"))).one()
        assert row == ("open", 2)
        dumped = str((await session.execute(text("select * from debug.code_window"))).all())
        assert "Секретная" not in dumped
        assert (await session.execute(text("select files, bytes from debug.code_read"))).one() == (1, 99)


def test_both_tables_are_purged_by_delete():
    from app.core import purge

    assert {"code_window", "code_read"} <= set(purge.PURGED_TABLES)
    assert purge.PURGED_TABLES.index("code_read") < purge.PURGED_TABLES.index("code_window")


def _imports(rel: str) -> set[str]:
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


CODE_ACCESS_IMPORTERS = {"app/tg/vault.py", "app/web/code_access.py", "app/core/scheduler.py", "app/startup.py"}


def test_only_the_named_modules_import_the_code_window_module():
    """Plan section 4.2: one module owns the tables; these four ask it."""
    found = set()
    for path in sorted((ROOT / "app").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if any(name == "app.vault.code_access" for name in _imports(rel)):
            found.add(rel)
    assert found == CODE_ACCESS_IMPORTERS


def test_only_the_web_module_calls_the_code_client_methods():
    """Plan section 4.4: `code_tree` / `code_file` are called from
    app/web/code_access.py only (client.py defines them)."""
    callers = set()
    for path in sorted((ROOT / "app").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("code_tree", "code_file"):
                callers.add(rel)
    assert callers == {"app/web/code_access.py"}


# --- the web chat ----------------------------------------------------------------


@pytest.mark.parametrize(
    "command", ["/vault code", "/vault code on", "/VAULT Code on 6h Notes", "/vault@EchoBot code off"]
)
def test_the_web_chat_cannot_send_vault_code(command):
    assert ingress.is_blocked_command(command)


@pytest.mark.parametrize("command", ["/vault", "/vault notes on", "/vault codex"])
def test_the_web_chat_keeps_the_rest_of_vault(command):
    assert not ingress.is_blocked_command(command)


def test_the_card_buttons_are_blocked_on_the_web():
    data = vault_ui.code_keyboard(3, 1700000000).inline_keyboard[0]
    for button in data:
        assert button.callback_data.startswith(ingress.BLOCKED_CALLBACK_PREFIX)


async def test_read_rows_cascade_with_their_window(sessionmaker):
    clock = FrozenClock(NOW)
    window = await _open(sessionmaker, clock)
    async with sessionmaker() as session:
        await code_access.record_read(session, clock, window.id, "file", 1, 1)
        await session.execute(text("delete from code_window"))
        await session.commit()
        assert (await session.execute(text("select count(*) from code_read"))).scalar_one() == 0
        assert CodeRead.__tablename__ == "code_read"


async def test_startup_with_the_flag_off_closes_the_open_window(sessionmaker, tmp_path):
    """Turning CODE_ACCESS_ENABLED back on revives no window."""
    from app.core.clock import SystemClock
    from app.startup import run_startup_tasks

    window = await _open(sessionmaker, SystemClock())
    persona = tmp_path / "persona.md"
    persona.write_text("persona")
    async with sessionmaker() as session:
        await run_startup_tasks(session, Settings(ALLOWED_CHAT_ID=555), persona)
    assert (await _row(sessionmaker, window.id)).closed_reason == "off"
    other = await _open(sessionmaker, SystemClock())
    async with sessionmaker() as session:
        await run_startup_tasks(session, Settings(ALLOWED_CHAT_ID=555, CODE_ACCESS_ENABLED=True), persona)
    assert (await _row(sessionmaker, other.id)).status == "open"

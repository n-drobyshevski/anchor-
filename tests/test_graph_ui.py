"""/graph (app/tg/graph.py), its client calls and its weekly job.

vaultd's side -- the rule, the footer edits, the routes -- is pinned by
vaultd/tests/test_graph_prune.py. Here: what the bot asks, what it
sends, and that only a pruning changeset is ever undone.
"""

from __future__ import annotations

import datetime

import pytest

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.scene import Deferred
from app.core.scheduler import GRAPH_PRUNE, graph_prune_dedup_key, maybe_enqueue_graph_prune
from app.db.models import Job, UserState
from app.tg import graph as graph_ui
from app.tg.router import BOT_COMMANDS
from app.vault import errors
from app.vault.client import ChangeEntry, PruneApplied, PrunePlan, UndoResult, VaultClient
from app.vault.errors import VaultError
from app.web import ingress
from vault_stub import TOKEN, start_stub

HASH = "0123456789abcdef0123456789abcdef"
# 2026-10-11 is a Sunday.
SUNDAY_NOON = datetime.datetime(2026, 10, 11, 12, 0, tzinfo=datetime.timezone.utc)


def _settings(**over) -> Settings:
    base = {"VAULT_MODE": "sync", "GRAPH_PRUNE_ENABLED": True}
    base.update(over)
    return Settings(**base)


def _plan(**over) -> PrunePlan:
    values = {
        "plan_hash": HASH, "config_error": None, "notes": 10, "edges_before": 20, "edges_after": 17,
        "removals": 3, "files": 4, "unresolved": 2, "orphans": 1, "report": "# Граф ссылок\n",
    }
    values.update(over)
    return PrunePlan(**values)


class FakeClient:
    def __init__(self, plan: PrunePlan | None = None, *, fail: str | None = None, changes=()) -> None:
        self.plan = plan or _plan()
        self.fail = fail
        self.changes = list(changes)
        self.applied: list[tuple[str, str]] = []
        self.undone: list[tuple[str, str]] = []

    async def graph_prune_plan(self) -> PrunePlan:
        if self.fail:
            raise VaultError(self.fail)
        return self.plan

    async def graph_prune_apply(self, plan_hash: str, changeset: str) -> PruneApplied:
        if self.fail:
            raise VaultError(self.fail)
        self.applied.append((plan_hash, changeset))
        return PruneApplied(files=4, removed=3, replayed=False)

    async def list_changes(self):
        return self.changes

    async def undo_changeset(self, ref: str, *, writer: str = "claude") -> UndoResult:
        self.undone.append((ref, writer))
        return UndoResult(restored=4, refused=0)


def _factory(client):
    return lambda _settings: client


def _change(cid: str, *, writer="echo", purpose="prune", undone=False, hour=10) -> ChangeEntry:
    return ChangeEntry(
        id=cid, kind="write", time=datetime.datetime(2026, 10, 1, hour, tzinfo=datetime.timezone.utc),
        undone=undone, files=[], writer=writer, purpose=purpose,
    )


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[str, object]] = []
        self.documents: list[tuple[bytes, str]] = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None):
        self.messages.append((text, reply_markup))

        class _M:
            message_id = 7

        return _M()

    async def send_document(self, chat_id, document, caption=None, parse_mode=None):
        self.documents.append((document.data, document.filename))

        class _M:
            message_id = 6

        return _M()


# -- rendering and commands -----------------------------------------------------------


def test_render_offers_apply_with_the_plan_hash():
    reply = graph_ui.render(_plan(), datetime.date(2026, 10, 11))
    assert "связей 20 → 17" in reply.text and HASH in reply.text
    assert reply.keyboard.inline_keyboard[0][0].callback_data == f"gp:a:{HASH}"
    assert reply.document == "# Граф ссылок\n".encode()
    assert reply.filename == "graph-2026-10-11.md"


def test_render_without_removals_has_no_button():
    reply = graph_ui.render(_plan(removals=0, files=0), datetime.date(2026, 10, 11))
    assert reply.keyboard is None and reply.text.startswith("Граф ссылок: убирать нечего.")


def test_render_reports_a_config_error():
    reply = graph_ui.render(_plan(config_error="unknown key: x", removals=0), datetime.date(2026, 10, 11))
    assert reply.text.startswith("Anchor/graph.md не читается: unknown key: x.")


async def test_dry_run_command(sessionmaker):
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, timezone="UTC"))
        await session.commit()
    reply = await graph_ui.command(
        sessionmaker, _settings(), FrozenClock(SUNDAY_NOON), None, client_factory=_factory(FakeClient())
    )
    assert reply.keyboard is not None and reply.document is not None


async def test_off_without_a_vault(sessionmaker):
    reply = await graph_ui.command(
        sessionmaker, _settings(VAULT_MODE="off"), FrozenClock(SUNDAY_NOON), None,
        client_factory=_factory(FakeClient()),
    )
    assert reply.text == graph_ui.OFF


async def test_apply_sends_the_hash_and_offers_undo(sessionmaker):
    client = FakeClient()
    reply = await graph_ui.command(
        sessionmaker, _settings(), FrozenClock(SUNDAY_NOON), f"apply {HASH}", client_factory=_factory(client)
    )
    [(sent_hash, changeset)] = client.applied
    assert sent_hash == HASH and changeset.startswith("prune-")
    assert reply.text.startswith("Готово: убрано связей 3, изменено заметок 4.")
    assert reply.keyboard.inline_keyboard[0][0].callback_data == f"gp:u:{changeset}"


@pytest.mark.parametrize(
    "code, text",
    [
        (errors.CONFLICT, graph_ui.PLAN_CHANGED),
        (errors.REFUSED, graph_ui.APPLY_REFUSED),
        (errors.UNAVAILABLE, graph_ui.UNAVAILABLE),
    ],
)
async def test_apply_failures(sessionmaker, code, text):
    reply = await graph_ui.command(
        sessionmaker, _settings(), FrozenClock(SUNDAY_NOON), f"apply {HASH}",
        client_factory=_factory(FakeClient(fail=code)),
    )
    assert reply.text == text


async def test_apply_needs_a_hash(sessionmaker):
    reply = await graph_ui.command(
        sessionmaker, _settings(), FrozenClock(SUNDAY_NOON), "apply nope", client_factory=_factory(FakeClient())
    )
    assert reply.text == graph_ui.APPLY_USAGE


async def test_undo_takes_back_only_the_newest_pruning(sessionmaker):
    client = FakeClient(
        changes=[
            _change("prune-old", hour=9),
            _change("prune-new", hour=11),
            _change("inbox", purpose=None, hour=12),
            _change("claude-1", writer="claude", purpose=None, hour=13),
            _change("prune-gone", undone=True, hour=14),
        ]
    )
    reply = await graph_ui.command(
        sessionmaker, _settings(), FrozenClock(SUNDAY_NOON), "undo", client_factory=_factory(client)
    )
    assert client.undone == [("prune-new", "echo")]
    assert reply.text == "Отменено: восстановлено заметок 4."


async def test_undo_with_nothing_to_undo(sessionmaker):
    client = FakeClient(changes=[_change("inbox", purpose=None)])
    reply = await graph_ui.command(
        sessionmaker, _settings(), FrozenClock(SUNDAY_NOON), "undo", client_factory=_factory(client)
    )
    assert reply.text == graph_ui.NOTHING_TO_UNDO and client.undone == []


async def test_the_undo_button_refuses_a_changeset_that_is_not_a_pruning():
    client = FakeClient(changes=[_change("prune-0123456789abcdef", purpose=None)])
    reply = await graph_ui.undo(_settings(), _factory(client), "prune-0123456789abcdef")
    assert reply.text == graph_ui.UNDO_GONE and client.undone == []


def test_graph_is_registered_and_telegram_only():
    assert "graph" in {c.command for c in BOT_COMMANDS}
    assert "graph" in ingress.BLOCKED_COMMANDS
    assert "gp:" in ingress.BLOCKED_CALLBACK_PREFIX


# -- the weekly job ---------------------------------------------------------------------


async def _seed_state(sessionmaker, **over) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, timezone="UTC", **over))
        await session.commit()


async def test_weekly_sends_the_report_and_the_button(sessionmaker):
    await _seed_state(sessionmaker)
    bot = FakeBot()
    async with sessionmaker() as session:
        await graph_ui.run_weekly(session, _settings(), FrozenClock(SUNDAY_NOON), bot, _factory(FakeClient()))
    assert bot.documents == [("# Граф ссылок\n".encode(), "graph-2026-10-11.md")]
    [(_text, keyboard)] = bot.messages
    assert keyboard.inline_keyboard[0][0].callback_data == f"gp:a:{HASH}"


async def test_weekly_stays_quiet_when_there_is_nothing(sessionmaker):
    await _seed_state(sessionmaker)
    bot = FakeBot()
    client = FakeClient(_plan(removals=0, files=0, unresolved=0, orphans=0))
    async with sessionmaker() as session:
        await graph_ui.run_weekly(session, _settings(), FrozenClock(SUNDAY_NOON), bot, _factory(client))
    assert bot.messages == [] and bot.documents == []


async def test_weekly_defers_when_the_vault_is_down(sessionmaker):
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        with pytest.raises(Deferred):
            await graph_ui.run_weekly(
                session, _settings(), FrozenClock(SUNDAY_NOON), FakeBot(),
                _factory(FakeClient(fail=errors.UNAVAILABLE)),
            )


async def _jobs(sessionmaker) -> list[str]:
    from sqlalchemy import select

    async with sessionmaker() as session:
        return list((await session.execute(select(Job.dedup_key).where(Job.kind == GRAPH_PRUNE))).scalars())


async def test_the_dry_run_is_queued_once_a_week_from_its_day(sessionmaker):
    settings = _settings()
    saturday = FrozenClock(SUNDAY_NOON - datetime.timedelta(days=1))
    sunday_early = FrozenClock(SUNDAY_NOON.replace(hour=9))
    async with sessionmaker() as session:
        assert not await maybe_enqueue_graph_prune(session, settings, saturday, "UTC")
        assert not await maybe_enqueue_graph_prune(session, settings, sunday_early, "UTC")
        assert await maybe_enqueue_graph_prune(session, settings, FrozenClock(SUNDAY_NOON), "UTC")
        await session.commit()
    async with sessionmaker() as session:
        assert not await maybe_enqueue_graph_prune(session, settings, FrozenClock(SUNDAY_NOON), "UTC")
        await session.commit()
    assert await _jobs(sessionmaker) == [graph_prune_dedup_key(SUNDAY_NOON.date())]


async def test_a_midweek_day_catches_up(sessionmaker):
    settings = _settings(GRAPH_PRUNE_WEEKDAY=2)  # Wednesday
    friday = FrozenClock(datetime.datetime(2026, 10, 9, 8, 0, tzinfo=datetime.timezone.utc))
    async with sessionmaker() as session:
        assert await maybe_enqueue_graph_prune(session, settings, friday, "UTC")


async def test_nothing_is_queued_with_the_vault_off(sessionmaker):
    async with sessionmaker() as session:
        assert not await maybe_enqueue_graph_prune(
            session, _settings(VAULT_MODE="off"), FrozenClock(SUNDAY_NOON), "UTC"
        )


def test_the_weekday_must_be_a_day():
    with pytest.raises(ValueError):
        _settings(GRAPH_PRUNE_WEEKDAY=7)


# -- the client -------------------------------------------------------------------------


@pytest.fixture
async def stub():
    stub, server = await start_stub()
    yield stub
    await server.close()


async def test_client_parses_the_plan_and_maps_409(stub):
    body = {
        "plan_hash": HASH, "config_error": None, "notes": 3, "edges_before": 3, "edges_after": 2,
        "removals": 1, "files": 2, "unresolved": 0, "orphans": 0, "report": "# r\n",
    }
    stub.respond("GET", "/v1/graph/prune", 200, body)
    client = VaultClient(stub.url, TOKEN)
    plan = await client.graph_prune_plan()
    assert plan.plan_hash == HASH and plan.removals == 1 and plan.report == "# r\n"
    stub.respond("POST", "/v1/graph/prune", 409, {"error": "plan_changed"})
    with pytest.raises(VaultError) as exc:
        await client.graph_prune_apply(HASH, "prune-1")
    assert exc.value.code == errors.CONFLICT


async def test_client_reads_the_changeset_purpose(stub):
    stub.respond(
        "GET", "/v1/changes", 200,
        {"changes": [{"id": "p", "kind": "write", "time": "2026-10-01T10:00:00Z", "undone": False,
                      "files": [], "writer": "echo", "purpose": "prune"}]},
    )
    [change] = await VaultClient(stub.url, TOKEN).list_changes()
    assert change.purpose == "prune"

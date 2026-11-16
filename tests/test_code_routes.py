"""The code window's HTTP routes (anchor-code-window-plan.md section 4.4, CW1).

Through the production app (app/main.py's `build_webhook_app`) with a
fake vaultd (tests/code_vault_fake.py) and a fake Telegram session:
the token (bare 404, wrong and missing alike), the lockout, the rate
limit, no window, confinement both ways, the file cap, vaultd's own
refusals, the ledger and the single notice, and a log capture proving
no path, root, text or token is logged.
"""

from __future__ import annotations

import datetime
import hashlib
import logging

import pytest
from aiogram import Dispatcher
from aiohttp.test_utils import TestClient, TestServer
from code_vault_fake import FakeCodeVault
from conftest import make_bot
from sqlalchemy import text

from app.config import Settings
from app.core.clock import FrozenClock
from app.db.models import UserState
from app.main import build_webhook_app
from app.vault import code_access
from app.web import code_access as code_access_web

NOW = datetime.datetime(2026, 11, 16, 12, 0, tzinfo=datetime.UTC)
TOKEN = "code-token-" + "c" * 40
TOKEN_SHA256 = hashlib.sha256(TOKEN.encode()).hexdigest()
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _settings(**overrides) -> Settings:
    base = dict(
        MODE="webhook",
        TELEGRAM_BOT_TOKEN="123456:TEST",
        PUBLIC_URL="https://anchor.example.test",
        ALLOWED_CHAT_ID=555,
        VAULT_MODE="status",
        CODE_ACCESS_ENABLED=True,
        CODE_ACCESS_TOKEN_SHA256=TOKEN_SHA256,
    )
    base.update(overrides)
    return Settings(**base)


class Harness:
    def __init__(self, client: TestClient, fake: FakeCodeVault, tg, clock: FrozenClock) -> None:
        self.client, self.fake, self.tg, self.clock = client, fake, tg, clock

    async def post(self, route: str, body=None, headers=AUTH, **kw):
        kwargs = {"json": body} if body is not None else {}
        return await self.client.post(f"/code/v1/{route}", headers=headers, **kwargs, **kw)


@pytest.fixture
async def harness(sessionmaker):
    async def make(**overrides) -> Harness:
        settings = _settings(**overrides)
        clock = FrozenClock(NOW)
        bot, tg = make_bot()
        app = build_webhook_app(
            settings, bot, Dispatcher(), sessionmaker,
            engine=None, provider=None, cheap_provider=None, safety_provider=None,
            llm_client=None, clock=clock, hub=None, code_store=None,
        )
        app.on_startup.clear()
        app.on_cleanup.clear()
        fake = FakeCodeVault()
        app[code_access_web.CLIENT_FACTORY_KEY] = fake.factory
        client = TestClient(TestServer(app))
        await client.start_server()
        made.append((client, bot))
        return Harness(client, fake, tg, clock)

    made: list = []
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, timezone="UTC"))
        await session.commit()
    yield make
    for client, bot in made:
        await client.close()
        await bot.session.close()


async def _open(sessionmaker, clock, roots=()) -> code_access.Window:
    async with sessionmaker() as session:
        pending = await code_access.create_pending(session, clock, list(roots), 3600)
    async with sessionmaker() as session:
        return await code_access.open_pending(session, clock, pending.id, int(pending.created_at.timestamp()))


async def _unrouted(h: Harness):
    response = await h.client.post("/code/v1/nonexistent", headers=AUTH)
    return response.status, await response.text()


# --- the token ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": "Bearer"},
        {"Authorization": f"Bearer {TOKEN_SHA256}"},
    ],
)
@pytest.mark.parametrize("route", ["status", "tree", "file"])
async def test_a_missing_or_wrong_token_is_the_bare_404(harness, sessionmaker, headers, route):
    h = await harness()
    await _open(sessionmaker, h.clock)
    response = await h.post(route, {"path": "Notes/plan.md"} if route == "file" else {}, headers=headers)
    assert (response.status, await response.text()) == await _unrouted(h)
    assert not [c for c in h.fake.calls if c[0] != "status"]


async def test_get_is_the_bare_404_too(harness):
    h = await harness()
    response = await h.client.get("/code/v1/status", headers=AUTH)
    assert (response.status, await response.text()) == await _unrouted(h)


async def test_ten_wrong_tokens_lock_every_route_for_an_hour(harness, sessionmaker):
    h = await harness()
    await _open(sessionmaker, h.clock)
    for _ in range(9):
        await h.post("status", {}, headers={"Authorization": "Bearer nope"})
    assert (await h.post("status", {})).status == 200
    await h.post("status", {}, headers={"Authorization": "Bearer nope"})
    assert [m.text for m in h.tg.sent] == [code_access_web.LOCKOUT_TEXT.format(n=10)]
    for route in ("status", "tree", "file"):
        response = await h.post(route, {"path": "Notes/plan.md"} if route == "file" else {})
        assert response.status == 404, route
    h.clock.advance(datetime.timedelta(minutes=59))
    assert (await h.post("status", {})).status == 404
    h.clock.advance(datetime.timedelta(minutes=1))
    assert (await h.post("status", {})).status == 200


async def test_a_missing_header_is_not_counted_toward_the_lockout(harness):
    h = await harness()
    for _ in range(20):
        await h.post("status", {}, headers={})
    assert (await h.post("status", {})).status == 200


async def test_an_upper_case_configured_hash_still_matches(harness):
    h = await harness(CODE_ACCESS_TOKEN_SHA256=TOKEN_SHA256.upper())
    assert (await h.post("status", {})).status == 200


# --- status, window, rate limit ----------------------------------------------------


async def test_status_without_and_with_a_window(harness, sessionmaker):
    h = await harness(CODE_FILES_PER_WINDOW=5)
    response = await h.post("status")
    assert (response.status, await response.json()) == (200, {"open": False})
    window = await _open(sessionmaker, h.clock, ["Notes"])
    await h.post("file", {"path": "Notes/plan.md"})
    body = await (await h.post("status", {})).json()
    assert body == {
        "open": True,
        "expires_at": window.expires_at.isoformat(),
        "roots": ["Notes"],
        "files_read": 1,
        "files_left": 4,
    }


@pytest.mark.parametrize("route,body", [("tree", {}), ("file", {"path": "Notes/plan.md"})])
async def test_no_window_is_403(harness, route, body):
    h = await harness()
    response = await h.post(route, body)
    assert (response.status, await response.json()) == (403, {"error": "no_window"})
    assert h.fake.calls == []


async def test_an_expired_window_is_no_window(harness, sessionmaker):
    h = await harness()
    await _open(sessionmaker, h.clock)
    h.clock.advance(datetime.timedelta(hours=1))
    assert (await h.post("tree", {})).status == 403


async def test_120_requests_a_minute(harness):
    h = await harness()
    for _ in range(120):
        assert (await h.post("status", {})).status == 200
    response = await h.post("status", {})
    assert response.status == 429 and response.headers["Retry-After"]
    h.clock.advance(datetime.timedelta(seconds=61))
    assert (await h.post("status", {})).status == 200


@pytest.mark.parametrize(
    "route,body",
    [
        ("status", {"x": 1}),
        ("tree", {"roots": []}),
        ("tree", {"root": 3}),
        ("tree", {"root": "../x"}),
        ("file", {}),
        ("file", {"path": "Notes/plan.md", "roots": []}),
        ("file", {"path": "/Notes/plan.md"}),
        ("file", {"path": "a/../b.md"}),
        ("file", ["Notes/plan.md"]),
    ],
)
async def test_malformed_bodies_are_400(harness, sessionmaker, route, body):
    h = await harness()
    await _open(sessionmaker, h.clock)
    response = await h.post(route, body)
    assert (response.status, await response.json()) == (400, {"error": "bad_request"})
    assert not [c for c in h.fake.calls if c[0] != "status"]


async def test_a_body_that_is_not_json_is_400(harness, sessionmaker):
    h = await harness()
    await _open(sessionmaker, h.clock)
    response = await h.client.post("/code/v1/tree", headers=AUTH, data=b"{not json")
    assert response.status == 400


# --- tree and file, confinement ----------------------------------------------------


async def test_the_tree_of_the_whole_vault(harness, sessionmaker):
    h = await harness()
    await _open(sessionmaker, h.clock)
    response = await h.post("tree", {})
    body = await response.json()
    assert response.status == 200 and body["truncated"] is False
    assert [f["path"] for f in body["files"]] == sorted(h.fake.notes)
    assert body["files"][0] == {"path": "Lens/Ashby.md", "class": "lens", "bytes": 18, "mtime": "2026-11-16T10:00:00Z"}
    assert h.fake.calls == [("tree", [])]


async def test_the_tree_narrowed_to_a_root(harness, sessionmaker):
    h = await harness()
    await _open(sessionmaker, h.clock, ["Notes", "Lens"])
    body = await (await h.post("tree", {"root": "Notes/sub"})).json()
    assert [f["path"] for f in body["files"]] == ["Notes/sub/deep.md"]
    assert h.fake.calls[-1] == ("tree", ["Notes/sub"])
    body = await (await h.post("tree", {})).json()
    assert [f["path"] for f in body["files"]] == ["Lens/Ashby.md", "Notes/plan.md", "Notes/sub/deep.md"]
    assert h.fake.calls[-1] == ("tree", ["Notes", "Lens"])


@pytest.mark.parametrize("root", ["Notes2", "Projects", "Anchor", "anchor/Memory"])
async def test_a_root_outside_the_window_is_404_without_asking_vaultd(harness, sessionmaker, root):
    h = await harness()
    await _open(sessionmaker, h.clock, ["Notes"])
    response = await h.post("tree", {"root": root})
    assert (response.status, await response.json()) == (404, {"error": "not_found"})
    assert h.fake.calls == []


async def test_the_bot_drops_a_tree_entry_vaultd_should_not_have_sent(harness, sessionmaker):
    h = await harness()
    await _open(sessionmaker, h.clock, ["Notes"])

    async def leaky(roots):
        tree = await FakeCodeVault(h.fake.notes | {"Anchor/Memory/1.md": ("none", "x")}).code_tree([])
        return tree

    h.fake.code_tree = leaky
    body = await (await h.post("tree", {})).json()
    assert [f["path"] for f in body["files"]] == ["Notes/plan.md", "Notes/sub/deep.md"]


async def test_a_file_inside_the_window(harness, sessionmaker):
    h = await harness()
    await _open(sessionmaker, h.clock, ["Notes"])
    response = await h.post("file", {"path": "Notes/sub/deep.md"})
    body = await response.json()
    assert response.status == 200
    assert body == {
        "path": "Notes/sub/deep.md",
        "class": "knowledge",
        "sha256": hashlib.sha256("Глубоко.".encode()).hexdigest(),
        "text": "Глубоко.",
    }
    assert h.fake.calls == [("file", "Notes/sub/deep.md", ["Notes"])]


@pytest.mark.parametrize(
    "path", ["Notes2/other.md", "Lens/Ashby.md", "Anchor/settings.md", "anchor/Memory/1.md"]
)
async def test_a_file_outside_the_window_is_the_missing_note_404(harness, sessionmaker, path):
    h = await harness()
    await _open(sessionmaker, h.clock, ["Notes"])
    response = await h.post("file", {"path": path})
    missing = await h.post("file", {"path": "Notes/missing.md"})
    assert (response.status, await response.json()) == (404, {"error": "not_found"})
    assert (missing.status, await missing.json()) == (404, {"error": "not_found"})
    assert [c[1] for c in h.fake.calls] == ["Notes/missing.md"]


async def test_vaultd_checks_again_a_file_the_bot_let_through(harness, sessionmaker):
    """The other way round: the bot passes the window's roots on, and
    vaultd's own refusal is the same 404."""
    h = await harness()
    await _open(sessionmaker, h.clock, ["Notes"])
    h.fake.notes.pop("Notes/plan.md")
    assert (await h.post("file", {"path": "Notes/plan.md"})).status == 404
    assert h.fake.calls == [("file", "Notes/plan.md", ["Notes"])]


async def test_the_file_cap(harness, sessionmaker):
    h = await harness(CODE_FILES_PER_WINDOW=2)
    await _open(sessionmaker, h.clock)
    assert (await h.post("file", {"path": "Notes/plan.md"})).status == 200
    assert (await h.post("file", {"path": "Lens/Ashby.md"})).status == 200
    response = await h.post("file", {"path": "Notes/sub/deep.md"})
    assert (response.status, await response.json()) == (429, {"error": "window_cap"})
    assert len([c for c in h.fake.calls if c[0] == "file"]) == 2
    # A tree call is not a file read.
    assert (await h.post("tree", {})).status == 200
    # A new window starts again.
    await _open(sessionmaker, h.clock)
    assert (await h.post("file", {"path": "Notes/sub/deep.md"})).status == 200


@pytest.mark.parametrize(
    "error,route,status,code",
    [
        ("conflict", "tree", 409, "settings_unusable"),
        ("conflict", "file", 409, "settings_unusable"),
        ("not_found", "tree", 503, "vault_code_off"),
        ("unavailable", "tree", 502, "vault_unavailable"),
        ("unavailable", "file", 502, "vault_unavailable"),
        ("unauthorized", "file", 502, "vault_unavailable"),
        ("refused", "file", 400, "bad_request"),
        ("bad_response", "tree", 502, "vault_unavailable"),
    ],
)
async def test_vaultd_refusals(harness, sessionmaker, error, route, status, code):
    h = await harness()
    await _open(sessionmaker, h.clock)
    h.fake.error = error
    response = await h.post(route, {"path": "Notes/plan.md"} if route == "file" else {})
    assert (response.status, await response.json()) == (status, {"error": code})
    async with sessionmaker() as session:
        assert (await session.execute(text("select count(*) from code_read"))).scalar_one() == 0


# --- the ledger and the notice -----------------------------------------------------


async def test_each_read_is_one_row_and_the_first_is_announced_once(harness, sessionmaker):
    h = await harness()
    window = await _open(sessionmaker, h.clock, ["Notes"])
    await h.post("status", {})
    assert h.tg.sent == []
    await h.post("tree", {})
    await h.post("file", {"path": "Notes/plan.md"})
    await h.post("file", {"path": "Notes/sub/deep.md"})
    await h.post("file", {"path": "Notes/nope.md"})
    assert [m.text for m in h.tg.sent] == ["Claude Code начал читать vault: окно до 13:00"]
    assert h.tg.sent[0].chat_id == 555
    async with sessionmaker() as session:
        rows = (await session.execute(text("select window_id, route, files from code_read order by id"))).all()
        notified = (
            await session.execute(text("select notified_at from code_window where id = :i"), {"i": window.id})
        ).scalar_one()
    assert rows == [(window.id, "tree", 2), (window.id, "file", 1), (window.id, "file", 1)]
    assert notified == NOW
    # A new window gets its own notice.
    await _open(sessionmaker, h.clock)
    await h.post("file", {"path": "Lens/Ashby.md"})
    assert len(h.tg.sent) == 2


async def test_nothing_is_logged_but_ids_counts_and_codes(harness, sessionmaker, caplog, monkeypatch):
    # alembic's fileConfig (the session's migration) disables loggers that
    # existed then; switch this module's back on for the capture.
    monkeypatch.setattr(code_access_web.logger, "disabled", False)
    h = await harness()
    await _open(sessionmaker, h.clock, ["Notes"])
    caplog.set_level(logging.DEBUG)
    await h.post("tree", {"root": "Notes"})
    await h.post("tree", {"root": "Notes2"})
    await h.post("file", {"path": "Notes/plan.md"})
    await h.post("file", {"path": "Notes2/other.md"})
    await h.post("status", {}, headers={"Authorization": "Bearer секрет-не-тот"})
    h.fake.error = "conflict"
    await h.post("file", {"path": "Notes/sub/deep.md"})
    records = [r for r in caplog.records if r.name.startswith("app.")]
    assert records
    dumped = " ".join(f"{r.getMessage()} {r.__dict__}" for r in records)
    for secret in (
        "Notes", "plan.md", "deep.md", "other.md", "Синтетический", "Глубоко", TOKEN, TOKEN_SHA256, "секрет",
    ):
        assert secret not in dumped, secret

"""What the Claude connector never lets out (connector plan sections 4, 8, 10).

- No log record, over a full connect-and-read flow, carries a token, an
  authorization code, `state`, the confirmation code, the handle, the
  browser cookie or the client id URL.
- aiohttp's access log stays off (the paths and queries carry secrets).
- Every `/oauth/*` and well-known response carries no-store,
  no-referrer and `frame-ancestors 'none'`.
- The debug views show ids, statuses and times only.
- The OAuth modules reach no model, no state or memory writer, no
  outbound path and no vault notes; only oauth_store names the tables.
- With the flag off, every new route is aiohttp's own 404.
"""

from __future__ import annotations

import ast
import json
import logging
import urllib.parse
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import text

from app.config import Settings
from app.main import build_webhook_app
from app.web import oauth
from claude_helpers import STATE, World, settings

pytestmark = pytest.mark.asyncio

ROOT = Path(__file__).resolve().parent.parent
LOGGERS = (
    "app.web.oauth",
    "app.web.oauth_store",
    "app.web.mcp_core",
    "app.web.mcp_claude",
    "app.tg.claude",
    "app.tg.access",
    "app.tg.router",
)
NEW_PATHS = [
    ("GET", "/.well-known/oauth-protected-resource"),
    ("GET", "/.well-known/oauth-protected-resource/mcp/claude"),
    ("GET", "/.well-known/oauth-authorization-server"),
    ("GET", "/oauth/authorize"),
    ("GET", "/oauth/authorize/status"),
    ("POST", "/oauth/token"),
    ("POST", "/oauth/revoke"),
    ("POST", "/mcp/claude"),
]


@pytest.fixture
def live_loggers(monkeypatch):
    """The session fixture runs alembic in-process, and alembic.ini's
    fileConfig disables every logger that already exists. Production
    runs alembic in its own process, so these are live there; re-enable
    them here, or the caplog test would pass on silence."""
    for name in LOGGERS:
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)


async def test_no_secret_reaches_any_log_record(sessionmaker, caplog, live_loggers):
    world = World(sessionmaker)
    await world.seed()
    caplog.set_level(logging.DEBUG)
    secrets_seen: list[str] = [STATE, oauth.COOKIE]
    async with TestClient(TestServer(world.app)) as client:
        handle, code, cookie = await world.start(client)
        secrets_seen += [handle, code, cookie]
        await world.command(f"/claude connect {code}")
        redirect = await world.poll(client, handle, cookie)
        auth_code = urllib.parse.parse_qs(
            urllib.parse.urlsplit(redirect.headers["Location"]).query
        )["code"][0]
        secrets_seen.append(auth_code)
        tokens = await (await world.exchange(client, auth_code)).json()
        rotated = await (await world.refresh(client, tokens["refresh_token"])).json()
        secrets_seen += [
            tokens["access_token"], tokens["refresh_token"],
            rotated["access_token"], rotated["refresh_token"],
        ]
        await world.open_window(["memory"])
        await world.mcp(client, rotated["access_token"], "tools/call", {"name": "get_memory"})
        await world.exchange(client, auth_code)  # a replay, logged as one
        await world.command("/claude connect ZZZZZZ")
        await client.post(
            "/oauth/revoke", data={"token": rotated["refresh_token"], "client_id": "x"}
        )
        await world.command("/revoke")
    from app.web import oauth_store

    secrets_seen += [oauth_store.CLIENT_ID, "mcp-oauth-client-metadata"]
    events = {getattr(r, "event", None) for r in caplog.records}
    # Not vacuous: the flow was logged, just not its secrets.
    assert {"oauth", "oauth_approved", "oauth_connected", "oauth_refresh", "mcp"} <= events
    for record in caplog.records:
        rendered = record.getMessage() + json.dumps(record.__dict__, default=str)
        for secret in secrets_seen:
            assert secret not in rendered, (record.name, record.getMessage())


async def test_security_headers_on_every_oauth_response(sessionmaker):
    world = World(sessionmaker)
    await world.seed()
    async with TestClient(TestServer(world.app)) as client:
        handle, code, cookie = await world.start(client)
        responses = [
            await client.get("/.well-known/oauth-protected-resource"),
            await client.get("/.well-known/oauth-protected-resource/mcp/claude"),
            await client.get("/.well-known/oauth-authorization-server"),
            await client.get("/oauth/authorize", params=world.authorize_params(state=None)),
            await client.get("/oauth/authorize", params=world.authorize_params()),
            await world.poll(client, handle, cookie),
            await world.poll(client, handle, None),
        ]
        await world.command(f"/claude connect {code}")
        responses.append(await world.poll(client, handle, cookie))
        responses += [
            await client.post("/oauth/token", data={"grant_type": "authorization_code"}),
            await client.post("/oauth/token", data={"grant_type": "nope"}),
            await client.post("/oauth/token", data=b"\xff" * 10),
            await client.post("/oauth/revoke", data={"token": "x", "client_id": "y"}),
            await client.post("/oauth/revoke", data={}),
        ]
    assert {r.status for r in responses} >= {200, 302, 400, 404}
    for response in responses:
        assert response.headers["Cache-Control"] == "no-store", response.url
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_the_access_log_is_off():
    tree = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run_app"
    ]
    assert calls
    for call in calls:
        keywords = {k.arg: k.value for k in call.keywords}
        assert isinstance(keywords.get("access_log"), ast.Constant)
        assert keywords["access_log"].value is None


async def test_the_debug_views_show_no_secret_column(sessionmaker):
    async with sessionmaker() as session:
        rows = await session.execute(
            text(
                "select table_name, column_name from information_schema.columns "
                "where table_schema = 'debug' and table_name in "
                "('oauth_connection', 'oauth_request', 'access_grant')"
            )
        )
        columns: dict[str, set[str]] = {}
        for table, column in rows.all():
            columns.setdefault(table, set()).add(column)
    assert columns["oauth_connection"] == {
        "id", "created_at", "expires_at", "last_used_at", "revoked_at"
    }
    assert columns["oauth_request"] == {
        "id", "status", "code_expires_at", "connection_id", "created_at"
    }
    assert "token_sha256" not in columns["access_grant"]
    assert "connection_id" in columns["access_grant"]
    exists = await _table_exists(sessionmaker, "debug", "oauth_token")
    assert not exists


async def _table_exists(sessionmaker, schema: str, name: str) -> bool:
    async with sessionmaker() as session:
        found = await session.execute(
            text(
                "select 1 from information_schema.tables where table_schema = :s "
                "and table_name = :n"
            ),
            {"s": schema, "n": name},
        )
        return found.first() is not None


async def test_the_flag_off_leaves_every_new_route_aiohttp_s_404(sessionmaker):
    off = Settings(
        MODE="webhook", TELEGRAM_BOT_TOKEN="123456:TEST", PUBLIC_URL="https://anchor.example",
        GROK_ACCESS_ENABLED=True,
    )
    assert not off.CLAUDE_ACCESS_ENABLED
    bot = Bot(token=off.TELEGRAM_BOT_TOKEN)
    app = build_webhook_app(
        off, bot, Dispatcher(), sessionmaker,
        engine=None, provider=None, cheap_provider=None, safety_provider=None,
        llm_client=None, clock=None, hub=None, code_store=None,
    )
    app.on_startup.clear()
    app.on_cleanup.clear()
    async with TestClient(TestServer(app)) as client:
        reference = await (await client.get("/no/such/path")).text()
        for method, path in NEW_PATHS:
            resp = await client.request(method, path)
            assert resp.status == 404, path
            assert await resp.text() == reference == "404: Not Found"
            assert "Content-Security-Policy" not in resp.headers
    await bot.session.close()


# --- isolation ---

OAUTH_MODULES = ("app/web/oauth.py", "app/web/oauth_store.py", "app/web/mcp_claude.py")
FORBIDDEN_PREFIXES = (
    "app.llm",
    "app.core.state",
    "app.core.memory",
    "app.core.proposal",
    "app.core.outbound",
    "app.core.turn",
    "app.core.extract",
    "app.research",
    "app.planner",
    "app.vault",
    "app.tg",
)
TABLE_NAMES = ("OauthConnection", "OauthRequest", "OauthToken", "oauth_connection", "oauth_request", "oauth_token")
# Who may name the oauth tables: the models define them, purge
# truncates them, and oauth_store is their only writer.
TABLE_OWNERS = ("app/db/models.py", "app/core/purge.py", "app/web/oauth_store.py")


def _imports(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


@pytest.mark.parametrize("module", OAUTH_MODULES)
def test_the_oauth_modules_reach_nothing_that_writes_or_thinks(module):
    imported = _imports(ROOT / module)
    offenders = sorted(
        name for name in imported if name.startswith(FORBIDDEN_PREFIXES) or "update_state" in name
    )
    assert offenders == []


def _names_a_table(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            word = node.id
        elif isinstance(node, ast.Attribute):
            word = node.attr
        elif isinstance(node, ast.alias):
            word = node.name
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            word = node.value
        else:
            continue
        if any(name in word for name in TABLE_NAMES):
            return True
    return False


def test_only_oauth_store_names_the_oauth_tables():
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in sorted((ROOT / "app").rglob("*.py"))
        if path.relative_to(ROOT).as_posix() not in TABLE_OWNERS and _names_a_table(path)
    ]
    assert offenders == []


def test_the_table_check_is_not_vacuous(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text("from app.db.models import OauthToken\n")
    assert _names_a_table(sample)
    sample.write_text('"""Mentions oauth_token in prose."""\nx = 1\n')
    assert not _names_a_table(sample)
    sample.write_text("q = 'delete from oauth_request'\n")
    assert _names_a_table(sample)


def test_settings_default_off():
    assert settings(CLAUDE_ACCESS_ENABLED=False).CLAUDE_ACCESS_ENABLED is False
    assert Settings().CLAUDE_ACCESS_ENABLED is False

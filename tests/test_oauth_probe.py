"""The claude.ai dry-run probe grants nothing and logs only shapes.

app/web/oauth_probe.py exists to answer the connector plan's section 11
before C2 writes an authorization server. What is pinned here:

- off by default, and then every probe path is aiohttp's own 404;
- the two metadata documents, per mode, with `issuer` equal to
  `authorization_servers[0]` byte for byte, and the exact 401 challenge;
- nothing is granted: authorize never redirects, token never succeeds,
  no row is written and no Telegram message is sent;
- no log record carries a value -- not `state`, a challenge, a code, a
  token, an Authorization header, a cookie, or a client_id URL that is
  not claude.ai's public document;
- the security headers are on every response;
- the module reaches nothing it could read or write through.
"""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import text

from app.config import Settings, check_runtime_settings
from app.main import build_webhook_app
from app.web import oauth_probe

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _probe_logger_enabled(monkeypatch):
    """The session fixture runs alembic in-process, and alembic.ini's
    fileConfig disables every logger that already exists, this module's
    included. Production runs alembic in its own process first, so the
    logger is live there; these tests assert on its records."""
    monkeypatch.setattr(logging.getLogger(oauth_probe.__name__), "disabled", False)


ORIGIN = "https://anchor.example.test"
R = f"{ORIGIN}/mcp/claude"

SECRETS = {
    "state": "STATE-sentinel-7f3a",
    "code_challenge": "CHALLENGE-sentinel-91bc",
    "code": "CODE-sentinel-22de",
    "refresh_token": "REFRESH-sentinel-5e0f",
    "token": "TOKEN-sentinel-8a41",
    "bearer": "BEARER-sentinel-c0de",
    "cookie": "COOKIE-sentinel-beef",
    "code_verifier": "VERIFIER-sentinel-f00d",
    "stranger": "https://evil.example/SECRET-client-path-sentinel",
}

PROBE_PATHS = [
    ("GET", "/.well-known/oauth-protected-resource"),
    ("GET", "/.well-known/oauth-protected-resource/mcp/claude"),
    ("GET", "/.well-known/oauth-authorization-server"),
    ("POST", "/mcp/claude"),
    ("POST", "/oauth/register"),
    ("GET", "/oauth/authorize"),
    ("POST", "/oauth/token"),
    ("POST", "/oauth/revoke"),
]


def _settings(**overrides) -> Settings:
    base = dict(
        MODE="webhook",
        TELEGRAM_BOT_TOKEN="123456:TEST",
        PUBLIC_URL=ORIGIN + "/",
        CLAUDE_OAUTH_PROBE="both",
    )
    base.update(overrides)
    return Settings(**base)


def _app(settings: Settings, sessionmaker):
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    app = build_webhook_app(
        settings,
        bot,
        Dispatcher(),
        sessionmaker,
        engine=None,
        provider=None,
        cheap_provider=None,
        safety_provider=None,
        llm_client=None,
        clock=None,
        hub=None,
        code_store=None,
    )
    app.on_startup.clear()
    app.on_cleanup.clear()
    return app, bot


class _Client:
    def __init__(self, settings, sessionmaker):
        self.app, self.bot = _app(settings, sessionmaker)

    async def __aenter__(self):
        self.client = TestClient(TestServer(self.app))
        await self.client.__aenter__()
        return self.client

    async def __aexit__(self, *exc):
        await self.client.__aexit__(*exc)
        await self.bot.session.close()


async def _full_flow(client, client_id: str) -> list:
    """Everything claude.ai could do against the probe, with secrets in
    every value slot."""
    responses = [
        await client.get("/.well-known/oauth-protected-resource/mcp/claude"),
        await client.get("/.well-known/oauth-authorization-server"),
        await client.get("/.well-known/openid-configuration"),
        await client.post(
            "/mcp/claude",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            headers={"Authorization": f"Bearer {SECRETS['bearer']}"},
        ),
        await client.post(
            "/oauth/register",
            json={
                "redirect_uris": [oauth_probe.CALLBACK],
                "grant_types": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_method": "none",
                "client_name": SECRETS["cookie"],
            },
        ),
        await client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": oauth_probe.CALLBACK,
                "code_challenge": SECRETS["code_challenge"],
                "code_challenge_method": "S256",
                "resource": R,
                "scope": "anchor.read",
                "state": SECRETS["state"],
            },
            headers={"Cookie": f"__Host-anchor_oauth={SECRETS['cookie']}"},
            allow_redirects=False,
        ),
        await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": SECRETS["code"],
                "code_verifier": SECRETS["code_verifier"],
                "client_id": client_id,
                "redirect_uri": oauth_probe.CALLBACK,
                "resource": R,
            },
        ),
        await client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": SECRETS["refresh_token"],
            },
        ),
        await client.post(
            "/oauth/revoke", data={"token": SECRETS["token"], "client_id": client_id}
        ),
    ]
    return responses


# --- the switch ---


async def test_off_by_default_every_path_is_aiohttp_s_own_404(sessionmaker):
    settings = _settings(CLAUDE_OAUTH_PROBE="off")
    assert Settings().CLAUDE_OAUTH_PROBE == "off"
    async with _Client(settings, sessionmaker) as client:
        reference = await client.get("/no/such/path")
        for method, path in PROBE_PATHS:
            resp = await client.request(method, path)
            assert resp.status == 404, path
            assert await resp.text() == await reference.text() == "404: Not Found"
            assert "Content-Security-Policy" not in resp.headers


@pytest.mark.parametrize(
    "overrides",
    [
        {"CLAUDE_OAUTH_PROBE": "yes"},
        {"CLAUDE_OAUTH_PROBE": "both", "MODE": "polling"},
        {"CLAUDE_OAUTH_PROBE": "both", "PUBLIC_URL": "http://anchor.example.test"},
    ],
)
def test_a_misconfigured_probe_refuses_to_start(overrides):
    values = dict(
        MODE="webhook",
        TELEGRAM_BOT_TOKEN="123456:TEST",
        TELEGRAM_SECRET_TOKEN="secret",
        PUBLIC_URL=ORIGIN,
        OPENROUTER_API_KEY="k",
        ALLOWED_CHAT_ID=1,
        DATABASE_URL="postgresql://anchor:anchor@127.0.0.1:5432/x",
    )
    values.update(overrides)
    with pytest.raises(SystemExit, match="CLAUDE_OAUTH_PROBE"):
        check_runtime_settings(Settings(**values))


# --- metadata and the challenge ---


@pytest.mark.parametrize("mode", oauth_probe.MODES)
async def test_metadata_per_mode(sessionmaker, mode):
    async with _Client(_settings(CLAUDE_OAUTH_PROBE=mode), sessionmaker) as client:
        prm = await (
            await client.get("/.well-known/oauth-protected-resource/mcp/claude")
        ).json()
        root = await (await client.get("/.well-known/oauth-protected-resource")).json()
        asm = await (await client.get("/.well-known/oauth-authorization-server")).json()
    assert (
        prm
        == root
        == {
            "resource": R,
            "authorization_servers": [ORIGIN],
            "scopes_supported": ["anchor.read"],
            "bearer_methods_supported": ["header"],
        }
    )
    assert asm["issuer"] == prm["authorization_servers"][0] == ORIGIN
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert asm["authorization_response_iss_parameter_supported"] is True
    assert asm["token_endpoint_auth_methods_supported"] == ["none"]
    assert asm["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert asm.get("client_id_metadata_document_supported") is (
        True if mode in ("both", "cimd") else None
    )
    assert ("registration_endpoint" in asm) is (mode in ("both", "dcr"))


async def test_the_mcp_route_is_always_the_exact_401(sessionmaker):
    async with _Client(_settings(GROK_ACCESS_ENABLED=True), sessionmaker) as client:
        for headers in ({}, {"Authorization": "Bearer anything"}):
            resp = await client.post("/mcp/claude", json={}, headers=headers)
            assert resp.status == 401
            assert resp.headers["WWW-Authenticate"] == (
                f'Bearer resource_metadata="{ORIGIN}/.well-known/oauth-protected-resource'
                f'/mcp/claude", scope="anchor.read"'
            )


# --- nothing is granted ---


async def test_the_full_flow_grants_nothing_and_writes_nothing(sessionmaker):
    async with sessionmaker() as session:
        before = (
            await session.execute(
                text(
                    "select sum(n_tup_ins + n_tup_upd + n_tup_del) from pg_stat_user_tables"
                )
            )
        ).scalar_one()
    settings = _settings()
    app, bot = _app(settings, sessionmaker)
    sent = []

    async def refuse(*args, **kwargs):  # any Telegram call is a failure
        sent.append(args)
        raise AssertionError("the probe must never talk to Telegram")

    bot.__call__ = refuse
    async with TestClient(TestServer(app)) as client:
        responses = await _full_flow(
            client, "https://claude.ai/oauth/claude-code-client-metadata"
        )
        statuses = [r.status for r in responses]
        authorize = responses[5]
        assert authorize.status == 200
        assert "Location" not in authorize.headers
        assert "Проверка подключения" in await authorize.text()
        assert not any(300 <= s < 400 for s in statuses)
        for r in responses[6:8]:
            assert r.status == 400 and (await r.json())["error"] == "invalid_grant"
        registered = await responses[4].json()
        assert registered["client_id"] == oauth_probe.DRY_RUN_CLIENT_ID
    await bot.session.close()
    assert sent == []
    async with sessionmaker() as session:
        await session.execute(text("select pg_stat_clear_snapshot()"))
        after = (
            await session.execute(
                text(
                    "select sum(n_tup_ins + n_tup_upd + n_tup_del) from pg_stat_user_tables"
                )
            )
        ).scalar_one()
    assert after == before


async def test_registration_refuses_another_callback(sessionmaker):
    async with _Client(_settings(), sessionmaker) as client:
        for uris in (
            [oauth_probe.CALLBACK + "/"],
            ["http://claude.ai/api/mcp/auth_callback"],
            [oauth_probe.CALLBACK, "https://evil.example/cb"],
        ):
            resp = await client.post("/oauth/register", json={"redirect_uris": uris})
            assert resp.status == 400
        assert (await client.post("/oauth/register", data=b"not json")).status == 400


# --- logs ---


def _rendered(caplog) -> list[str]:
    return [
        record.getMessage()
        + json.dumps(record.__dict__, default=str, ensure_ascii=False)
        for record in caplog.records
    ]


@pytest.mark.parametrize(
    "client_id",
    [
        "https://claude.ai/oauth/claude-code-client-metadata",
        SECRETS["stranger"],
        "opaque-id-sentinel",
    ],
)
async def test_no_value_reaches_the_logs(sessionmaker, caplog, client_id):
    caplog.set_level(logging.DEBUG)
    async with _Client(_settings(), sessionmaker) as client:
        await _full_flow(client, client_id)
    rendered = _rendered(caplog)
    probe = [r for r in caplog.records if getattr(r, "event", None) == "oauth_probe"]
    assert len(probe) == 9
    for line in rendered:
        for secret in SECRETS.values():
            assert secret not in line
        assert "opaque-id-sentinel" not in line
        assert "evil.example/" not in line


async def test_the_logs_say_what_c2_needs(sessionmaker, caplog):
    caplog.set_level(logging.INFO)
    async with _Client(_settings(), sessionmaker) as client:
        await _full_flow(client, "https://claude.ai/oauth/claude-code-client-metadata")
    by_route = {}
    for record in caplog.records:
        if getattr(record, "event", None) == "oauth_probe":
            by_route.setdefault(record.route, record)
    authorize = by_route["/oauth/authorize"]
    assert authorize.fields == (
        "client_id,code_challenge,code_challenge_method,redirect_uri,resource,"
        "response_type,scope,state"
    )
    assert authorize.client_id_kind == "url"
    assert authorize.client_host == "claude.ai"
    assert authorize.client_id_path == "/oauth/claude-code-client-metadata"
    assert authorize.redirect_uri_expected is True
    assert authorize.resource_form == "exact"
    assert authorize.pkce_method == "S256"
    assert "cookie" in authorize.header_names
    register = by_route["/oauth/register"]
    assert register.grant_type == "authorization_code+refresh_token"
    assert register.auth_method == "none"
    assert by_route["unrouted"].fields == "/.well-known/openid-configuration"
    assert by_route["/mcp/claude"].http_method == "POST"
    assert "authorization" in by_route["/mcp/claude"].header_names


async def test_a_stranger_client_id_is_logged_as_its_host_only(sessionmaker, caplog):
    caplog.set_level(logging.INFO)
    async with _Client(_settings(), sessionmaker) as client:
        await client.get("/oauth/authorize", params={"client_id": SECRETS["stranger"]})
    (record,) = [
        r for r in caplog.records if getattr(r, "event", None) == "oauth_probe"
    ]
    assert record.client_host == "evil.example"
    assert not hasattr(record, "client_id_path")


async def test_an_unrouted_non_discovery_path_is_not_logged(sessionmaker, caplog):
    caplog.set_level(logging.DEBUG)
    async with _Client(_settings(), sessionmaker) as client:
        await client.get("/mcp/" + SECRETS["token"])
        await client.get("/.well-known/" + SECRETS["token"].upper())
    for line in _rendered(caplog):
        assert SECRETS["token"] not in line and SECRETS["token"].upper() not in line


@pytest.mark.parametrize(
    "value, form",
    [
        (None, "absent"),
        (R, "exact"),
        (R + "/", "slash"),
        ("HTTPS://Anchor.Example.Test/mcp/claude", "case"),
        ("HTTPS://Anchor.Example.Test/mcp/claude/", "case_slash"),
        (f"{ORIGIN}/mcp/Claude", "other"),
        (f"{ORIGIN}/mcp/claude?x=1", "other"),
        ("http://anchor.example.test/mcp/claude", "other"),
    ],
)
def test_compare_resource(value, form):
    assert oauth_probe.compare_resource(value, R) == form


# --- headers ---


async def test_security_headers_on_every_probe_response(sessionmaker):
    async with _Client(_settings(), sessionmaker) as client:
        for response in await _full_flow(client, "x"):
            if response.status == 404:
                continue  # the unrouted discovery path: aiohttp's own
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["Referrer-Policy"] == "no-referrer"
            assert (
                "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
            )


# --- isolation ---

ALLOWED_IMPORTS = {
    "__future__",
    "json",
    "logging",
    "re",
    "urllib.parse",
    "aiohttp",
    "app.config",
}


def test_the_probe_imports_nothing_that_reads_or_writes():
    tree = ast.parse(Path(oauth_probe.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported <= ALLOWED_IMPORTS, imported - ALLOWED_IMPORTS

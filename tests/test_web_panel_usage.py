"""app/web/panels/usage.py end to end: the Лимиты screen.

Covers: 401 without a session, 403 from a foreign origin; GET
/api/usage's spend (today, the 14-day series with every date present,
per category and per model), its quotas (only for switched-on features)
and Claude's used counts (null while Claude access is off); GET
/api/usage/openrouter passing only numbers through (never the key or its
label), `available: false` on failure, and its cache; and the `used`
field on /api/state's claude_write_limits.
"""

from __future__ import annotations

import datetime
import decimal

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.db.models import ClaudeChangeset, OauthConnection, SpendLedger
from app.web.panels import usage as usage_panel
from test_web_panel_settings import (
    START,
    _app,
    _get,
    _log_in,
    _seed_state,
)

pytestmark = pytest.mark.asyncio

TODAY = datetime.date(2026, 1, 1)  # START in Europe/Paris


async def _spend(sessionmaker, rows) -> None:
    async with sessionmaker() as session:
        for local_date, category, model, usd in rows:
            session.add(
                SpendLedger(local_date=local_date, category=category, model=model, usd_cost=decimal.Decimal(usd))
            )
        await session.commit()


async def _connection(sessionmaker) -> int:
    async with sessionmaker() as session:
        connection = OauthConnection(
            client_id="usage-test", created_at=START, expires_at=START + datetime.timedelta(days=30)
        )
        session.add(connection)
        await session.commit()
        await session.refresh(connection)
        return connection.id


async def _changeset(sessionmaker, connection_id, *, kind="write", minutes_ago=5, created=0, bytes_=0, moves=0):
    when = START - datetime.timedelta(minutes=minutes_ago)
    async with sessionmaker() as session:
        session.add(
            ClaudeChangeset(
                connection_id=connection_id,
                vault_ref=f"ref-{kind}-{minutes_ago}",
                kind=kind,
                files=1,
                bytes=bytes_,
                created=created,
                moves=moves,
                created_at=when,
                last_write_at=when,
            )
        )
        await session.commit()


async def test_usage_requires_a_session(sessionmaker):
    app, _hub, _fake = await _app(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        assert (await _get(client, "/api/usage")).status == 401
        assert (await _get(client, "/api/usage/openrouter")).status == 401


async def test_usage_rejects_a_foreign_origin(sessionmaker):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(
            client, "/api/usage", cookies=cookies,
            headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
        )
        assert resp.status == 403


async def test_usage_spend_today_and_history(sessionmaker):
    await _seed_state(sessionmaker)
    await _spend(
        sessionmaker,
        [
            (TODAY, "chat", "main/model", "0.30"),
            (TODAY, "idle:reflect", "cheap/model", "0.05"),
            (TODAY - datetime.timedelta(days=2), "chat", "main/model", "0.20"),
            (TODAY - datetime.timedelta(days=2), "summary", None, "0.01"),
            # Outside the 14-day window and in the future: never counted.
            (TODAY - datetime.timedelta(days=14), "chat", "main/model", "9.00"),
            (TODAY + datetime.timedelta(days=1), "chat", "main/model", "9.00"),
        ],
    )
    app, _hub, fake = await _app(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(client, "/api/usage", cookies=cookies)
        assert resp.status == 200
        body = await resp.json()

    spend = body["spend"]
    assert spend["today_usd"] == pytest.approx(0.35)
    assert spend["cap_usd"] == 1.0
    assert spend["idle_today_usd"] == pytest.approx(0.05)
    assert spend["idle_cap_usd"] == 0.25

    history = spend["history"]
    assert len(history) == usage_panel.HISTORY_DAYS
    assert history[0]["date"] == (TODAY - datetime.timedelta(days=13)).isoformat()
    assert history[-1]["date"] == TODAY.isoformat()
    assert history[-1]["usd"] == pytest.approx(0.35)
    assert list(history[-1]["by_category"]) == ["chat", "idle:reflect"]
    assert history[-3]["usd"] == pytest.approx(0.21)
    assert history[-2] == {"date": (TODAY - datetime.timedelta(days=1)).isoformat(), "usd": 0, "by_category": {}}
    assert sum(day["usd"] for day in history) == pytest.approx(0.56)

    assert spend["by_model"] == {
        "main/model": pytest.approx(0.50),
        "cheap/model": pytest.approx(0.05),
        "—": pytest.approx(0.01),
    }


async def test_usage_quotas_follow_the_feature_switches(sessionmaker):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app(sessionmaker, IDLE_ENABLED=False, OUTBOUND_ENABLED=False)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        body = await (await _get(client, "/api/usage", cookies=cookies)).json()
    assert [q["key"] for q in body["quotas"]] == ["web_sends"]
    assert body["spend"]["idle_cap_usd"] is None
    assert body["claude"] is None

    app, _hub, fake = await _app(sessionmaker, RESEARCH_ENABLED=True, PLANNER_ENABLED=True)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        body = await (await _get(client, "/api/usage", cookies=cookies)).json()
    quotas = {q["key"]: q for q in body["quotas"]}
    assert list(quotas) == ["unsolicited", "idle_jobs", "study", "read", "planner_writes", "web_sends"]
    assert quotas["unsolicited"] == {
        "key": "unsolicited", "label": "Сообщений первой", "used": 0, "limit": 3, "window": "day",
    }
    assert quotas["planner_writes"]["limit"] == 20
    assert all(q["used"] == 0 for q in quotas.values())


async def test_usage_claude_counts(sessionmaker):
    await _seed_state(sessionmaker)
    connection_id = await _connection(sessionmaker)
    await _changeset(sessionmaker, connection_id, minutes_ago=5, created=2, bytes_=300)
    await _changeset(sessionmaker, connection_id, minutes_ago=90, created=1, moves=4)
    await _changeset(sessionmaker, connection_id, kind="undo", minutes_ago=10)
    app, _hub, fake = await _app(sessionmaker, CLAUDE_ACCESS_ENABLED=True)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        body = await (await _get(client, "/api/usage", cookies=cookies)).json()
        state = await (await _get(client, "/api/state", cookies=cookies)).json()

    claude = {row["key"]: row for row in body["claude"]}
    assert list(claude) == [
        "changesets_per_hour", "creates_per_day", "bytes_per_day",
        "undos_per_hour", "folders_per_day", "moves_per_day",
    ]
    assert claude["changesets_per_hour"]["used"] == 1  # the 90-minute-old one is outside the hour
    assert claude["changesets_per_hour"]["window"] == "hour"
    assert claude["creates_per_day"] == {
        "key": "creates_per_day", "label": "Новых заметок в день", "used": 3, "limit": 40,
        "unit": "count", "window": "day",
    }
    assert claude["bytes_per_day"]["used"] == 300 and claude["bytes_per_day"]["unit"] == "bytes"
    assert claude["undos_per_hour"]["used"] == 1
    assert claude["moves_per_day"]["used"] == 4

    limits = {row["key"]: row for row in state["claude_write_limits"]}
    assert limits["creates_per_day"]["used"] == 3
    assert limits["files_per_changeset"]["used"] is None


async def test_usage_claude_counts_without_a_connection(sessionmaker):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app(sessionmaker, CLAUDE_ACCESS_ENABLED=True)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        body = await (await _get(client, "/api/usage", cookies=cookies)).json()
    assert body["claude"] and all(row["used"] == 0 for row in body["claude"])


async def test_openrouter_passes_only_numbers_and_caches(sessionmaker, monkeypatch):
    await _seed_state(sessionmaker)
    calls = []

    async def fake_fetch(api_key):
        calls.append(api_key)
        return {
            "limit": 20.0, "limit_remaining": 12.5, "limit_reset": "monthly", "usage": 7.5,
            "usage_daily": 0.4, "usage_weekly": 2.0, "usage_monthly": 7.5, "is_free_tier": False,
            "balance": 31.25,
        }

    monkeypatch.setattr(usage_panel, "fetch_openrouter", fake_fetch)
    app, _hub, fake = await _app(sessionmaker, OPENROUTER_API_KEY="sk-or-SECRET")
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(client, "/api/usage/openrouter", cookies=cookies)
        raw = await resp.text()
        again = await (await _get(client, "/api/usage/openrouter", cookies=cookies)).json()
    assert resp.status == 200
    assert "SECRET" not in raw
    assert calls == ["sk-or-SECRET"]  # the second request came from the cache
    assert again["available"] is True
    assert again["balance"] == 31.25 and again["limit_remaining"] == 12.5


async def test_openrouter_failure_is_unavailable(sessionmaker, monkeypatch):
    await _seed_state(sessionmaker)

    async def failing_fetch(api_key):
        raise usage_panel.aiohttp.ClientError("boom sk-or-SECRET")

    monkeypatch.setattr(usage_panel, "fetch_openrouter", failing_fetch)
    app, _hub, fake = await _app(sessionmaker, OPENROUTER_API_KEY="sk-or-SECRET")
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(client, "/api/usage/openrouter", cookies=cookies)
        body = await resp.json()
    assert resp.status == 200
    assert body["available"] is False


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._body


class _FakeHttp:
    def __init__(self, responses):
        self._responses = responses
        self.headers = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, headers):
        self.headers.append(headers)
        return _FakeResponse(*self._responses[url])


async def test_fetch_openrouter_drops_the_label_and_tolerates_no_credits(monkeypatch):
    http = _FakeHttp({
        usage_panel.OPENROUTER_KEY_URL: (200, {"data": {
            "label": "sk-or-v1-abc...xyz", "limit": None, "limit_remaining": None, "usage": 1.5,
            "usage_daily": 0.1, "usage_weekly": 0.5, "usage_monthly": 1.5, "is_free_tier": False,
        }}),
        usage_panel.OPENROUTER_CREDITS_URL: (403, {"error": {"message": "management key required"}}),
    })
    monkeypatch.setattr(usage_panel.aiohttp, "ClientSession", lambda **_kw: http)
    result = await usage_panel.fetch_openrouter("sk-or-SECRET")
    assert "label" not in result
    assert result["usage"] == 1.5 and result["limit"] is None and result["balance"] is None
    assert http.headers[0] == {"Authorization": "Bearer sk-or-SECRET"}

    http = _FakeHttp({
        usage_panel.OPENROUTER_KEY_URL: (200, {"data": {"usage": 2}}),
        usage_panel.OPENROUTER_CREDITS_URL: (200, {"data": {"total_credits": 10, "total_usage": 2.5}}),
    })
    monkeypatch.setattr(usage_panel.aiohttp, "ClientSession", lambda **_kw: http)
    result = await usage_panel.fetch_openrouter("sk-or-SECRET")
    assert result["balance"] == 7.5

    http = _FakeHttp({
        usage_panel.OPENROUTER_KEY_URL: (401, {}),
        usage_panel.OPENROUTER_CREDITS_URL: (200, {}),
    })
    monkeypatch.setattr(usage_panel.aiohttp, "ClientSession", lambda **_kw: http)
    assert await usage_panel.fetch_openrouter("sk-or-SECRET") is None

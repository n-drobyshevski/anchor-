"""app/web/board_sync_trigger.py: `POST /planner/board-sync`, the planner's
"Sync with Obsidian" button. A fake Supabase (`/auth/v1/user`) on a local
test server stands in for the planner's project, and a fake worker marks
the queued job done; every token, id and title here is synthetic.

What each test pins: the gates (plain 404), the header check (401), the
rate limit (429), Supabase's verdict (401) and the linked-account check
(403, 503), the enqueue and its trigger, the wait (done / queued /
already_queued / failed), and that no log line carries a token, an id or
a board name.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import json
import logging

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from app.config import Settings
from app.db.models import BoardSyncRun, Job, PlannerCredential
from app.planner import board_job
from app.web import board_sync_trigger

pytestmark = pytest.mark.asyncio

USER_ID = "0b6f7c1e-1111-4a2b-9c3d-5e6f7a8b9c0d"
OTHER_ID = "9f8e7d6c-2222-4b3a-8c2d-1e0f9a8b7c6d"
ANON = "anon-public-key-for-tests"
BOARD = "Секретная доска.md"
TZ = "Europe/Paris"


def _jwt(sub: str | None, *, marker: str = "sig") -> str:
    def seg(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    claims = {"aud": "authenticated"}
    if sub is not None:
        claims["sub"] = sub
    return f"{seg({'alg': 'HS256'})}.{seg(claims)}.{marker}{'x' * 20}"


USER_TOKEN = _jwt(USER_ID, marker="usertoken")


class FakeSupabase:
    """`GET /auth/v1/user`: the user for a known token, else 401."""

    def __init__(self, users: dict[str, str] | None = None, *, status: int | None = None) -> None:
        self.users = users if users is not None else {USER_TOKEN: USER_ID}
        self.status = status
        self.calls: list[dict] = []

    def app(self) -> web.Application:
        application = web.Application()
        application.router.add_get("/auth/v1/user", self._user)
        return application

    async def _user(self, request: web.Request) -> web.Response:
        self.calls.append({"authorization": request.headers.get("Authorization"), "apikey": request.headers.get("apikey")})
        if self.status is not None:
            return web.json_response({"msg": "nope"}, status=self.status)
        token = (request.headers.get("Authorization") or "").removeprefix("Bearer ")
        if request.headers.get("apikey") != ANON or token not in self.users:
            return web.json_response({"msg": "invalid JWT"}, status=401)
        return web.json_response({"id": self.users[token], "aud": "authenticated"})


def _settings(supabase_url: str, **extra) -> Settings:
    values = {
        "_env_file": None,
        "PLANNER_ENABLED": True,
        "PLANNER_SUPABASE_URL": supabase_url,
        "PLANNER_SUPABASE_ANON_KEY": ANON,
        "BOARD_SYNC_ENABLED": True,
        "VAULT_MODE": "sync",
        "VAULT_API_TOKEN": "x" * 32,
    }
    values.update(extra)
    return Settings(**values)


async def _link(sessionmaker, clock, *, access_token: str | None = None, status: str = "active") -> None:
    async with sessionmaker() as session:
        session.add(
            PlannerCredential(
                id=1,
                access_token=access_token if access_token is not None else _jwt(USER_ID, marker="echotoken"),
                refresh_token="r",
                expires_at=clock.now_utc() + datetime.timedelta(hours=1),
                status=status,
            )
        )
        await session.commit()


def _app(settings: Settings, sessionmaker, clock) -> web.Application:
    app = web.Application()
    app["settings"] = settings
    app["sessionmaker"] = sessionmaker
    app["clock"] = clock
    board_sync_trigger.register(app)
    return app


@pytest.fixture()
def fast_wait(monkeypatch):
    monkeypatch.setattr(board_sync_trigger, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(board_sync_trigger, "WAIT_SECONDS", 1.0)


class _World:
    def __init__(self, sessionmaker, clock, supabase: FakeSupabase | None = None, **settings_extra) -> None:
        self.sessionmaker = sessionmaker
        self.clock = clock
        self.supabase = supabase or FakeSupabase()
        self.settings_extra = settings_extra

    async def __aenter__(self):
        self._supa = TestClient(TestServer(self.supabase.app()))
        await self._supa.__aenter__()
        url = str(self._supa.make_url("")).rstrip("/")
        self.settings = _settings(url, **self.settings_extra)
        self._client = TestClient(TestServer(_app(self.settings, self.sessionmaker, self.clock)))
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self._client.__aexit__(*exc)
        await self._supa.__aexit__(*exc)

    async def post(self, token: str | None = USER_TOKEN, *, header: str | None = None):
        headers = {}
        if header is not None:
            headers["Authorization"] = header
        elif token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return await self._client.post(board_sync_trigger.PATH, headers=headers)


async def _jobs(sessionmaker) -> list[Job]:
    async with sessionmaker() as session:
        return list((await session.execute(select(Job).order_by(Job.id))).scalars().all())


async def _fake_worker(sessionmaker, clock, *, runs, status: str = "done") -> None:
    """Wait for the queued pass, write its run rows as run_pass would, finish it."""
    for _ in range(200):
        async with sessionmaker() as session:
            job = (
                await session.execute(select(Job).where(Job.kind == board_job.BOARD_SYNC, Job.status == "pending"))
            ).scalar_one_or_none()
            if job is not None:
                now = clock.now_utc()
                for board, outcome, counts in runs:
                    session.add(BoardSyncRun(at=now, board=board, outcome=outcome, **counts))
                job.status = status
                await session.commit()
                return
        await asyncio.sleep(0.01)
    raise AssertionError("no pass was queued")


# -- gates ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"BOARD_SYNC_ENABLED": False},
        {"PLANNER_ENABLED": False},
        {"PLANNER_SUPABASE_ANON_KEY": ""},
        {"VAULT_MODE": "off"},
    ],
    ids=["board_sync_off", "planner_off", "no_anon_key", "vault_off"],
)
async def test_a_shut_gate_is_a_plain_404(sessionmaker, frozen_clock, extra):
    clock = frozen_clock(2026, 11, 11, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    async with _World(sessionmaker, clock, **extra) as world:
        resp = await world.post()
        assert resp.status == 404
        assert await resp.text() == "404: Not Found"
        assert world.supabase.calls == []
    assert await _jobs(sessionmaker) == []


@pytest.mark.parametrize("status", [None, "revoked"], ids=["never_linked", "revoked"])
async def test_no_active_planner_link_is_a_404(sessionmaker, frozen_clock, status):
    clock = frozen_clock(2026, 11, 11, 9, 0, tz=TZ)
    if status is not None:
        await _link(sessionmaker, clock, status=status)
    async with _World(sessionmaker, clock) as world:
        resp = await world.post()
        assert resp.status == 404
        assert world.supabase.calls == []


# -- the caller ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    ["", "Basic abcdefghijklmnopqrstuvwxyz", "Bearer short", f"bearer {USER_TOKEN}", f"Bearer {USER_TOKEN} x", "Bearer " + "a" * 4097],
    ids=["empty", "basic", "short", "lowercase", "trailing", "too_long"],
)
async def test_a_malformed_authorization_header_is_401_before_supabase(sessionmaker, frozen_clock, header):
    clock = frozen_clock(2026, 11, 11, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    async with _World(sessionmaker, clock) as world:
        resp = await world.post(header=header)
        assert resp.status == 401
        assert world.supabase.calls == []
    assert await _jobs(sessionmaker) == []


async def test_a_missing_header_is_401(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 11, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    async with _World(sessionmaker, clock) as world:
        resp = await world.post(token=None)
        assert resp.status == 401


async def test_a_token_supabase_refuses_is_401(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 11, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    async with _World(sessionmaker, clock) as world:
        resp = await world.post(_jwt(USER_ID, marker="forged"))
        assert resp.status == 401
        assert world.supabase.calls[0]["apikey"] == ANON
    assert await _jobs(sessionmaker) == []


async def test_a_supabase_error_status_is_401(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 11, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    async with _World(sessionmaker, clock, FakeSupabase(status=500)) as world:
        resp = await world.post()
        assert resp.status == 401


async def test_an_unreachable_supabase_is_503(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 11, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    settings = _settings("http://127.0.0.1:9")
    async with TestClient(TestServer(_app(settings, sessionmaker, clock))) as client:
        resp = await client.post(board_sync_trigger.PATH, headers={"Authorization": f"Bearer {USER_TOKEN}"})
        assert resp.status == 503
    assert await _jobs(sessionmaker) == []


async def test_another_planner_account_is_403(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 11, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    other = _jwt(OTHER_ID, marker="othertoken")
    async with _World(sessionmaker, clock, FakeSupabase({other: OTHER_ID})) as world:
        resp = await world.post(other)
        assert resp.status == 403
        assert await resp.json() == {"error": "other_account"}
    assert await _jobs(sessionmaker) == []


@pytest.mark.parametrize("stored", ["not-a-jwt", _jwt(None), "a.!!!.b"], ids=["opaque", "no_sub", "garbage"])
async def test_an_unreadable_stored_token_is_503(sessionmaker, frozen_clock, stored):
    clock = frozen_clock(2026, 11, 11, 9, 0, tz=TZ)
    await _link(sessionmaker, clock, access_token=stored)
    async with _World(sessionmaker, clock) as world:
        resp = await world.post()
        assert resp.status == 503
        assert await resp.json() == {"error": "no_linked_account"}
    assert await _jobs(sessionmaker) == []


async def test_the_rate_limit_stops_the_seventh_call_in_a_minute(sessionmaker, frozen_clock, fast_wait, monkeypatch):
    clock = frozen_clock(2026, 11, 11, 9, 0, tz=TZ)
    await _link(sessionmaker, clock, access_token=_jwt(OTHER_ID))
    async with _World(sessionmaker, clock) as world:
        statuses = [(await world.post()).status for _ in range(board_sync_trigger.RATE_PER_MINUTE + 1)]
    # The account check fails each time (403), but each one counted.
    assert statuses == [403] * board_sync_trigger.RATE_PER_MINUTE + [429]
    assert len(world.supabase.calls) == board_sync_trigger.RATE_PER_MINUTE


# -- the pass --------------------------------------------------------------------------------


async def test_the_linked_account_queues_a_planner_pass_and_gets_its_counts(sessionmaker, frozen_clock, fast_wait):
    clock = frozen_clock(2026, 11, 11, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    async with _World(sessionmaker, clock) as world:
        worker = asyncio.create_task(
            _fake_worker(
                sessionmaker, clock,
                runs=[(BOARD, "ok", {"cards_changed": 3, "tasks_created": 1}), ("General tasks.md", "changed", {})],
            )
        )
        resp = await world.post()
        await worker
        assert resp.status == 200
        body = await resp.json()
        assert world.supabase.calls == [{"authorization": f"Bearer {USER_TOKEN}", "apikey": ANON}]
    assert body["status"] == "done"
    assert body["outcome"] == "ok"
    assert [b["outcome"] for b in body["boards"]] == ["ok", "changed"]
    assert body["boards"][0]["cards_changed"] == 3 and body["boards"][0]["tasks_created"] == 1
    assert set(body["boards"][0]) == {"outcome", *board_job.Counts.__dataclass_fields__}
    assert BOARD not in json.dumps(body, ensure_ascii=False)
    (job,) = await _jobs(sessionmaker)
    assert job.payload == {"trigger": board_job.TRIGGER_PLANNER}


async def test_a_pass_that_stopped_reports_its_outcome(sessionmaker, frozen_clock, fast_wait):
    clock = frozen_clock(2026, 11, 11, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    async with _World(sessionmaker, clock) as world:
        worker = asyncio.create_task(_fake_worker(sessionmaker, clock, runs=[(None, "planner_unavailable", {})]))
        resp = await world.post()
        await worker
        assert resp.status == 200
        assert await resp.json() == {"status": "done", "outcome": "planner_unavailable", "boards": []}


async def test_a_failed_job_says_failed(sessionmaker, frozen_clock, fast_wait):
    clock = frozen_clock(2026, 11, 11, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    async with _World(sessionmaker, clock) as world:
        worker = asyncio.create_task(_fake_worker(sessionmaker, clock, runs=[], status="failed"))
        resp = await world.post()
        await worker
        assert await resp.json() == {"status": "failed"}


async def test_older_runs_are_not_this_pass(sessionmaker, frozen_clock, fast_wait):
    clock = frozen_clock(2026, 11, 11, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    async with sessionmaker() as session:
        session.add(BoardSyncRun(at=clock.now_utc() - datetime.timedelta(minutes=15), board=BOARD, outcome="ok", cards_added=9))
        await session.commit()
    async with _World(sessionmaker, clock) as world:
        worker = asyncio.create_task(_fake_worker(sessionmaker, clock, runs=[]))
        resp = await world.post()
        await worker
        assert await resp.json() == {"status": "done", "outcome": "ok", "boards": []}


async def test_a_slow_pass_answers_202_queued(sessionmaker, frozen_clock, fast_wait, monkeypatch):
    monkeypatch.setattr(board_sync_trigger, "WAIT_SECONDS", 0.1)
    clock = frozen_clock(2026, 11, 11, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    async with _World(sessionmaker, clock) as world:
        resp = await world.post()
        assert resp.status == 202
        assert await resp.json() == {"status": "queued"}
    (job,) = await _jobs(sessionmaker)
    assert job.status == "pending"


async def test_a_second_tap_in_the_same_minute_is_already_queued(sessionmaker, frozen_clock, fast_wait, monkeypatch):
    monkeypatch.setattr(board_sync_trigger, "WAIT_SECONDS", 0.05)
    clock = frozen_clock(2026, 11, 11, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    async with _World(sessionmaker, clock) as world:
        first = await world.post()
        second = await world.post()
        assert first.status == 202
        assert second.status == 200
        assert await second.json() == {"status": "already_queued"}
    assert len(await _jobs(sessionmaker)) == 1


# -- privacy ---------------------------------------------------------------------------------


async def test_logs_carry_events_only(sessionmaker, frozen_clock, fast_wait, caplog, monkeypatch):
    # alembic's fileConfig in the session fixture disables existing
    # loggers (tests/test_board_job.py does the same): re-enable.
    monkeypatch.setattr(logging.getLogger("app.web.board_sync_trigger"), "disabled", False)
    clock = frozen_clock(2026, 11, 11, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    other = _jwt(OTHER_ID, marker="othertoken")
    caplog.set_level(logging.DEBUG, logger="app.web.board_sync_trigger")
    async with _World(sessionmaker, clock, FakeSupabase({USER_TOKEN: USER_ID, other: OTHER_ID})) as world:
        worker = asyncio.create_task(_fake_worker(sessionmaker, clock, runs=[(BOARD, "ok", {"cards_added": 1})]))
        assert (await world.post()).status == 200
        await worker
        assert (await world.post(other)).status == 403
        assert (await world.post(header="Bearer nope")).status == 401
    records = [r for r in caplog.records if r.name == "app.web.board_sync_trigger"]
    events = [r.event for r in records]
    assert events == [
        "board_sync_trigger_queued",
        "board_sync_trigger_done",
        "board_sync_trigger_other_account",
        "board_sync_trigger_bad_header",
    ]
    for record in records:
        dumped = json.dumps({k: str(v) for k, v in record.__dict__.items()}, ensure_ascii=False)
        for secret in (USER_TOKEN, other, USER_ID, OTHER_ID, BOARD, ANON):
            assert secret not in dumped


def test_token_subject_reads_the_payload_without_verifying():
    assert board_sync_trigger.token_subject(_jwt(USER_ID)) == USER_ID
    assert board_sync_trigger.token_subject(_jwt(None)) is None
    assert board_sync_trigger.token_subject("only.two") is None
    assert board_sync_trigger.token_subject("a.bm90IGpzb24.c") is None

"""app/planner/auth.py: OAuth PKCE, discovery, rotation and revocation.

The Supabase authorization server is stood in with a tiny aiohttp app
serving `.well-known/oauth-authorization-server` and `/token` -- no
real network, and no new dependency (aiohttp ships test_utils).
"""

from __future__ import annotations

import asyncio
import datetime

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from app.config import Settings
from app.db.models import PlannerCredential
from app.planner import auth

pytestmark = pytest.mark.asyncio


class _FakeAuthServer:
    def __init__(self, *, refresh_reply: dict | None = None, invalid_grant: bool = False) -> None:
        self.refresh_calls = 0
        self.token_calls: list[dict] = []
        self.refresh_reply = refresh_reply
        self.invalid_grant = invalid_grant

    def app(self) -> web.Application:
        application = web.Application()
        application.router.add_get(
            "/auth/v1/.well-known/oauth-authorization-server", self._meta
        )
        application.router.add_post("/auth/v1/token", self._token)
        return application

    async def _meta(self, request: web.Request) -> web.Response:
        # Computed from the request's own origin, not a precomputed base
        # -- the test server's port is only known once it is listening,
        # and this way the app can be built before that.
        base = f"{request.scheme}://{request.host}"
        return web.json_response(
            {
                "authorization_endpoint": f"{base}/auth/v1/authorize",
                "token_endpoint": f"{base}/auth/v1/token",
            }
        )

    async def _token(self, request: web.Request) -> web.Response:
        data = dict(await request.post())
        self.token_calls.append(data)
        if data.get("grant_type") == "refresh_token":
            self.refresh_calls += 1
            if self.invalid_grant:
                return web.json_response({"error": "invalid_grant"}, status=400)
            reply = self.refresh_reply or {
                "access_token": f"new-access-{self.refresh_calls}",
                "refresh_token": f"new-refresh-{self.refresh_calls}",
                "expires_in": 3600,
            }
            return web.json_response(reply)
        if data.get("grant_type") == "authorization_code":
            return web.json_response(
                {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600}
            )
        return web.json_response({"error": "unsupported_grant_type"}, status=400)


def _settings(supabase_url: str) -> Settings:
    return Settings(
        _env_file=None,
        PLANNER_ENABLED=True,
        PLANNER_SUPABASE_URL=supabase_url,
        PLANNER_OAUTH_CLIENT_ID="cid",
        PLANNER_OAUTH_REDIRECT_URI="https://anchor.example/planner/oauth/callback",
    )


async def _seed_credential(sessionmaker, clock, *, expires_soon: bool = True) -> None:
    expires_at = clock.now_utc() + (
        datetime.timedelta(seconds=1) if expires_soon else datetime.timedelta(hours=1)
    )
    async with sessionmaker() as session:
        session.add(
            PlannerCredential(
                id=1, access_token="old-access", refresh_token="old-refresh",
                expires_at=expires_at, status="active",
            )
        )
        await session.commit()


def test_generate_pkce_challenge_matches_verifier():
    import base64
    import hashlib

    verifier, challenge = auth.generate_pkce()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


async def test_a_fresh_token_is_returned_without_a_network_call(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0)
    await _seed_credential(sessionmaker, clock, expires_soon=False)
    settings = _settings("https://unused.invalid")
    async with sessionmaker() as session:
        token = await auth.get_access_token(session, settings, clock)
    assert token == "old-access"


async def test_the_rotated_refresh_token_is_persisted_before_being_returned(
    sessionmaker, frozen_clock
):
    clock = frozen_clock(2026, 9, 23, 9, 0)
    await _seed_credential(sessionmaker, clock, expires_soon=True)
    fake = _FakeAuthServer()
    async with TestClient(TestServer(fake.app())) as client:
        settings = _settings(str(client.make_url("")).rstrip("/"))
        async with sessionmaker() as session:
            token = await auth.get_access_token(session, settings, clock)
        assert token == "new-access-1"

        async with sessionmaker() as session:
            row = (
                await session.execute(select(PlannerCredential).where(PlannerCredential.id == 1))
            ).scalar_one()
        assert row.refresh_token == "new-refresh-1"
        assert row.access_token == "new-access-1"


async def test_a_concurrent_refresh_only_hits_the_token_endpoint_once(
    sessionmaker, frozen_clock
):
    clock = frozen_clock(2026, 9, 23, 9, 0)
    await _seed_credential(sessionmaker, clock, expires_soon=True)
    fake = _FakeAuthServer()
    async with TestClient(TestServer(fake.app())) as client:
        settings = _settings(str(client.make_url("")).rstrip("/"))

        async def _get():
            async with sessionmaker() as session:
                return await auth.get_access_token(session, settings, clock)

        results = await asyncio.gather(_get(), _get())
    # in-process asyncio.Lock serializes both calls onto one refresh
    assert fake.refresh_calls == 1
    assert len(set(results)) == 1


async def test_invalid_grant_revokes_and_the_notice_gate_fires_once(
    sessionmaker, frozen_clock
):
    clock = frozen_clock(2026, 9, 23, 9, 0)
    await _seed_credential(sessionmaker, clock, expires_soon=True)
    fake = _FakeAuthServer(invalid_grant=True)
    async with TestClient(TestServer(fake.app())) as client:
        settings = _settings(str(client.make_url("")).rstrip("/"))

        async with sessionmaker() as session:
            with pytest.raises(auth.PlannerAuthError):
                await auth.get_access_token(session, settings, clock)

        async with sessionmaker() as session:
            row = await session.get(PlannerCredential, 1)
        assert row.status == auth.REVOKED

        async with sessionmaker() as session:
            first = await auth.mark_notice_sent(session)
        async with sessionmaker() as session:
            second = await auth.mark_notice_sent(session)
    assert first is True
    assert second is False


async def test_get_access_token_without_a_credential_raises(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0)
    settings = _settings("https://unused.invalid")
    async with sessionmaker() as session:
        with pytest.raises(auth.PlannerAuthError):
            await auth.get_access_token(session, settings, clock)


async def test_is_enabled_reflects_status_and_the_local_toggle(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0)
    async with sessionmaker() as session:
        assert await auth.is_enabled(session) is False  # never linked

    await _seed_credential(sessionmaker, clock, expires_soon=False)
    async with sessionmaker() as session:
        assert await auth.is_enabled(session) is True

    async with sessionmaker() as session:
        await auth.set_enabled(session, False)
    async with sessionmaker() as session:
        assert await auth.is_enabled(session) is False

"""The Claude connector's authorization server (connector plan sections 4, 5, 10).

Every refusal the plan names has its own test here. The promises:
- nothing is approved except by typing the code from the waiting page
  into Telegram, and no web request ever sends a Telegram message;
- a failed authorize never redirects and never writes a row;
- codes are single use, bound to the client, the redirect, the PKCE
  challenge and the resource; a replay revokes what they issued;
- refresh tokens rotate; reuse after 30 s revokes the connection; the
  connection's 30 days are absolute;
- one connection at a time; the flag off revokes everything, for good.
"""

from __future__ import annotations

import asyncio
import datetime
import urllib.parse

import pytest
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from app.core import grants
from app.db.models import AccessGrant, OauthConnection, OauthToken
from app.web import oauth, oauth_store
from claude_helpers import CHALLENGE, PUBLIC_URL, R, STATE, World, settings

pytestmark = pytest.mark.asyncio


async def _world(sessionmaker, **overrides) -> World:
    world = World(sessionmaker, settings(**overrides) if overrides else None)
    await world.seed()
    return world


# --- metadata and the challenge ---


async def test_metadata_documents(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        prm = await (await client.get("/.well-known/oauth-protected-resource/mcp/claude")).json()
        root = await (await client.get("/.well-known/oauth-protected-resource")).json()
        asm = await (await client.get("/.well-known/oauth-authorization-server")).json()
    assert prm == root == {
        "resource": R,
        "authorization_servers": [PUBLIC_URL],
        "scopes_supported": ["anchor.read"],
        "bearer_methods_supported": ["header"],
    }
    assert asm["issuer"] == prm["authorization_servers"][0]
    assert asm["authorization_endpoint"] == PUBLIC_URL + "/oauth/authorize"
    assert asm["token_endpoint"] == PUBLIC_URL + "/oauth/token"
    assert asm["revocation_endpoint"] == PUBLIC_URL + "/oauth/revoke"
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert asm["authorization_response_iss_parameter_supported"] is True
    assert asm["response_types_supported"] == ["code"]
    assert asm["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert asm["token_endpoint_auth_methods_supported"] == ["none"]
    assert asm["revocation_endpoint_auth_methods_supported"] == ["none"]
    assert asm["client_id_metadata_document_supported"] is True
    assert "registration_endpoint" not in asm


@pytest.mark.parametrize("authorization", [None, "Bearer nope", "Basic abc", "Bearer " + "A" * 43])
async def test_the_exact_401(sessionmaker, authorization):
    world = await _world(sessionmaker)
    headers = {"Authorization": authorization} if authorization else {}
    async with TestClient(TestServer(world.app)) as client:
        resp = await client.post("/mcp/claude", json={}, headers=headers)
    assert resp.status == 401
    assert resp.headers["WWW-Authenticate"] == (
        f'Bearer resource_metadata="{PUBLIC_URL}/.well-known/oauth-protected-resource'
        f'/mcp/claude", scope="anchor.read"'
    )


# --- authorize ---


@pytest.mark.parametrize(
    "overrides",
    [
        {"redirect_uri": oauth_store.REDIRECT_URI + "/"},
        {"redirect_uri": oauth_store.REDIRECT_URI.replace("auth_callback", "AUTH_CALLBACK")},
        {"redirect_uri": oauth_store.REDIRECT_URI.replace("https", "http")},
        {"redirect_uri": oauth_store.REDIRECT_URI + "?x=1"},
        {"redirect_uri": None},
        {"client_id": "https://evil.example/client"},
        {"client_id": oauth_store.CLIENT_ID + "/"},
        {"client_id": None},
        {"code_challenge_method": "plain"},
        {"code_challenge_method": None},
        {"code_challenge": None},
        {"code_challenge": "short"},
        {"resource": "https://evil.example/mcp/claude"},
        {"resource": PUBLIC_URL + "/mcp/grok"},
        {"resource": None},
        {"state": None},
        {"state": ""},
        {"state": "x" * 513},
        {"response_type": "token"},
        {"scope": "anchor.read anchor.write"},
    ],
)
async def test_a_bad_authorize_is_refused_without_redirect_or_write(sessionmaker, overrides):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        resp = await client.get(
            "/oauth/authorize", params=world.authorize_params(**overrides), allow_redirects=False
        )
        assert resp.status == 400
        assert "Location" not in resp.headers
        assert oauth.COOKIE not in resp.cookies
        page = await resp.text()
    assert "/claude connect" not in page
    assert len(world.pending) == 0
    assert await world.request_rows() == 0
    assert world.web_fake.sent == []


async def test_an_unknown_client_is_named_on_the_page(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        resp = await client.get(
            "/oauth/authorize", params=world.authorize_params(client_id="https://x.example/c")
        )
        assert oauth.UNKNOWN_CLIENT in await resp.text()


async def test_a_repeated_parameter_is_refused(sessionmaker):
    world = await _world(sessionmaker)
    query = urllib.parse.urlencode(world.authorize_params()) + "&state=second"
    async with TestClient(TestServer(world.app)) as client:
        resp = await client.get(f"/oauth/authorize?{query}", allow_redirects=False)
    assert resp.status == 400
    assert len(world.pending) == 0


@pytest.mark.parametrize(
    "value",
    ["HTTPS://ANCHOR.EXAMPLE/mcp/claude", R + "/", "https://Anchor.Example/mcp/claude/"],
)
async def test_resource_case_and_trailing_slash_are_accepted(sessionmaker, value):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.start(client, resource=value)
    assert len(world.pending) == 1


async def test_pending_caps_per_address_and_in_total(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        for _ in range(oauth_store.PENDING_PER_ADDRESS):
            await world.start(client, address="203.0.113.1")
        refused = await client.get(
            "/oauth/authorize",
            params=world.authorize_params(),
            headers={"X-Forwarded-For": "203.0.113.1"},
        )
        assert refused.status == 429
        # A spoofed leading entry does not change the address that counts.
        spoofed = await client.get(
            "/oauth/authorize",
            params=world.authorize_params(),
            headers={"X-Forwarded-For": "10.0.0.9, 203.0.113.1"},
        )
        assert spoofed.status == 429
        for i in range(oauth_store.PENDING_TOTAL - oauth_store.PENDING_PER_ADDRESS):
            await world.start(client, address=f"198.51.100.{i}")
        full = await client.get(
            "/oauth/authorize",
            params=world.authorize_params(),
            headers={"X-Forwarded-For": "192.0.2.99"},
        )
        assert full.status == 429
        # The refusal page says nothing about who is waiting.
        assert "/claude connect" not in await full.text()
    world.clock.advance(oauth_store.PENDING_TTL + datetime.timedelta(seconds=1))
    assert len(world.pending) == 0


# --- approval ---


async def test_a_wrong_code_is_refused_and_five_lock_connect_for_an_hour(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        _handle, code, _cookie = await world.start(client)
        wrong = "ZZZZZZ" if code != "ZZZZZZ" else "YYYYYY"
        for _ in range(oauth_store.CONNECT_FAIL_LIMIT - 1):
            assert await world.command(f"/claude connect {wrong}") == "Код не найден или устарел."
        from app.tg import claude as claude_ui

        assert await world.command(f"/claude connect {wrong}") == claude_ui.LOCKED
        # Locked: even the right code is refused.
        assert await world.command(f"/claude connect {code}") == claude_ui.LOCKED
        assert await world.request_rows() == 0
        world.clock.advance(oauth_store.CONNECT_LOCKOUT + datetime.timedelta(seconds=1))
        _handle, code, _cookie = await world.start(client)
        assert (await world.command(f"/claude connect {code}")).startswith("Подтверждено")
    assert await world.request_rows() == 1


@pytest.mark.parametrize("typed", ["", "abc", "ÄÄÄÄÄÄ", "12345678", "O0O0O0"])
async def test_malformed_codes_match_nothing(sessionmaker, typed):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.start(client)
        reply = await world.command(f"/claude connect {typed}".strip())
    assert not reply.startswith("Подтверждено")
    assert await world.request_rows() == 0


async def test_a_right_code_approves_only_its_own_request(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        handle_a, code_a, cookie_a = await world.start(client)
        handle_b, _code_b, cookie_b = await world.start(client, address="203.0.113.8")
        assert cookie_a != cookie_b
        assert (await world.command(f"/claude connect {code_a}")).startswith("Подтверждено")
        waiting = await world.poll(client, handle_b, cookie_b)
        assert waiting.status == 200 and "Location" not in waiting.headers
        done = await world.poll(client, handle_a, cookie_a)
        assert done.status == 302
        location = urllib.parse.urlsplit(done.headers["Location"])
        query = urllib.parse.parse_qs(location.query)
    assert f"{location.scheme}://{location.netloc}{location.path}" == oauth_store.REDIRECT_URI
    assert set(query) == {"code", "state", "iss"}
    assert query["state"] == [STATE]
    assert query["iss"] == [PUBLIC_URL]


async def test_status_answers_only_the_browser_that_asked(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        handle_a, code_a, cookie_a = await world.start(client)
        _handle_b, _code_b, cookie_b = await world.start(client, address="203.0.113.8")
        await world.command(f"/claude connect {code_a}")
        unknown = await (await world.poll(client, "A" * 22, cookie_a)).text()
        answers = [
            await world.poll(client, handle_a, None),  # no cookie
            await world.poll(client, handle_a, cookie_b),  # another request's cookie
            await world.poll(client, "A" * 22, cookie_a),  # a guessed handle
            await world.poll(client, "not-a-handle", cookie_a),
        ]
        for resp in answers:
            assert resp.status == 404
            assert "Location" not in resp.headers
            assert await resp.text() == unknown
        # The rightful browser still gets its redirect afterwards.
        assert (await world.poll(client, handle_a, cookie_a)).status == 302


async def test_repeated_authorizes_in_one_browser_share_its_cookie(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        handle_1, _code_1, cookie = await world.start(client)
        handle_2, code_2, same = await world.start(client, cookie=cookie)
        assert same == cookie
        await world.command(f"/claude connect {code_2}")
        assert (await world.poll(client, handle_1, cookie)).status == 200
        assert (await world.poll(client, handle_2, cookie)).status == 302


async def test_the_cookie_is_host_only_secure_and_httponly(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        resp = await client.get("/oauth/authorize", params=world.authorize_params())
        header = resp.headers["Set-Cookie"]
    assert header.startswith(f"{oauth.COOKIE}=")
    for part in ("Secure", "HttpOnly", "SameSite=Lax", "Path=/"):
        assert part in header
    assert "Domain" not in header


async def test_an_approved_code_the_browser_never_collects_expires(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        handle, code, cookie = await world.start(client)
        await world.command(f"/claude connect {code}")
        world.clock.advance(oauth_store.CODE_TTL + datetime.timedelta(seconds=1))
        assert (await world.poll(client, handle, cookie)).status == 404


async def test_no_web_request_ever_sends_a_telegram_message(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        await world.refresh(client, tokens["refresh_token"])
        await client.post("/oauth/revoke", data={"token": "x", "client_id": "y"})
        await world.mcp(client, tokens["access_token"])
        await world.mcp(client, None)
        for _ in range(3):
            await client.get("/oauth/authorize", params=world.authorize_params(state=None))
            await world.start(client)
    assert world.web_fake.sent == []


# --- token ---


async def test_a_code_is_single_use_and_a_replay_revokes_its_tokens(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        code = await world.auth_code(client)
        first = await world.exchange(client, code)
        assert first.status == 200
        tokens = await first.json()
        assert tokens["token_type"] == "Bearer"
        assert tokens["expires_in"] == 3600
        assert (await world.mcp(client, tokens["access_token"])).status == 200
        replay = await world.exchange(client, code)
        assert replay.status == 400 and (await replay.json())["error"] == "invalid_grant"
        assert (await world.mcp(client, tokens["access_token"])).status == 401
        assert (await world.refresh(client, tokens["refresh_token"])).status == 400


@pytest.mark.parametrize(
    "overrides",
    [
        {"code_verifier": "wrong-" + "v" * 50},
        {"code_verifier": None},
        {"client_id": "https://evil.example/client"},
        {"client_id": None},
        {"redirect_uri": oauth_store.REDIRECT_URI + "/"},
        {"resource": "https://evil.example/mcp/claude"},
        {"code": "not-the-code"},
    ],
)
async def test_a_bad_exchange_is_invalid_grant(sessionmaker, overrides):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        code = await world.auth_code(client)
        if "code" in overrides:
            code = overrides.pop("code")
        resp = await world.exchange(client, code, **overrides)
        assert resp.status == 400
        assert (await resp.json())["error"] == "invalid_grant"
    async with sessionmaker() as session:
        assert (await session.execute(select(OauthToken))).first() is None


async def test_an_expired_code_is_invalid_grant(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        code = await world.auth_code(client)
        world.clock.advance(oauth_store.CODE_TTL + datetime.timedelta(seconds=1))
        resp = await world.exchange(client, code)
    assert resp.status == 400


async def test_resource_may_be_absent_at_the_token_endpoint(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        resp = await world.exchange(client, await world.auth_code(client), resource=None)
    assert resp.status == 200


async def test_two_concurrent_redemptions_one_success(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        code = await world.auth_code(client)
        responses = await asyncio.gather(world.exchange(client, code), world.exchange(client, code))
        statuses = sorted(r.status for r in responses)
    assert statuses == [200, 400]


async def test_unsupported_grant_type(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        resp = await client.post("/oauth/token", data={"grant_type": "client_credentials"})
        assert (await resp.json())["error"] == "unsupported_grant_type"


# --- refresh ---


async def test_refresh_rotates(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        resp = await world.refresh(client, tokens["refresh_token"])
        assert resp.status == 200
        rotated = await resp.json()
        assert rotated["refresh_token"] != tokens["refresh_token"]
        assert (await world.mcp(client, rotated["access_token"])).status == 200
        again = await world.refresh(client, rotated["refresh_token"])
        assert again.status == 200


async def test_refresh_without_client_id_or_resource_is_accepted(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        resp = await world.refresh(client, tokens["refresh_token"], client_id=None)
    assert resp.status == 200


@pytest.mark.parametrize(
    "overrides",
    [{"client_id": "https://evil.example/client"}, {"resource": "https://evil.example/mcp"}],
)
async def test_refresh_with_a_mismatch_is_refused(sessionmaker, overrides):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        resp = await world.refresh(client, tokens["refresh_token"], **overrides)
        assert resp.status == 400
        # Nothing was rotated: the original still works.
        assert (await world.refresh(client, tokens["refresh_token"])).status == 200


async def test_reuse_within_the_grace_is_refused_without_revoking(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        rotated = await (await world.refresh(client, tokens["refresh_token"])).json()
        world.clock.advance(datetime.timedelta(seconds=29))
        retry = await world.refresh(client, tokens["refresh_token"])
        assert retry.status == 400
        assert (await world.mcp(client, rotated["access_token"])).status == 200
        assert (await world.refresh(client, rotated["refresh_token"])).status == 200


async def test_reuse_after_the_grace_revokes_the_connection(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        rotated = await (await world.refresh(client, tokens["refresh_token"])).json()
        world.clock.advance(oauth_store.REFRESH_GRACE)
        reuse = await world.refresh(client, tokens["refresh_token"])
        assert reuse.status == 400
        assert (await world.mcp(client, rotated["access_token"])).status == 401
        assert (await world.refresh(client, rotated["refresh_token"])).status == 400
    async with sessionmaker() as session:
        assert await oauth_store.current_connection(session, world.clock) is None


async def test_the_connection_s_absolute_expiry_wins(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        # Keep refreshing well inside the window, right up to the end.
        current = tokens
        for _ in range(29):
            world.clock.advance(datetime.timedelta(days=1))
            resp = await world.refresh(client, current["refresh_token"])
            assert resp.status == 200
            current = await resp.json()
        world.clock.advance(datetime.timedelta(days=1))
        assert (await world.refresh(client, current["refresh_token"])).status == 400
        assert (await world.mcp(client, current["access_token"])).status == 401
    async with sessionmaker() as session:
        tokens_left = (await session.execute(select(OauthToken))).scalars().all()
        connection = (await session.execute(select(OauthConnection))).scalar_one()
    assert all(t.expires_at <= connection.expires_at for t in tokens_left)


async def test_an_access_token_lasts_an_hour(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        world.clock.advance(oauth_store.ACCESS_TTL)
        assert (await world.mcp(client, tokens["access_token"])).status == 401


# --- revoke ---


async def test_revoke_an_unknown_token_is_200(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        resp = await client.post(
            "/oauth/revoke", data={"token": "nope", "client_id": oauth_store.CLIENT_ID}
        )
    assert resp.status == 200


async def test_revoke_another_client_s_token_is_200_with_no_effect(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        resp = await client.post(
            "/oauth/revoke",
            data={"token": tokens["access_token"], "client_id": "https://evil.example/client"},
        )
        assert resp.status == 200
        assert (await world.mcp(client, tokens["access_token"])).status == 200


async def test_revoke_requires_a_client_id(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        resp = await client.post("/oauth/revoke", data={"token": tokens["access_token"]})
        assert resp.status == 400
        assert (await world.mcp(client, tokens["access_token"])).status == 200


async def test_revoking_a_refresh_token_revokes_its_access_tokens(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        resp = await client.post(
            "/oauth/revoke",
            data={"token": tokens["refresh_token"], "client_id": oauth_store.CLIENT_ID},
        )
        assert resp.status == 200
        assert (await world.mcp(client, tokens["access_token"])).status == 401
        assert (await world.refresh(client, tokens["refresh_token"])).status == 400


# --- the connection ---


async def test_a_second_connection_replaces_the_first_and_closes_its_windows(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        first = await world.connect(client)
        await world.open_window(["journal"])
        async with sessionmaker() as session:
            old = await oauth_store.current_connection(session, world.clock)
            assert await grants.find_open_window(session, world.clock, old.id) is not None
        second = await world.connect(client)
        assert (await world.mcp(client, first["access_token"])).status == 401
        assert (await world.refresh(client, first["refresh_token"])).status == 400
        assert (await world.mcp(client, second["access_token"])).status == 200
    async with sessionmaker() as session:
        assert await grants.find_open_window(session, world.clock, old.id) is None
        active = await session.execute(
            select(OauthConnection).where(OauthConnection.revoked_at.is_(None))
        )
        assert len(active.scalars().all()) == 1


async def test_disconnect_revokes_the_connection(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        from app.tg import claude as claude_ui

        assert await world.command("/claude disconnect") == claude_ui.DISCONNECTED
        assert (await world.mcp(client, tokens["access_token"])).status == 401
        assert await world.command("/claude disconnect") == claude_ui.NOTHING_CONNECTED


async def test_the_flag_off_revokes_everything_and_on_again_revives_nothing(sessionmaker, tmp_path):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        await world.open_window(["journal"])

    from app.startup import run_startup_tasks

    persona = tmp_path / "persona.md"
    persona.write_text("persona")
    async with sessionmaker() as session:
        await run_startup_tasks(session, settings(CLAUDE_ACCESS_ENABLED=False), persona)

    again = World(sessionmaker)
    async with TestClient(TestServer(again.app)) as client:
        assert (await again.mcp(client, tokens["access_token"])).status == 401
        assert (await again.refresh(client, tokens["refresh_token"])).status == 400
    async with sessionmaker() as session:
        open_windows = await session.execute(
            select(AccessGrant).where(AccessGrant.client == "claude", AccessGrant.revoked_at.is_(None))
        )
        assert open_windows.first() is None
        unrevoked = await session.execute(select(OauthToken).where(OauthToken.revoked_at.is_(None)))
        assert unrevoked.first() is None


def test_challenge_fixture_is_well_formed():
    assert len(CHALLENGE) == 43


# --- retention ---


async def test_the_sweep_keeps_a_replaced_refresh_token_until_its_own_expiry(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        tokens = await world.connect(client)
        rotated = await (await world.refresh(client, tokens["refresh_token"])).json()
        world.clock.advance(datetime.timedelta(hours=2))
        async with sessionmaker() as session:
            removed = await oauth_store.sweep(session, world.clock)
        assert removed == 2  # the two expired access tokens
        # The replaced refresh token is still there, so its reuse is
        # still recognised -- and still revokes the connection.
        assert (await world.refresh(client, tokens["refresh_token"])).status == 400
        assert (await world.refresh(client, rotated["refresh_token"])).status == 400


async def test_the_sweep_forgets_requests_after_a_day_and_revoked_connections_after_30(
    sessionmaker,
):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.connect(client)
        await world.start(client)  # an unapproved request writes nothing to sweep
    await world.command("/claude disconnect")
    world.clock.advance(datetime.timedelta(days=2))
    async with sessionmaker() as session:
        await oauth_store.sweep(session, world.clock)
    assert await world.request_rows() == 0
    async with sessionmaker() as session:
        assert (await session.execute(select(OauthConnection))).first() is not None
    world.clock.advance(datetime.timedelta(days=29))
    async with sessionmaker() as session:
        await oauth_store.sweep(session, world.clock)
        assert (await session.execute(select(OauthConnection))).first() is None
        assert (await session.execute(select(OauthToken))).first() is None


async def test_the_retention_job_runs_the_oauth_sweep(sessionmaker):
    from app.core import retention

    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.connect(client)
    world.clock.advance(datetime.timedelta(days=2))
    async with sessionmaker() as session:
        await retention.run_retention_sweep(session, world.settings, world.clock)
    assert await world.request_rows() == 0


# --- the code, as a phone types it ---

_TO_CYRILLIC = str.maketrans("ABEKMHPCTYX", "АВЕКМНРСТУХ")


@pytest.mark.parametrize("shape", ["cyrillic", "lower", "cyrillic_lower", "spaced"])
async def test_a_code_typed_on_another_layout_still_matches(sessionmaker, shape):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.start(client)
        # A code made only of look-alike letters, so no draw of the
        # random code can make this test vacuous.
        entry = next(iter(world.pending._pending.values()))
        entry.code = code = "KEMA3X"
        typed = {
            "cyrillic": code.translate(_TO_CYRILLIC),
            "lower": code.lower(),
            "cyrillic_lower": code.translate(_TO_CYRILLIC).lower(),
            "spaced": f"  {code} ",
        }[shape]
        # /claude connect takes one argument; `spaced` exercises strip() directly.
        if shape == "spaced":
            assert world.pending.match(typed) is not None
            return
        assert (await world.command(f"/claude connect {typed}")).startswith("Подтверждено")


async def test_a_cyrillic_letter_that_is_not_a_look_alike_matches_nothing(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        _handle, code, _cookie = await world.start(client)
        typed = "Ж" + code[1:]
        assert await world.command(f"/claude connect {typed}") == "Код не найден или устарел."

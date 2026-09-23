"""OAuth 2.1 + PKCE against the planner's Supabase authorization server.

The planner delegates OAuth to Supabase's own GoTrue server (see
`lib/mcp/auth.ts` and `lib/mcp/env.ts` in the planner repo:
`getSupabaseAuthIssuer()` is `${NEXT_PUBLIC_SUPABASE_URL}/auth/v1`). We
therefore never guess a fixed set of paths: `_discover()` fetches that
issuer's `/.well-known/oauth-authorization-server` (RFC 8414) and reads
`authorization_endpoint` / `token_endpoint` from it, falling back to the
conventional `<issuer>/authorize` and `<issuer>/token` only if discovery
itself fails (a transient fetch error, not "the endpoint disagrees with
us" -- if it answers, its answer wins).

The consent step (`/api/oauth/decision` in the planner) is a browser
page: this module cannot complete a link on its own. `/planner_link`
(app/tg/planner.py) hands the user a URL to open in a logged-in browser
tab; the planner then redirects to our own `GET /planner/oauth/callback`
(app/main.py), which calls `complete_link()` below with the `code` and
`state` it received.

The refresh token is the one thing this module treats as a secret that
must survive a rotation without ever being used twice: `get_access_token`
takes `SELECT ... FOR UPDATE` on the singleton `planner_credential` row
and, within the process, an `asyncio.Lock` -- a rotated token is
committed to the database *before* the caller gets to use the new
access token, so a crash between the two never leaves Anchor holding an
already-invalidated refresh token.

`invalid_grant` on a refresh means the grant is gone (revoked from the
planner side, or the refresh token was already rotated by a concurrent
process). The credential is marked `revoked`, and `mark_notice_sent()`
is the once-only gate the job layer (app/planner/jobs.py) uses to send
exactly one "reconnect the planner" message rather than one per failed
sync.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import logging
import secrets
from urllib.parse import urlencode

import aiohttp
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.clock import Clock
from app.db.models import PlannerCredential

logger = logging.getLogger(__name__)

CREDENTIAL_ID = 1
ACTIVE = "active"
REVOKED = "revoked"

# How long a PKCE `state` value stays redeemable. Plan: 10 minutes.
STATE_TTL = datetime.timedelta(minutes=10)

# Refresh this far ahead of actual expiry, so a token handed to
# client.py's caller is never on the edge of expiring mid-request.
REFRESH_SKEW = datetime.timedelta(seconds=60)

# HTTP timeout for the discovery/authorize/token round trips. These are
# rare, interactive-adjacent calls, not the per-turn hot path (that is
# client.py's 5s), so a slightly longer budget is fine.
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)


class PlannerAuthError(Exception):
    """The planner is not linked, or the grant was revoked."""


class _InvalidGrant(Exception):
    """Internal: the token endpoint answered `error=invalid_grant`."""


# In-memory PKCE state, keyed by the `state` value. A single-user bot
# behind ALLOWED_CHAT_ID has exactly one link flow in flight at a time,
# and the window is ten minutes -- there is nothing here worth persisting
# across a restart; a lost verifier just means running /planner_link
# again. Unlike the credential itself (which rotates and must survive a
# deploy), this is disposable by design.
_pending_states: dict[str, tuple[str, datetime.datetime]] = {}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_pkce() -> tuple[str, str]:
    """(code_verifier, code_challenge), S256 per RFC 7636."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _issuer(settings: Settings) -> str:
    return settings.PLANNER_SUPABASE_URL.rstrip("/") + "/auth/v1"


async def _discover(settings: Settings) -> dict[str, str]:
    """`{authorization_endpoint, token_endpoint}`, from RFC 8414 metadata.

    Falls back to the conventional GoTrue paths on any fetch or parse
    failure -- a network hiccup during discovery must not make
    /planner_link itself the thing that failed.
    """
    issuer = _issuer(settings)
    fallback = {
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
    }
    url = f"{issuer}/.well-known/oauth-authorization-server"
    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as http:
            async with http.get(url) as resp:
                if resp.status != 200:
                    return fallback
                data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return fallback
    return {
        "authorization_endpoint": data.get("authorization_endpoint")
        or fallback["authorization_endpoint"],
        "token_endpoint": data.get("token_endpoint") or fallback["token_endpoint"],
    }


def start_link(clock: Clock) -> tuple[str, str, str]:
    """Mint (state, code_verifier, code_challenge) and remember the state.

    Split from `build_authorize_url` so a test can drive PKCE generation
    without an HTTP round trip for discovery.
    """
    verifier, challenge = generate_pkce()
    state = _b64url(secrets.token_bytes(16))
    _pending_states[state] = (verifier, clock.now_utc() + STATE_TTL)
    return state, verifier, challenge


def build_authorize_url(
    endpoints: dict[str, str], settings: Settings, *, state: str, code_challenge: str
) -> str:
    params = {
        "response_type": "code",
        "client_id": settings.PLANNER_OAUTH_CLIENT_ID,
        "redirect_uri": settings.PLANNER_OAUTH_REDIRECT_URI,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{endpoints['authorization_endpoint']}?{urlencode(params)}"


async def link_url(settings: Settings, clock: Clock) -> str:
    """The full URL for /planner_link to hand the user."""
    state, _verifier, challenge = start_link(clock)
    endpoints = await _discover(settings)
    return build_authorize_url(endpoints, settings, state=state, code_challenge=challenge)


async def _store_tokens(session: AsyncSession, clock: Clock, settings: Settings, body: dict) -> None:
    expires_in = int(body.get("expires_in") or 3600)
    values = {
        "id": CREDENTIAL_ID,
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "expires_at": clock.now_utc() + datetime.timedelta(seconds=expires_in),
        "client_id": settings.PLANNER_OAUTH_CLIENT_ID,
        "status": ACTIVE,
        "notified": False,
        "updated_at": clock.now_utc(),
    }
    await session.execute(
        pg_insert(PlannerCredential)
        .values(**values)
        .on_conflict_do_update(index_elements=["id"], set_=values)
    )
    await session.commit()


async def complete_link(
    session: AsyncSession, settings: Settings, clock: Clock, *, code: str, state: str
) -> None:
    """Exchange an authorization code for tokens; store the credential.

    Raises PlannerAuthError on an unknown/expired state or a failed
    exchange -- the callback handler (app/main.py) turns that into a
    plain-text page; it never has payload text to leak.
    """
    pending = _pending_states.pop(state, None)
    if pending is None:
        raise PlannerAuthError("unknown or already-used state")
    verifier, expires_at = pending
    if clock.now_utc() > expires_at:
        raise PlannerAuthError("state expired; run /planner_link again")

    endpoints = await _discover(settings)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.PLANNER_OAUTH_REDIRECT_URI,
        "client_id": settings.PLANNER_OAUTH_CLIENT_ID,
        "code_verifier": verifier,
    }
    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as http:
            async with http.post(endpoints["token_endpoint"], data=data) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200 or "access_token" not in body:
                    raise PlannerAuthError(f"token exchange failed: status {resp.status}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise PlannerAuthError(f"token exchange failed: {type(exc).__name__}") from exc

    await _store_tokens(session, clock, settings, body)
    logger.info("planner linked", extra={"event": "planner_link"})


async def _refresh_request(endpoints: dict[str, str], settings: Settings, refresh_token: str) -> dict:
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": settings.PLANNER_OAUTH_CLIENT_ID,
    }
    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as http:
            async with http.post(endpoints["token_endpoint"], data=data) as resp:
                body = await resp.json(content_type=None)
                if resp.status == 400 and body.get("error") == "invalid_grant":
                    raise _InvalidGrant()
                if resp.status != 200 or "access_token" not in body:
                    raise PlannerAuthError(f"refresh failed: status {resp.status}")
                return body
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise PlannerAuthError(f"refresh failed: {type(exc).__name__}") from exc


# One process-wide lock: worker concurrency is 1 for job/update handling,
# but the heartbeat loop and the claim loop are separate asyncio tasks,
# so a sync job and (later, P3) a write job could both want a fresh
# token at the same instant. The DB row lock (SELECT ... FOR UPDATE)
# is the cross-process guarantee; this lock only avoids two in-process
# refreshes racing each other on the same row before either commits.
_refresh_lock = asyncio.Lock()


async def get_access_token(session: AsyncSession, settings: Settings, clock: Clock) -> str:
    """The current access token, refreshing first if it is near expiry.

    Raises PlannerAuthError if there is no active credential -- not
    linked yet, or the grant was revoked.
    """
    async with _refresh_lock:
        result = await session.execute(
            select(PlannerCredential)
            .where(PlannerCredential.id == CREDENTIAL_ID)
            .with_for_update()
        )
        row = result.scalar_one_or_none()
        if row is None or row.status != ACTIVE:
            await session.commit()
            raise PlannerAuthError("the planner is not linked")

        if row.expires_at - REFRESH_SKEW > clock.now_utc():
            token = row.access_token
            await session.commit()
            return token

        endpoints = await _discover(settings)
        try:
            body = await _refresh_request(endpoints, settings, row.refresh_token)
        except _InvalidGrant:
            row.status = REVOKED
            await session.commit()
            logger.warning("planner grant revoked", extra={"event": "planner_revoked"})
            raise PlannerAuthError("the planner grant was revoked; run /planner_link again")

        # Persisted before being handed to the caller: a crash between
        # this commit and the caller's request leaves the *new* refresh
        # token in the database, never the already-rotated old one.
        row.access_token = body["access_token"]
        row.refresh_token = body.get("refresh_token", row.refresh_token)
        row.expires_at = clock.now_utc() + datetime.timedelta(
            seconds=int(body.get("expires_in") or 3600)
        )
        row.updated_at = clock.now_utc()
        await session.commit()
        return row.access_token


async def get_status(session: AsyncSession) -> PlannerCredential | None:
    """The credential row, or None if never linked. For /planner status."""
    return await session.get(PlannerCredential, CREDENTIAL_ID)


async def is_enabled(session: AsyncSession) -> bool:
    """True iff linked, active, and not user-paused via /planner off.

    Consulted by app/core/scheduler.py's maybe_enqueue_planner_sync and
    app/core/turn.py's stale-snapshot trigger, both of which must stop
    asking for fresh data once the user has explicitly turned it off --
    PLANNER_ENABLED alone (the deploy-level switch) is not enough.
    """
    row = await get_status(session)
    return row is not None and row.status == ACTIVE and row.enabled


async def set_enabled(session: AsyncSession, enabled: bool) -> PlannerCredential | None:
    """/planner on|off. None (a no-op) if the planner was never linked."""
    row = await get_status(session)
    if row is None:
        return None
    row.enabled = enabled
    await session.commit()
    return row


async def mark_notice_sent(session: AsyncSession) -> bool:
    """Flip `notified` False->True atomically. True iff this call flipped it.

    The one-shot gate for "send exactly one reconnect notice" (plan
    section 3.1): the first caller to see a revoked, not-yet-notified
    credential gets True and sends the message; every later PLANNER_SYNC
    attempt against the same revoked credential gets False and stays
    quiet.
    """
    result = await session.execute(
        select(PlannerCredential)
        .where(PlannerCredential.id == CREDENTIAL_ID)
        .where(PlannerCredential.status == REVOKED)
        .where(PlannerCredential.notified.is_(False))
        .with_for_update()
    )
    row = result.scalar_one_or_none()
    if row is None:
        await session.commit()
        return False
    row.notified = True
    await session.commit()
    return True


__all__ = [
    "CREDENTIAL_ID",
    "ACTIVE",
    "REVOKED",
    "STATE_TTL",
    "PlannerAuthError",
    "generate_pkce",
    "start_link",
    "build_authorize_url",
    "link_url",
    "complete_link",
    "get_access_token",
    "get_status",
    "is_enabled",
    "set_enabled",
    "mark_notice_sent",
]

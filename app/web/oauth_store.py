"""The Claude connector's OAuth state: the only writer of `oauth_*`.

anchor-claude-connector-plan.md sections 5 and 7, with the shapes the
dry run pinned (docs/decisions.md, "C2 -- the dry run's answers").
Everything that creates, redeems, rotates or revokes a credential is
here, so the rules live in one place:

- **Pending requests are memory, not rows.** `/oauth/authorize` from a
  stranger costs a dict entry that expires in 10 minutes, under caps of
  5 per client address and 20 in total. Only the user typing its
  confirmation code into Telegram (`approve`) writes anything.
- **Hashes only at rest.** Authorization codes, access and refresh
  tokens and the browser-binding secret are 256-bit random values; the
  database and the pending store keep their sha256. The one plaintext
  that lingers is an approved authorization code, in memory, until the
  browser that owns it collects it (or 60 seconds pass).
- **One connection at a time, 30 days absolute.** Redeeming a code
  revokes every other connection, its tokens and its windows in the
  same transaction; the partial unique index backs that up.
- **Codes are single use, atomically.** A replay revokes whatever the
  first redemption issued (OAuth 2.1 section 4.1.3).
- **Refresh tokens rotate.** Presenting a replaced one within 30
  seconds is treated as a lost-response retry (`invalid_grant`, nothing
  revoked); later, as theft (the connection is revoked).

Every function takes the injected Clock and never the database's
`now()`, so the tests can walk time forward. Nothing here logs a
token, code, state, handle, cookie or client id: only ids and outcomes.
"""

from __future__ import annotations

import base64
import collections
import dataclasses
import datetime
import hashlib
import hmac
import logging
import secrets

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import grants
from app.core.clock import Clock
from app.db.models import OauthConnection, OauthRequest, OauthToken

logger = logging.getLogger(__name__)

# Pinned by the dry run. A deploy must not be able to loosen any of
# these, so none is a setting.
CLIENT_ID = "https://claude.ai/oauth/mcp-oauth-client-metadata"
REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
SCOPE = "anchor.read"

ACCESS_TTL = datetime.timedelta(minutes=60)
CONNECTION_TTL = datetime.timedelta(days=30)
CODE_TTL = datetime.timedelta(seconds=60)
PENDING_TTL = datetime.timedelta(minutes=10)
PENDING_PER_ADDRESS = 5
PENDING_TOTAL = 20
REFRESH_GRACE = datetime.timedelta(seconds=30)
CONNECT_FAIL_LIMIT = 5
CONNECT_FAIL_WINDOW = datetime.timedelta(hours=1)
CONNECT_LOCKOUT = datetime.timedelta(hours=1)
REQUEST_RETENTION = datetime.timedelta(days=1)
REVOKED_CONNECTION_RETENTION = datetime.timedelta(days=30)

# Uppercase without 0/O/1/I: read off a screen, typed on a phone.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6
TOKEN_BYTES = 32


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_secret() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def pkce_matches(verifier: str, challenge: str) -> bool:
    """RFC 7636 S256, compared in constant time."""
    digest = hashlib.sha256(verifier.encode("ascii", "replace")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return hmac.compare_digest(computed, challenge)


# --- pending requests (memory) ---


@dataclasses.dataclass
class Pending:
    handle_sha256: str
    code: str
    browser_sha256: str
    address: str
    created_at: datetime.datetime
    client_id: str
    redirect_uri: str
    code_challenge: str
    resource: str
    scope: str
    state: str
    auth_code: str | None = None
    code_expires_at: datetime.datetime | None = None


class PendingStore:
    """Authorize requests waiting for the user's typed code, in memory.

    Keyed by the sha256 of the handle, so the store never holds a handle
    a guess could be compared against directly. One process, one event
    loop: no locking. A restart drops every pending request and resets
    the /claude connect lockout; both only cost the user a retry.
    """

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self._pending: dict[str, Pending] = {}
        self._failures: collections.deque[datetime.datetime] = collections.deque()
        self._locked_until: datetime.datetime | None = None

    def __len__(self) -> int:
        self._prune()
        return len(self._pending)

    def _prune(self) -> None:
        now = self.clock.now_utc()
        for key, entry in list(self._pending.items()):
            if now - entry.created_at > PENDING_TTL or (
                entry.code_expires_at is not None and now > entry.code_expires_at
            ):
                del self._pending[key]

    def _new_code(self) -> str:
        taken = {entry.code for entry in self._pending.values()}
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            if code not in taken:
                return code

    def create(self, *, address: str, browser_secret: str, **fields) -> tuple[str, Pending] | None:
        """A new pending request, or None when a cap is reached."""
        self._prune()
        if len(self._pending) >= PENDING_TOTAL:
            return None
        if sum(1 for e in self._pending.values() if e.address == address) >= PENDING_PER_ADDRESS:
            return None
        handle = secrets.token_urlsafe(16)
        entry = Pending(
            handle_sha256=sha256(handle),
            code=self._new_code(),
            browser_sha256=sha256(browser_secret),
            address=address,
            created_at=self.clock.now_utc(),
            **fields,
        )
        self._pending[entry.handle_sha256] = entry
        return handle, entry

    def for_browser(self, handle: str, browser_secret: str | None) -> Pending | None:
        """The entry behind `handle`, only for the browser that opened it.

        No cookie, the wrong cookie, an unknown or an expired handle all
        return None, and the caller answers all four the same way.
        """
        self._prune()
        entry = self._pending.get(sha256(handle))
        if entry is None or browser_secret is None:
            return None
        if not hmac.compare_digest(entry.browser_sha256, sha256(browser_secret)):
            return None
        return entry

    def drop(self, entry: Pending) -> None:
        self._pending.pop(entry.handle_sha256, None)

    def clear(self) -> None:
        """Forget everything (/delete)."""
        self._pending.clear()
        self._failures.clear()
        self._locked_until = None

    # --- /claude connect ---

    def locked(self) -> bool:
        return self._locked_until is not None and self.clock.now_utc() < self._locked_until

    def match(self, code: str) -> Pending | None:
        """The unapproved entry whose code this is. Every live code is
        compared in constant time, so timing says nothing about which."""
        self._prune()
        typed = code.strip().upper()
        if len(typed) != CODE_LENGTH or any(ch not in CODE_ALPHABET for ch in typed):
            return None
        found = None
        for entry in self._pending.values():
            if hmac.compare_digest(entry.code, typed) and entry.auth_code is None:
                found = entry
        return found

    def record_failure(self) -> bool:
        """Count a wrong code. True when this failure starts a lockout."""
        now = self.clock.now_utc()
        self._failures.append(now)
        while self._failures and now - self._failures[0] > CONNECT_FAIL_WINDOW:
            self._failures.popleft()
        if len(self._failures) >= CONNECT_FAIL_LIMIT:
            self._failures.clear()
            self._locked_until = now + CONNECT_LOCKOUT
            return True
        return False


# --- approval (Telegram) ---


async def approve(session: AsyncSession, clock: Clock, entry: Pending) -> int:
    """Write the approved request and hand its entry an authorization code.

    Only its sha256 is stored; the plaintext waits in the entry for the
    owning browser's next status poll.
    """
    now = clock.now_utc()
    code = new_secret()
    row = OauthRequest(
        client_id=entry.client_id,
        redirect_uri=entry.redirect_uri,
        resource=entry.resource,
        scope=entry.scope,
        code_challenge=entry.code_challenge,
        code_sha256=sha256(code),
        code_expires_at=now + CODE_TTL,
        status="approved",
        created_at=now,
    )
    session.add(row)
    await session.commit()
    entry.auth_code = code
    entry.code_expires_at = row.code_expires_at
    logger.info("oauth request approved", extra={"event": "oauth_approved", "request_id": row.id})
    return row.id


# --- tokens ---


@dataclasses.dataclass(frozen=True)
class Issued:
    access_token: str
    refresh_token: str
    expires_in: int
    connection_id: int


async def _issue(
    session: AsyncSession, connection: OauthConnection, request_id: int | None, audience: str,
    now: datetime.datetime,
) -> tuple[Issued, OauthToken]:
    access, refresh = new_secret(), new_secret()
    access_expires = min(now + ACCESS_TTL, connection.expires_at)
    refresh_row = OauthToken(
        connection_id=connection.id, request_id=request_id, kind="refresh",
        token_sha256=sha256(refresh), audience=audience, created_at=now,
        expires_at=connection.expires_at,
    )
    session.add_all(
        [
            OauthToken(
                connection_id=connection.id, request_id=request_id, kind="access",
                token_sha256=sha256(access), audience=audience, created_at=now,
                expires_at=access_expires,
            ),
            refresh_row,
        ]
    )
    await session.flush()
    issued = Issued(
        access_token=access,
        refresh_token=refresh,
        expires_in=max(1, int((access_expires - now).total_seconds())),
        connection_id=connection.id,
    )
    return issued, refresh_row


async def _revoke_connections(
    session: AsyncSession, now: datetime.datetime, connection_ids: list[int]
) -> None:
    """Revoke connections, their unrevoked tokens and their open windows. No commit."""
    if not connection_ids:
        return
    await session.execute(
        update(OauthConnection)
        .where(OauthConnection.id.in_(connection_ids), OauthConnection.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await session.execute(
        update(OauthToken)
        .where(OauthToken.connection_id.in_(connection_ids), OauthToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await grants.close_windows(session, now, connection_ids)


async def _active_connection_ids(session: AsyncSession) -> list[int]:
    rows = await session.execute(
        select(OauthConnection.id).where(OauthConnection.revoked_at.is_(None))
    )
    return list(rows.scalars().all())


async def redeem_code(
    session: AsyncSession,
    clock: Clock,
    *,
    code: str,
    client_id: str,
    redirect_uri: str,
    verifier: str,
    resource_ok,
) -> Issued | None:
    """Exchange an authorization code. None is `invalid_grant`.

    `resource_ok(bound_resource)` says whether the token request's own
    `resource` (if any) matches what the code was bound to.
    """
    now = clock.now_utc()
    code_hash = sha256(code)
    result = await session.execute(
        update(OauthRequest)
        .where(
            OauthRequest.code_sha256 == code_hash,
            OauthRequest.status == "approved",
            OauthRequest.code_expires_at > now,
            OauthRequest.client_id == client_id,
        )
        .values(status="redeemed")
        .returning(OauthRequest)
    )
    row = result.scalar_one_or_none()
    if row is None:
        replayed = await session.scalar(
            select(OauthRequest).where(
                OauthRequest.code_sha256 == code_hash, OauthRequest.status == "redeemed"
            )
        )
        if replayed is not None:
            await _revoke_replay(session, now, replayed)
            await session.commit()
            logger.warning(
                "oauth code replayed", extra={"event": "oauth_replay", "request_id": replayed.id}
            )
        else:
            await session.rollback()
        return None

    if (
        redirect_uri != row.redirect_uri
        or not pkce_matches(verifier, row.code_challenge)
        or not resource_ok(row.resource)
    ):
        # The code is spent either way: single use means single attempt.
        await session.commit()
        logger.info("oauth code refused", extra={"event": "oauth_refused", "request_id": row.id})
        return None

    await _revoke_connections(session, now, await _active_connection_ids(session))
    connection = OauthConnection(
        client_id=row.client_id, created_at=now, expires_at=now + CONNECTION_TTL
    )
    session.add(connection)
    await session.flush()
    row.connection_id = connection.id
    issued, _refresh = await _issue(session, connection, row.id, row.resource, now)
    await session.commit()
    logger.info(
        "oauth connection created",
        extra={"event": "oauth_connected", "connection_id": connection.id},
    )
    return issued


async def _revoke_replay(session: AsyncSession, now: datetime.datetime, request: OauthRequest) -> None:
    await session.execute(
        update(OauthToken)
        .where(OauthToken.request_id == request.id, OauthToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    if request.connection_id is not None:
        await _revoke_connections(session, now, [request.connection_id])


async def rotate_refresh(
    session: AsyncSession,
    clock: Clock,
    *,
    refresh_token: str,
    client_id: str | None,
    resource_ok,
) -> Issued | None:
    """Rotate a refresh token. None is `invalid_grant`."""
    now = clock.now_utc()
    row = (
        await session.execute(
            select(OauthToken)
            .where(OauthToken.token_sha256 == sha256(refresh_token), OauthToken.kind == "refresh")
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        await session.rollback()
        return None
    connection = await session.get(OauthConnection, row.connection_id)
    if (
        connection is None
        or connection.revoked_at is not None
        or connection.expires_at <= now
        or (client_id is not None and client_id != connection.client_id)
        or not resource_ok(row.audience)
    ):
        await session.rollback()
        return None
    if row.replaced_at is not None:
        if now - row.replaced_at < REFRESH_GRACE:
            connection_id = connection.id  # a rollback expires the object
            await session.rollback()
            logger.info(
                "oauth refresh retried",
                extra={"event": "oauth_refresh_retry", "connection_id": connection_id},
            )
            return None
        await _revoke_connections(session, now, [connection.id])
        await session.commit()
        logger.warning(
            "oauth refresh reused; connection revoked",
            extra={"event": "oauth_refresh_reuse", "connection_id": connection.id},
        )
        return None
    if row.revoked_at is not None or row.expires_at <= now:
        await session.rollback()
        return None
    issued, new_refresh = await _issue(session, connection, row.request_id, row.audience, now)
    row.replaced_at = now
    row.replaced_by = new_refresh.id
    await session.commit()
    logger.info(
        "oauth refresh rotated",
        extra={"event": "oauth_refresh", "connection_id": connection.id},
    )
    return issued


async def revoke_token(session: AsyncSession, clock: Clock, *, token: str, client_id: str) -> None:
    """RFC 7009. Silent for an unknown token or another client's."""
    now = clock.now_utc()
    row = await session.scalar(select(OauthToken).where(OauthToken.token_sha256 == sha256(token)))
    if row is None:
        return
    connection = await session.get(OauthConnection, row.connection_id)
    if connection is None or connection.client_id != client_id:
        return
    if row.revoked_at is None:
        row.revoked_at = now
    if row.kind == "refresh":
        await session.execute(
            update(OauthToken)
            .where(
                OauthToken.connection_id == connection.id,
                OauthToken.kind == "access",
                OauthToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
    await session.commit()
    logger.info("oauth token revoked", extra={"event": "oauth_revoke", "connection_id": connection.id})


async def authenticate_bearer(
    session: AsyncSession, clock: Clock, token: str, audience: str
) -> OauthConnection | None:
    """The connection an access token speaks for, if every check passes:
    an unrevoked, unexpired access token for this audience, on the one
    active connection, within that connection's absolute lifetime."""
    now = clock.now_utc()
    connection = await session.scalar(
        select(OauthConnection)
        .join(OauthToken, OauthToken.connection_id == OauthConnection.id)
        .where(
            OauthToken.token_sha256 == sha256(token),
            OauthToken.kind == "access",
            OauthToken.revoked_at.is_(None),
            OauthToken.expires_at > now,
            OauthToken.audience == audience,
            OauthConnection.revoked_at.is_(None),
            OauthConnection.expires_at > now,
        )
    )
    if connection is not None:
        connection.last_used_at = now
        await session.commit()
    return connection


async def current_connection(session: AsyncSession, clock: Clock) -> OauthConnection | None:
    return await session.scalar(
        select(OauthConnection).where(
            OauthConnection.revoked_at.is_(None), OauthConnection.expires_at > clock.now_utc()
        )
    )


async def disconnect(session: AsyncSession, clock: Clock) -> int:
    """/claude disconnect: revoke the connection, its tokens and windows."""
    ids = await _active_connection_ids(session)
    await _revoke_connections(session, clock.now_utc(), ids)
    await session.commit()
    if ids:
        logger.info("oauth disconnected", extra={"event": "oauth_disconnect", "count": len(ids)})
    return len(ids)


async def revoke_everything(session: AsyncSession, clock: Clock) -> int:
    """With CLAUDE_ACCESS_ENABLED off: nothing may survive to be revived.

    Revokes every unrevoked connection and token and closes every Claude
    window, so turning the flag back on starts from nothing.
    """
    now = clock.now_utc()
    ids = await _active_connection_ids(session)
    await _revoke_connections(session, now, ids)
    await session.execute(
        update(OauthToken).where(OauthToken.revoked_at.is_(None)).values(revoked_at=now)
    )
    await grants.close_windows(session, now, None)
    await session.commit()
    if ids:
        logger.info("oauth revoked (flag off)", extra={"event": "oauth_flag_off", "count": len(ids)})
    return len(ids)


async def sweep(session: AsyncSession, clock: Clock) -> int:
    """Retention (app/core/retention.py): requests after a day, tokens
    past their own expiry (a replaced refresh token too: keeping it until
    then is what lets reuse be detected), revoked connections after 30
    days. Returns the number of rows removed."""
    now = clock.now_utc()
    await session.execute(
        update(OauthRequest)
        .where(OauthRequest.status == "approved", OauthRequest.code_expires_at <= now)
        .values(status="expired")
    )
    removed = 0
    for statement in (
        delete(OauthRequest).where(OauthRequest.created_at < now - REQUEST_RETENTION),
        delete(OauthToken).where(OauthToken.expires_at <= now),
        delete(OauthConnection).where(
            OauthConnection.revoked_at < now - REVOKED_CONNECTION_RETENTION
        ),
    ):
        removed += (await session.execute(statement)).rowcount or 0
    await session.commit()
    return removed

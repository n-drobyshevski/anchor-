"""app/web/auth.py (web-chat plan track 2, design section 4).

- scrypt round-trip: correct passphrase verifies, wrong one does not
- verify_passphrase runs a real scrypt hash even against a malformed
  configured hash (no early return before the CPU-bound work)
- CodeStore: single-use, TTL expiry, 5 wrong attempts invalidate it,
  and a fresh issue() replaces (not merges with) whatever was pending
- sessions: create/validate round-trip, idle timeout, absolute ceiling,
  the last_seen_at write is throttled, only the hash is ever stored
- revoke_session ends exactly one session; revoke_all ends every
  session and calls hub.close_all()
"""

from __future__ import annotations

import datetime

from sqlalchemy import func, select

from app.config import Settings
from app.core.clock import FrozenClock
from app.db.models import WebSession
from app.web import auth
from app.web.hub import WebHub
from scripts.web_passphrase import make_hash

PASSPHRASE = "correct horse battery staple"
HASH = make_hash(PASSPHRASE)


def _settings(**overrides) -> Settings:
    base = dict(WEB_SESSION_IDLE_HOURS=72, WEB_SESSION_MAX_DAYS=14, WEB_LOGIN_CODE_TTL_S=300)
    base.update(overrides)
    return Settings(**base)


# --- passphrase ---


async def test_correct_passphrase_verifies():
    assert await auth.verify_passphrase(PASSPHRASE, HASH) is True


async def test_wrong_passphrase_does_not_verify():
    assert await auth.verify_passphrase("wrong passphrase", HASH) is False


async def test_malformed_configured_hash_never_verifies():
    assert await auth.verify_passphrase(PASSPHRASE, "garbage") is False
    assert await auth.verify_passphrase(PASSPHRASE, "") is False


async def test_malformed_hash_still_costs_a_real_scrypt_call(monkeypatch):
    """The whole point of the dummy-salt fallback (design section 4:
    "Passphrase failures take similar time regardless of cause") is
    that hashlib.scrypt actually runs even when parsing failed."""
    calls = []
    import hashlib

    real_scrypt = hashlib.scrypt

    def spy(*args, **kwargs):
        calls.append(1)
        return real_scrypt(*args, **kwargs)

    monkeypatch.setattr(hashlib, "scrypt", spy)
    await auth.verify_passphrase(PASSPHRASE, "not-a-real-hash")
    assert calls == [1]


def test_parse_passphrase_hash_round_trips_salt_and_digest():
    parsed = auth.parse_passphrase_hash(HASH)
    assert parsed is not None
    salt, digest = parsed
    assert isinstance(salt, bytes) and len(salt) == 16
    assert isinstance(digest, bytes) and len(digest) == 64


def test_parse_passphrase_hash_rejects_garbage():
    assert auth.parse_passphrase_hash("nonsense") is None
    assert auth.parse_passphrase_hash("scrypt$16$8$1$c2FsdA$aGFzaA") is None  # wrong N


# --- CodeStore ---


def test_code_store_issue_and_verify_round_trip():
    store = auth.CodeStore()
    clock = FrozenClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    code = store.issue("pre-token", clock, ttl_s=300)
    assert len(code) == 9 and code[4] == "-"  # "XXXX-XXXX"
    assert store.verify("pre-token", code, clock) is True


def test_code_store_accepts_with_or_without_dash_case_insensitive():
    store = auth.CodeStore()
    clock = FrozenClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    code = store.issue("pre-token", clock, ttl_s=300)
    no_dash = code.replace("-", "").lower()
    assert store.verify("pre-token", no_dash, clock) is True


def test_code_store_is_single_use():
    store = auth.CodeStore()
    clock = FrozenClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    code = store.issue("pre-token", clock, ttl_s=300)
    assert store.verify("pre-token", code, clock) is True
    assert store.verify("pre-token", code, clock) is False  # already consumed


def test_code_store_expires_by_ttl():
    store = auth.CodeStore()
    clock = FrozenClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    code = store.issue("pre-token", clock, ttl_s=300)
    clock.advance(datetime.timedelta(seconds=301))
    assert store.verify("pre-token", code, clock) is False


def test_code_store_invalidates_after_five_wrong_attempts():
    store = auth.CodeStore()
    clock = FrozenClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    code = store.issue("pre-token", clock, ttl_s=300)
    for _ in range(auth.CODE_MAX_ATTEMPTS):
        assert store.verify("pre-token", "0000-0000", clock) is False
    # The 5 wrong attempts consumed the entry; even the real code fails now.
    assert store.verify("pre-token", code, clock) is False


def test_code_store_issue_replaces_a_pending_code():
    store = auth.CodeStore()
    clock = FrozenClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    old_code = store.issue("pre-token", clock, ttl_s=300)
    new_code = store.issue("pre-token", clock, ttl_s=300)
    assert store.verify("pre-token", old_code, clock) is False
    assert store.verify("pre-token", new_code, clock) is True


def test_code_store_clear_drops_every_pending_code():
    store = auth.CodeStore()
    clock = FrozenClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    code_a = store.issue("token-a", clock, ttl_s=300)
    code_b = store.issue("token-b", clock, ttl_s=300)

    store.clear()

    assert store.verify("token-a", code_a, clock) is False
    assert store.verify("token-b", code_b, clock) is False
    assert store.pending("token-a", clock) is False


def test_code_store_pending_reports_a_live_unconsumed_code():
    store = auth.CodeStore()
    clock = FrozenClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    assert store.pending("pre-token", clock) is False
    store.issue("pre-token", clock, ttl_s=300)
    assert store.pending("pre-token", clock) is True
    clock.advance(datetime.timedelta(seconds=301))
    assert store.pending("pre-token", clock) is False


# --- sessions ---


async def test_create_and_validate_session_round_trip(sessionmaker):
    clock = FrozenClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    settings = _settings()
    async with sessionmaker() as session:
        token, expires_at = await auth.create_session(session, clock, settings)
    assert expires_at == clock.now_utc() + datetime.timedelta(days=14)

    async with sessionmaker() as session:
        assert await auth.validate_session(session, clock, settings, token) is True
        assert await auth.validate_session(session, clock, settings, "wrong-token") is False
        assert await auth.validate_session(session, clock, settings, None) is False


async def test_only_the_token_hash_is_ever_stored(sessionmaker):
    clock = FrozenClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    async with sessionmaker() as session:
        token, _ = await auth.create_session(session, clock, _settings())

    async with sessionmaker() as session:
        result = await session.execute(select(WebSession))
        rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].token_hash != token.encode()
    assert token not in repr(rows[0].token_hash)


async def test_session_idle_timeout(sessionmaker):
    clock = FrozenClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    settings = _settings(WEB_SESSION_IDLE_HOURS=72)
    async with sessionmaker() as session:
        token, _ = await auth.create_session(session, clock, settings)

    clock.advance(datetime.timedelta(hours=73))
    async with sessionmaker() as session:
        assert await auth.validate_session(session, clock, settings, token) is False


async def test_session_absolute_ceiling(sessionmaker):
    clock = FrozenClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    settings = _settings(WEB_SESSION_IDLE_HOURS=999999, WEB_SESSION_MAX_DAYS=14)
    async with sessionmaker() as session:
        token, _ = await auth.create_session(session, clock, settings)

    clock.advance(datetime.timedelta(days=15))
    async with sessionmaker() as session:
        assert await auth.validate_session(session, clock, settings, token) is False


async def test_last_seen_write_is_throttled(sessionmaker):
    clock = FrozenClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    settings = _settings()
    async with sessionmaker() as session:
        token, _ = await auth.create_session(session, clock, settings)

    clock.advance(datetime.timedelta(seconds=10))
    async with sessionmaker() as session:
        await auth.validate_session(session, clock, settings, token)
    async with sessionmaker() as session:
        result = await session.execute(select(WebSession))
        row = result.scalar_one()
        first_seen = row.last_seen_at

    clock.advance(datetime.timedelta(seconds=10))  # under the 60s throttle
    async with sessionmaker() as session:
        await auth.validate_session(session, clock, settings, token)
    async with sessionmaker() as session:
        result = await session.execute(select(WebSession))
        row = result.scalar_one()
        assert row.last_seen_at == first_seen  # unchanged: throttled

    clock.advance(datetime.timedelta(seconds=61))  # past the throttle window
    async with sessionmaker() as session:
        await auth.validate_session(session, clock, settings, token)
    async with sessionmaker() as session:
        result = await session.execute(select(WebSession))
        row = result.scalar_one()
        assert row.last_seen_at > first_seen


async def test_revoke_session_ends_only_that_session(sessionmaker):
    clock = FrozenClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    settings = _settings()
    async with sessionmaker() as session:
        token_a, _ = await auth.create_session(session, clock, settings)
    async with sessionmaker() as session:
        token_b, _ = await auth.create_session(session, clock, settings)

    async with sessionmaker() as session:
        await auth.revoke_session(session, token_a)

    async with sessionmaker() as session:
        assert await auth.validate_session(session, clock, settings, token_a) is False
        assert await auth.validate_session(session, clock, settings, token_b) is True


async def test_non_ascii_session_cookie_is_treated_as_invalid_not_an_error(sessionmaker):
    """Low-severity finding: `_hash_token` used to do
    `token.encode('ascii')`, which raised UnicodeEncodeError -- an
    unhandled 500, skipping app/web/security.py's header middleware
    entirely -- for any unauthenticated request carrying a non-ASCII
    __Host-anchor_s cookie. Every endpoint that calls validate_session/
    revoke_session was reachable this way without authenticating at
    all. It must instead just be "not a valid session"."""
    clock = FrozenClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    settings = _settings()
    async with sessionmaker() as session:
        assert await auth.validate_session(session, clock, settings, "é") is False
        assert await auth.validate_session(session, clock, settings, "тест") is False
        # A lone surrogate codepoint (the shape a mis-decoded raw header
        # byte can leave behind) must not raise either.
        assert await auth.validate_session(session, clock, settings, "\ud800") is False
        await auth.revoke_session(session, "é")  # must not raise


async def test_revoke_session_with_no_token_is_a_no_op(sessionmaker):
    async with sessionmaker() as session:
        await auth.revoke_session(session, None)  # must not raise


async def test_revoke_all_ends_every_session_and_closes_the_hub(sessionmaker):
    clock = FrozenClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    settings = _settings()
    async with sessionmaker() as session:
        token_a, _ = await auth.create_session(session, clock, settings)
    async with sessionmaker() as session:
        token_b, _ = await auth.create_session(session, clock, settings)

    hub = WebHub()
    sub = hub.subscribe()
    hub.register_keyboard(1, [[{"text": "Да", "data": "w:resume"}]])

    async with sessionmaker() as session:
        await auth.revoke_all(session, hub)

    async with sessionmaker() as session:
        assert await auth.validate_session(session, clock, settings, token_a) is False
        assert await auth.validate_session(session, clock, settings, token_b) is False
        result = await session.execute(select(func.count()).select_from(WebSession))
        assert result.scalar_one() == 0

    assert hub.allow_press(1, "w:resume") is False  # allowlist cleared
    events = [record async for record in sub.events()]
    assert events == []  # the poison pill ended the stream with nothing further


async def test_revoke_all_also_invalidates_pending_login_codes(sessionmaker):
    """Low-severity finding: the login-code message tells the owner
    'Если это не ты — /weblogout', but revoke_all used to touch only
    web_session -- a code already issued (or requested moments later,
    were the CodeStore not cleared) stayed valid and let the attacker's
    login finish anyway. `code_store` is now threaded through and
    cleared in the same call."""
    clock = FrozenClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    hub = WebHub()
    code_store = auth.CodeStore()
    code = code_store.issue("pre-token", clock, ttl_s=300)

    async with sessionmaker() as session:
        await auth.revoke_all(session, hub, code_store)

    assert code_store.verify("pre-token", code, clock) is False
    assert code_store.pending("pre-token", clock) is False


async def test_revoke_all_without_a_code_store_still_revokes_everything_else(sessionmaker):
    """`code_store=None` (the default) must not be a hard requirement --
    every one of this module's own tests that predates the parameter,
    and any future caller with no CodeStore of its own to hand, still
    gets a working revoke of the session table and the hub."""
    clock = FrozenClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    settings = _settings()
    async with sessionmaker() as session:
        token, _ = await auth.create_session(session, clock, settings)
    hub = WebHub()

    async with sessionmaker() as session:
        await auth.revoke_all(session, hub)  # no code_store passed

    async with sessionmaker() as session:
        assert await auth.validate_session(session, clock, settings, token) is False

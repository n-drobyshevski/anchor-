"""Opt-in read grants for an outside assistant (docs/grok-access.md).

A grant is created only by the user pressing [Разрешить] on /grok. It
carries a set of scopes, an optional look-back for dialogs, and an
expiry; /revoke ends it early and /delete wipes the table.

The capability token is `secrets.token_urlsafe(32)` -- 256 bits -- and
only its sha256 is stored. `find_active_grant` hashes what it is given
and looks the hash up, so a timing difference can only reveal whether
some *hash* prefix exists, which says nothing about a valid token.

A grant belongs to one client (`access_grant.client`). Grok's grants
carry a capability token (`create_grant`, `find_active_grant`, which
looks at nothing but `grok` rows). Claude's are *windows* on an OAuth
connection: no token of their own, opened by `open_window`, at most one
open at a time, found by `find_open_window` (connector plan section
6.1). /revoke closes both kinds.

The read functions return plain JSON-ready dicts. They are the only
place the MCP endpoints (app/web/mcp_core.py) touch content, so what
an outside assistant can see is decided here and nowhere else:

- dialogs exclude `ooc` rows and the welfare/canned/system kinds. A
  welfare exchange is the most sensitive thing this bot stores, and
  the same exclusion already keeps it out of scene summaries.
- nothing reads telegram_update, job, pending_memory or the research
  tables.
"""

from __future__ import annotations

import datetime
import hashlib
import secrets

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.export import encode
from app.db.models import (
    AccessGrant,
    Checkin,
    Journal,
    Memory,
    Message,
    Scene,
    SpendLedger,
    UserState,
)

SCOPES = ("memory", "journal", "dialogs", "state")
# Message kinds an outside assistant may read. Not 'welfare' (see the
# module docstring), not 'canned'/'system' (bot plumbing, not dialog).
DIALOG_KINDS = ("chat", "checkin", "outbound")
MAX_DIALOG_MESSAGES = 500
NOTIFY_EVERY = datetime.timedelta(minutes=10)
TOKEN_BYTES = 32
GROK = "grok"
CLAUDE = "claude"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_grant(
    session: AsyncSession,
    clock: Clock,
    *,
    scopes: tuple[str, ...] | list[str],
    ttl_hours: int,
    dialog_days: int | None = None,
    max_hours: int = 168,
) -> tuple[str, AccessGrant]:
    """Create a grant and return (token, row). The token is not stored."""
    scopes = [s for s in SCOPES if s in set(scopes)]
    if not scopes:
        raise ValueError("a grant needs at least one scope")
    ttl_hours = max(1, min(int(ttl_hours), max_hours))
    now = clock.now_utc()
    token = secrets.token_urlsafe(TOKEN_BYTES)
    grant = AccessGrant(
        client=GROK,
        token_sha256=hash_token(token),
        scopes=scopes,
        dialog_days=dialog_days if "dialogs" in scopes else None,
        created_at=now,
        expires_at=now + datetime.timedelta(hours=ttl_hours),
    )
    session.add(grant)
    await session.commit()
    await session.refresh(grant)
    return token, grant


async def find_active_grant(
    session: AsyncSession, clock: Clock, token: str
) -> AccessGrant | None:
    result = await session.execute(
        select(AccessGrant).where(
            AccessGrant.client == GROK,
            AccessGrant.token_sha256 == hash_token(token),
            AccessGrant.revoked_at.is_(None),
            AccessGrant.expires_at > clock.now_utc(),
        )
    )
    return result.scalar_one_or_none()


async def list_active(
    session: AsyncSession, clock: Clock, client: str | None = None
) -> list[AccessGrant]:
    query = select(AccessGrant).where(
        AccessGrant.revoked_at.is_(None), AccessGrant.expires_at > clock.now_utc()
    )
    if client is not None:
        query = query.where(AccessGrant.client == client)
    result = await session.execute(query.order_by(AccessGrant.id))
    return list(result.scalars().all())


async def revoke_all(session: AsyncSession, clock: Clock) -> int:
    return sum((await revoke_all_by_client(session, clock)).values())


async def revoke_all_by_client(session: AsyncSession, clock: Clock) -> dict[str, int]:
    """Close every open grant and window. Returns {client: how many}."""
    result = await session.execute(
        update(AccessGrant)
        .where(AccessGrant.revoked_at.is_(None), AccessGrant.expires_at > clock.now_utc())
        .values(revoked_at=clock.now_utc())
        .returning(AccessGrant.client)
    )
    counts = {GROK: 0, CLAUDE: 0}
    for client in result.scalars().all():
        counts[client] = counts.get(client, 0) + 1
    await session.commit()
    return counts


# --- Claude windows ---


async def open_window(
    session: AsyncSession,
    clock: Clock,
    *,
    connection_id: int,
    scopes: tuple[str, ...] | list[str],
    ttl_hours: int,
    dialog_days: int | None = None,
    max_hours: int = 24,
) -> AccessGrant:
    """Open the one Claude window on a connection, closing any other.

    The caller has checked that the connection is the active one; the
    foreign key and the pairing checks refuse anything else.
    """
    scopes = [s for s in SCOPES if s in set(scopes)]
    if not scopes:
        raise ValueError("a window needs at least one scope")
    ttl_hours = max(1, min(int(ttl_hours), max_hours))
    now = clock.now_utc()
    await close_windows(session, now, None)
    window = AccessGrant(
        client=CLAUDE,
        connection_id=connection_id,
        scopes=scopes,
        dialog_days=dialog_days if "dialogs" in scopes else None,
        created_at=now,
        expires_at=now + datetime.timedelta(hours=ttl_hours),
    )
    session.add(window)
    await session.commit()
    await session.refresh(window)
    return window


async def find_open_window(
    session: AsyncSession, clock: Clock, connection_id: int
) -> AccessGrant | None:
    """The connection's open window, if any (at most one by construction)."""
    result = await session.execute(
        select(AccessGrant)
        .where(
            AccessGrant.client == CLAUDE,
            AccessGrant.connection_id == connection_id,
            AccessGrant.revoked_at.is_(None),
            AccessGrant.expires_at > clock.now_utc(),
        )
        .order_by(AccessGrant.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def close_windows(
    session: AsyncSession, now: datetime.datetime, connection_ids: list[int] | None
) -> None:
    """Close open Claude windows (all, or those of `connection_ids`). No commit:
    the caller closes them in the same transaction as whatever ended them."""
    statement = update(AccessGrant).where(
        AccessGrant.client == CLAUDE, AccessGrant.revoked_at.is_(None)
    )
    if connection_ids is not None:
        statement = statement.where(AccessGrant.connection_id.in_(connection_ids))
    await session.execute(statement.values(revoked_at=now))


async def record_use(session: AsyncSession, clock: Clock, grant_id: int) -> bool:
    """Count one read. True when the user should be told about it now.

    The first read of a grant always notifies; after that at most once
    per NOTIFY_EVERY, so an assistant paging through tools does not
    flood the chat but a read hours later still shows up.
    """
    now = clock.now_utc()
    grant = await session.get(AccessGrant, grant_id)
    if grant is None:
        return False
    grant.use_count += 1
    grant.last_used_at = now
    notify = grant.last_notified_at is None or now - grant.last_notified_at >= NOTIFY_EVERY
    if notify:
        grant.last_notified_at = now
    await session.commit()
    return notify


# --- reads ---


async def read_memory(session: AsyncSession) -> list[dict]:
    result = await session.execute(
        select(Memory)
        .where(Memory.superseded_by.is_(None))
        .order_by(Memory.pinned.desc(), Memory.id)
    )
    return [
        {
            "id": m.id,
            "kind": m.kind,
            "text": m.text,
            "pinned": m.pinned,
            "created_at": encode(m.created_at),
        }
        for m in result.scalars().all()
    ]


async def read_journal(session: AsyncSession, since: datetime.date) -> dict:
    journal = await session.execute(
        select(Journal).where(Journal.local_date >= since).order_by(Journal.local_date, Journal.id)
    )
    checkins = await session.execute(
        select(Checkin).where(Checkin.local_date >= since).order_by(Checkin.local_date)
    )
    return {
        "journal": [
            {"date": encode(j.local_date), "text": j.text} for j in journal.scalars().all()
        ],
        "checkins": [
            {
                "date": encode(c.local_date),
                "day_rating": c.day_rating,
                "due_result": c.due_result,
                "note": c.note,
            }
            for c in checkins.scalars().all()
        ],
    }


def dialog_since(
    clock: Clock, grant: AccessGrant, requested_days: int
) -> datetime.datetime:
    """The requested look-back, never further than the grant allows."""
    days = max(1, int(requested_days))
    if grant.dialog_days is not None:
        days = min(days, grant.dialog_days)
    return clock.now_utc() - datetime.timedelta(days=days)


async def read_dialogs(
    session: AsyncSession, since: datetime.datetime, limit: int
) -> dict:
    limit = max(1, min(int(limit), MAX_DIALOG_MESSAGES))
    # Newest `limit` rows in the window, returned oldest first.
    messages = await session.execute(
        select(Message)
        .where(
            Message.created_at >= since,
            Message.ooc.is_(False),
            Message.kind.in_(DIALOG_KINDS),
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit)
    )
    scenes = await session.execute(
        select(Scene)
        .where(Scene.started_at >= since, Scene.summary.is_not(None))
        .order_by(Scene.started_at)
    )
    return {
        "messages": [
            {"role": m.role, "at": encode(m.created_at), "text": m.content}
            for m in reversed(messages.scalars().all())
        ],
        "scene_summaries": [
            {"started_at": encode(s.started_at), "summary": s.summary}
            for s in scenes.scalars().all()
        ],
    }


async def read_state(session: AsyncSession, today: datetime.date) -> dict:
    state = await session.get(UserState, 1)
    spend = await session.execute(
        select(SpendLedger.local_date, func.sum(SpendLedger.usd_cost))
        .where(SpendLedger.local_date > today - datetime.timedelta(days=7))
        .group_by(SpendLedger.local_date)
        .order_by(SpendLedger.local_date)
    )
    out: dict = {
        "spend_last_7_days_usd": [
            {"date": encode(day), "usd": encode(total)} for day, total in spend.all()
        ]
    }
    if state is not None:
        out.update(
            {
                "persona_active": state.persona_active,
                "intensity": state.intensity,
                "timezone": state.timezone,
                "focus_on": state.focus_on,
                "due_action": state.due_action,
                "streak": state.streak,
                "last_checkin_at": encode(state.last_checkin_at),
                "quiet_until": encode(state.quiet_until),
            }
        )
    return out

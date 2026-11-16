"""The code window's two tables (anchor-code-window-plan.md section 4.2, CW1).

A code window is a stretch of time, opened by the user from Telegram
(`/vault code on`, then «Открыть» on a card), during which Claude Code
may list and read vault notes through the bot's `/code/v1` routes
(app/web/code_access.py). This module is the only code that names
`code_window` and `code_read` (pinned by
tests/test_vault_notes_isolation.py), and it stores counts only: it
never receives a path, a title or note text. `roots` holds the folders
the user typed into Telegram, which are already in `message`.

**The lifecycle** (plan section 4.2):

- `create_pending` writes the card's row. Nothing is open yet.
- `open_pending` opens it, once, if the button that names it is the one
  issued for it; any other open window closes as `replaced` in the same
  transaction, and any other pending row is dropped (it opened
  nothing). A partial unique index keeps at most one row `open` even
  against a race.
- `cancel_pending` drops a pending row: «Отмена».
- `close_open` closes the open window now, as `off` (`/vault code off`,
  or startup with CODE_ACCESS_ENABLED off).
- A window past `expires_at` is closed for every reader: `current`
  marks it `closed`/`expired` lazily, whoever asks first.

**The ledger.** `record_read` writes one `code_read` row per successful
`tree` or `file` call and, in the same statement order, stamps
`notified_at` if it was empty: True means "this was the window's first
read, send the one Telegram notice". `files_read`, `reads_between` and
`active_between` are what `/vault code`, the file cap, the daily digest
and the scheduler ask.

**Roots and paths** are checked here by path segments, never by string
prefix (`Notes` does not cover `Notes2`), and `Anchor` is refused in any
case (`anchor/Memory` too), on the first segment only, and segments
are compared after NFC normalisation, as vaultd compares them. vaultd checks again (plan section 3.3); this
is the bot's own layer, not the boundary.

Every time comes from the injected `Clock`. Nothing here logs: callers
log ids and counts.
"""

from __future__ import annotations

import datetime
import unicodedata
from dataclasses import dataclass

from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.db.models import CodeRead, CodeWindow

__all__ = [
    "MAX_ROOTS",
    "ROUTES",
    "TTL_CHOICES",
    "Window",
    "active_between",
    "cancel_pending",
    "close_open",
    "covers",
    "create_pending",
    "current",
    "files_read",
    "is_anchor",
    "last_closed",
    "open_pending",
    "parse_rel",
    "parse_root",
    "pending",
    "reads_between",
    "record_read",
]

# `/vault code on [1h|6h|24h]`, default 1h (plan section 4.3). The
# migration's ck_code_window_ttl allows exactly these.
TTL_CHOICES: dict[str, int] = {"1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600}
DEFAULT_TTL = "1h"
# At most this many roots per window (plan section 4.3).
MAX_ROOTS = 8
ROUTES = ("tree", "file")
# A pending row older than this can no longer be opened (its button is
# past app/tg/data.py's CONFIRM_TTL, 300 s), so the next card drops it.
STALE_PENDING = datetime.timedelta(minutes=10)

# The bot's own folders, closed in every window (plan section 0,
# decision 1), compared case-insensitively.
ANCHOR_DIR = "anchor"


@dataclass(frozen=True)
class Window:
    """A `code_window` row, as its readers need it. No content."""

    id: int
    status: str
    roots: tuple[str, ...]
    ttl_s: int
    created_at: datetime.datetime
    opened_at: datetime.datetime | None
    expires_at: datetime.datetime | None
    closed_at: datetime.datetime | None
    closed_reason: str | None
    notified_at: datetime.datetime | None


def _window(row: CodeWindow) -> Window:
    return Window(
        id=row.id,
        status=row.status,
        roots=tuple(row.roots),
        ttl_s=row.ttl_s,
        created_at=row.created_at,
        opened_at=row.opened_at,
        expires_at=row.expires_at,
        closed_at=row.closed_at,
        closed_reason=row.closed_reason,
        notified_at=row.notified_at,
    )


# --- roots and paths -------------------------------------------------------


def parse_rel(raw: str) -> str | None:
    """`raw` if it is a well-formed vault-relative path, else None.

    Never rewrites it: a file path goes to vaultd as Claude Code sent
    it (vaultd echoes the request's own string, and the disk may store
    a name in NFD). Refuses what vaultd's `paths.parse_rel` refuses --
    an empty path, a leading or trailing slash, a backslash, a control
    character, an empty, `.` or `..` segment -- and a segment starting
    with a dot (vaultd serves no dot-folder). Says nothing about
    `Anchor`: see `is_anchor`.
    """
    if not isinstance(raw, str) or not raw:
        return None
    if raw.startswith("/") or raw.endswith("/") or "\\" in raw:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        return None
    for segment in raw.split("/"):
        if not segment or segment.startswith("."):
            return None
    return raw


def parse_root(raw: str) -> str | None:
    """A folder the user typed into Telegram: surrounding whitespace and
    one trailing slash dropped, NFC-normalised, then `parse_rel`."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if value.endswith("/"):
        value = value[:-1].rstrip()
    return parse_rel(unicodedata.normalize("NFC", value))


def _segments(rel: str) -> list[str]:
    """Segments as compared: after Unicode NFC normalisation, the way
    vaultd compares them (a name typed in NFC matches one stored NFD)."""
    return unicodedata.normalize("NFC", rel).split("/")


def is_anchor(rel: str) -> bool:
    """True for `Anchor` and anything under it, in any case. Only the
    first segment: `Projects/Anchor/x.md` is an ordinary note (vaultd
    serves it too)."""
    return _segments(rel)[0].casefold() == ANCHOR_DIR


def covers(roots: tuple[str, ...] | list[str], rel: str) -> bool:
    """True when `rel` is one of `roots` or lies under one, by whole
    NFC-normalised segments (`Notes` covers `Notes/a.md`, never
    `Notes2/a.md`). No roots means the whole vault. `Anchor/` is never
    covered."""
    if is_anchor(rel):
        return False
    if not roots:
        return True
    parts = _segments(rel)
    for root in roots:
        prefix = _segments(root)
        if parts[: len(prefix)] == prefix:
            return True
    return False


# --- the lifecycle -----------------------------------------------------------


async def _expire(session: AsyncSession, now: datetime.datetime) -> None:
    """Mark every open window past its time `closed`/`expired`. No commit."""
    await session.execute(
        update(CodeWindow)
        .where(CodeWindow.status == "open", CodeWindow.expires_at <= now)
        .values(status="closed", closed_at=CodeWindow.expires_at, closed_reason="expired")
    )


async def create_pending(
    session: AsyncSession, clock: Clock, roots: list[str], ttl_s: int
) -> Window:
    """The card's row (plan section 4.3): `pending`, nothing open yet.

    The caller has validated `roots` (`parse_rel`, not `is_anchor`, at
    most MAX_ROOTS) and picked `ttl_s` from TTL_CHOICES. Pending rows
    past STALE_PENDING are dropped first. Commits.
    """
    if ttl_s not in TTL_CHOICES.values():
        raise ValueError("unknown window lifetime")
    if len(roots) > MAX_ROOTS:
        raise ValueError("too many roots")
    now = clock.now_utc()
    await session.execute(
        delete(CodeWindow).where(
            CodeWindow.status == "pending", CodeWindow.created_at < now - STALE_PENDING
        )
    )
    row = CodeWindow(status="pending", roots=list(roots), ttl_s=ttl_s, created_at=now)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _window(row)


async def pending(session: AsyncSession, window_id: int) -> Window | None:
    row = await session.get(CodeWindow, window_id)
    if row is None or row.status != "pending":
        return None
    return _window(row)


async def open_pending(
    session: AsyncSession, clock: Clock, window_id: int, issued_at: int
) -> Window | None:
    """Open the pending window `window_id` («Открыть»), or None.

    None when the row is not pending any more (opened, cancelled,
    dropped by a newer open, wiped by /delete) or when `issued_at` is
    not the second its card was issued -- a button the bot never sent
    for this row. Any open window closes as `replaced`, any other
    pending row is dropped, in the same transaction. Commits.
    """
    now = clock.now_utc()
    row = (
        await session.execute(
            select(CodeWindow).where(CodeWindow.id == window_id).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None or row.status != "pending" or int(row.created_at.timestamp()) != issued_at:
        await session.rollback()
        return None
    await _expire(session, now)
    await session.execute(
        update(CodeWindow)
        .where(CodeWindow.status == "open")
        .values(status="closed", closed_at=now, closed_reason="replaced")
    )
    await session.execute(
        delete(CodeWindow).where(CodeWindow.status == "pending", CodeWindow.id != window_id)
    )
    row.status = "open"
    row.opened_at = now
    row.expires_at = now + datetime.timedelta(seconds=row.ttl_s)
    await session.commit()
    await session.refresh(row)
    return _window(row)


async def cancel_pending(session: AsyncSession, window_id: int, issued_at: int) -> bool:
    """«Отмена»: drop the pending row. False when there is none to drop
    (already decided) or the button was not issued for it. Commits."""
    row = await session.get(CodeWindow, window_id)
    if row is None or row.status != "pending" or int(row.created_at.timestamp()) != issued_at:
        return False
    await session.delete(row)
    await session.commit()
    return True


async def current(session: AsyncSession, clock: Clock) -> Window | None:
    """The open window, or None. A window past its time is closed here,
    as `expired`, and committed -- whoever notices first marks it."""
    now = clock.now_utc()
    row = (
        await session.execute(select(CodeWindow).where(CodeWindow.status == "open"))
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.expires_at <= now:
        await _expire(session, now)
        await session.commit()
        return None
    return _window(row)


async def close_open(session: AsyncSession, clock: Clock, reason: str = "off") -> Window | None:
    """Close the open window now (`/vault code off`: `off`). Returns it,
    or None when nothing was open (an expired one is marked `expired`,
    not `off`). Commits."""
    if reason != "off":
        raise ValueError("unknown close reason")
    open_window = await current(session, clock)
    if open_window is None:
        await session.commit()
        return None
    now = clock.now_utc()
    await session.execute(
        update(CodeWindow)
        .where(CodeWindow.id == open_window.id, CodeWindow.status == "open")
        .values(status="closed", closed_at=now, closed_reason=reason)
    )
    await session.commit()
    return await _get(session, open_window.id)


async def _get(session: AsyncSession, window_id: int) -> Window | None:
    row = (
        await session.execute(
            select(CodeWindow).where(CodeWindow.id == window_id).execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    return None if row is None else _window(row)


async def last_closed(session: AsyncSession) -> Window | None:
    """The most recently closed window, for `/vault code`'s «последнее
    закрылось» line."""
    row = (
        await session.execute(
            select(CodeWindow)
            .where(CodeWindow.status == "closed")
            .order_by(CodeWindow.closed_at.desc(), CodeWindow.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return None if row is None else _window(row)


# --- the ledger --------------------------------------------------------------


async def record_read(
    session: AsyncSession, clock: Clock, window_id: int, route: str, files: int, size: int
) -> bool:
    """One `code_read` row; True when it is the window's first read (its
    `notified_at` was empty and is stamped now), so the caller sends the
    one notice. Commits."""
    if route not in ROUTES:
        raise ValueError("unknown route")
    now = clock.now_utc()
    await session.execute(
        insert(CodeRead).values(window_id=window_id, at=now, route=route, files=files, bytes=size)
    )
    first = (
        await session.execute(
            update(CodeWindow)
            .where(CodeWindow.id == window_id, CodeWindow.notified_at.is_(None))
            .values(notified_at=now)
            .returning(CodeWindow.id)
        )
    ).scalar_one_or_none()
    await session.commit()
    return first is not None


async def files_read(session: AsyncSession, window_id: int) -> int:
    """How many notes this window has served through `file` (the cap)."""
    return int(
        (
            await session.execute(
                select(func.coalesce(func.sum(CodeRead.files), 0)).where(
                    CodeRead.window_id == window_id, CodeRead.route == "file"
                )
            )
        ).scalar_one()
    )


async def reads_between(
    session: AsyncSession, start: datetime.datetime, end: datetime.datetime
) -> tuple[int, int]:
    """(files read through `file`, requests of either route) in [start, end)."""
    files, requests = (
        await session.execute(
            select(
                func.coalesce(func.sum(CodeRead.files).filter(CodeRead.route == "file"), 0),
                func.count(),
            ).where(CodeRead.at >= start, CodeRead.at < end)
        )
    ).one()
    return int(files), int(requests)


async def active_between(
    session: AsyncSession, start: datetime.datetime, end: datetime.datetime
) -> bool:
    """Was a window open at some moment of [start, end), or was there a
    read in it? The daily digest's trigger (plan section 4.5)."""
    window = (
        await session.execute(
            select(CodeWindow.id)
            .where(
                CodeWindow.opened_at.is_not(None),
                CodeWindow.opened_at < end,
                CodeWindow.expires_at > start,
                or_(CodeWindow.closed_at.is_(None), CodeWindow.closed_at > start),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if window is not None:
        return True
    _files, requests = await reads_between(session, start, end)
    return requests > 0

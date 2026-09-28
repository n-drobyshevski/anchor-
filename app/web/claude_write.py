"""Claude writes knowledge notes: the bot's side (W2b,
anchor-claude-write-plan.md sections 3, 4, 6).

Everything a write MCP tool needs before/around a call to vaultd's
knowledge routes (app/vault/client.py): title sanitising, the
instruction/secret content checks, the bot's own caps (mirroring
vaultd's, app/core/claude_write_limits.py) and the changeset ledger
(`claude_changeset`, app/db/models.py) that the digest and `/claude
undo` both read.

**One refusal text for the tool surface.** Every function here returns
either a success payload or raises `Refused(code)`; `code` is a short
reason for the log line only (`app/web/mcp_core.py` turns every
`Refused` into the fixed `WRITE_REFUSED_TEXT`, never this code, never a
path). The two exceptions that are *not* `WRITE_REFUSED_TEXT` -- the
write switch being off, and no connection at all -- are handled by the
caller before these functions are ever reached (they need their own,
different, non-secret text per plan section 3).

**Caps are ledger-backed, not in-memory.** `claude_changeset` already
carries every column a cap needs (`files`, `bytes`, `created`,
`created_at`), so every cap is a `SELECT`/`SUM` over that table for the
current connection, computed and checked in the same transaction as
the write's own bookkeeping -- a worker restart cannot loosen any of
them, unlike an in-memory sliding window (the MCP rate limiter, the C2
connect lockout).

**Nothing here imports `notes_personal`** (app/vault isolation,
plan section 9). Only `app/web/mcp_core.py` calls into this module.
"""

from __future__ import annotations

import datetime
import logging
import re
import secrets

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import claude_write_limits as limits
from app.core import clock as clock_module
from app.core.clock import Clock
from app.core import redact
from app.db.models import ClaudeChangeset, UserState
from app.research import injection
from app.vault import secrets as vault_secrets
from app.vault.client import ChangeEntry, VaultClient
from app.vault.errors import VaultError

logger = logging.getLogger(__name__)

# plan section 6.5: the instruction ids that refuse a write. `url`,
# `handle` and `code_fence` are deliberately excluded -- a knowledge
# note legitimately holds links and code -- and there is no rule
# exemption for the rest, unlike app/vault's own memory-write path.
# Spelled out literally, not derived from injection.RULE_IDS, so a
# future addition to that list does not silently start refusing
# knowledge writes.
REFUSE_INJECTION_IDS = frozenset(
    {
        "override_previous",
        "override_previous_en",
        "override_previous_fr",
        "system_prompt",
        "developer_mode",
        "role_tag",
        "exfiltrate",
        "role_reassign",
        "speak_as_assistant",
    }
)

_BAD_TITLE_CHARS = re.compile(r"[/\\\x00-\x1f]")
MAX_TITLE_CHARS = 120


class Refused(Exception):
    """Every refusal on the write/undo tool surface. `code` is a reason
    for the log line only -- never shown to Claude, never a path."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def sanitize_title(title: str) -> str:
    """A safe file name: no `/`, no leading `.`, at most
    MAX_TITLE_CHARS, `.md` appended. Raises `Refused("bad_title")` for
    a title that sanitises to nothing."""
    cleaned = _BAD_TITLE_CHARS.sub("_", title.strip())
    while cleaned.startswith("."):
        cleaned = cleaned[1:]
    cleaned = cleaned.strip()[:MAX_TITLE_CHARS].strip()
    if not cleaned:
        raise Refused("bad_title")
    return cleaned + ".md"


def sanitize_folder(folder: str) -> str:
    """A nested folder path for `create_note` (rev. 3, plan section 14,
    BUILD item 7): each segment checked the way a title is -- no empty
    segment, no `..`, no leading `.`. vaultd decides the rest (segment
    length, control characters, `Anchor`, NFC, symlinks, and whether the
    path may actually be built -- the knowledge-root/depth rules)."""
    cleaned = []
    for segment in folder.strip("/").split("/"):
        segment = segment.strip()
        if not segment or segment == ".." or segment.startswith("."):
            raise Refused("bad_folder")
        cleaned.append(segment)
    if not cleaned:
        raise Refused("bad_folder")
    return "/".join(cleaned)


def _check_content(new_body: str) -> None:
    if set(injection.hits(new_body)) & REFUSE_INJECTION_IDS:
        raise Refused("instruction")
    if redact.secret_spans(new_body) or vault_secrets.spans(new_body):
        raise Refused("secret")


async def _local_day_start(session: AsyncSession, clock: Clock) -> datetime.datetime:
    state = await session.get(UserState, 1)
    timezone = state.timezone if state is not None else "UTC"
    today = clock_module.local_date(clock, timezone)
    return clock_module.combine_local(today, datetime.time(0, 0), timezone)


async def _open_changeset(
    session: AsyncSession, clock: Clock, connection_id: int
) -> ClaudeChangeset:
    """The connection's current open write changeset: reused if the
    last write was under CHANGESET_IDLE ago, else freshly minted.
    Raises `Refused("cap_changesets")` if minting a new one would
    exceed CHANGESETS_PER_HOUR. No commit -- the caller's own
    transaction covers this row and whatever it does next."""
    now = clock.now_utc()
    row = await session.scalar(
        select(ClaudeChangeset)
        .where(
            ClaudeChangeset.connection_id == connection_id,
            ClaudeChangeset.kind == "write",
            ClaudeChangeset.undone_at.is_(None),
        )
        .order_by(ClaudeChangeset.id.desc())
        .limit(1)
    )
    if row is not None and now - row.last_write_at < limits.CHANGESET_IDLE:
        return row

    count = await session.scalar(
        select(func.count())
        .select_from(ClaudeChangeset)
        .where(
            ClaudeChangeset.connection_id == connection_id,
            ClaudeChangeset.kind == "write",
            ClaudeChangeset.created_at >= now - datetime.timedelta(hours=1),
        )
    )
    if count >= limits.CHANGESETS_PER_HOUR:
        raise Refused("cap_changesets")

    new_row = ClaudeChangeset(
        connection_id=connection_id,
        vault_ref=secrets.token_urlsafe(24),
        kind="write",
        files=0,
        bytes=0,
        refused=0,
        created=0,
        renamed=0,
        folders=0,
        moves=0,
        created_at=now,
        last_write_at=now,
    )
    session.add(new_row)
    await session.flush()
    return new_row


async def _check_day_caps(
    session: AsyncSession, clock: Clock, connection_id: int, *, new_bytes: int, is_create: bool
) -> None:
    day_start = await _local_day_start(session, clock)
    totals = (
        await session.execute(
            select(func.coalesce(func.sum(ClaudeChangeset.bytes), 0), func.coalesce(func.sum(ClaudeChangeset.created), 0))
            .where(
                ClaudeChangeset.connection_id == connection_id,
                ClaudeChangeset.kind == "write",
                ClaudeChangeset.created_at >= day_start,
            )
        )
    ).one()
    bytes_so_far, creates_so_far = totals
    if bytes_so_far + new_bytes > limits.BYTES_PER_CONNECTION_PER_DAY:
        raise Refused("cap_bytes_day")
    if is_create and creates_so_far >= limits.CREATES_PER_DAY:
        raise Refused("cap_creates")


async def _check_day_folder_cap(session: AsyncSession, clock: Clock, connection_id: int) -> None:
    """Rev. 3's FOLDERS_PER_DAY, fast-rejected the same way
    `_check_day_caps` rejects CREATES_PER_DAY -- a local check against
    the ledger's sum so far, before ever calling vaultd. vaultd enforces
    the authoritative copy of this same cap on its own changeset."""
    day_start = await _local_day_start(session, clock)
    folders_so_far = await session.scalar(
        select(func.coalesce(func.sum(ClaudeChangeset.folders), 0)).where(
            ClaudeChangeset.connection_id == connection_id,
            ClaudeChangeset.kind == "write",
            ClaudeChangeset.created_at >= day_start,
        )
    )
    if folders_so_far >= limits.FOLDERS_PER_DAY:
        raise Refused("cap_folders_day")


async def _check_day_move_cap(session: AsyncSession, clock: Clock, connection_id: int) -> None:
    """Rev. 3's MOVES_PER_DAY, same shape as `_check_day_folder_cap`."""
    day_start = await _local_day_start(session, clock)
    moves_so_far = await session.scalar(
        select(func.coalesce(func.sum(ClaudeChangeset.moves), 0)).where(
            ClaudeChangeset.connection_id == connection_id,
            ClaudeChangeset.kind == "write",
            ClaudeChangeset.created_at >= day_start,
        )
    )
    if moves_so_far >= limits.MOVES_PER_DAY:
        raise Refused("cap_moves_day")


async def _bump_refused(session: AsyncSession, row: ClaudeChangeset | None) -> None:
    if row is not None:
        row.refused += 1
        await session.commit()


def _log(tool: str, connection_id: int, *, outcome: str, reason: str | None = None, bytes_: int | None = None) -> None:
    logger.info(
        "claude_write",
        extra={
            "event": "claude_write",
            "tool": tool,
            "connection_id": connection_id,
            "outcome": outcome,
            "reason": reason,
            "bytes": bytes_,
        },
    )


def _map_vault_error(exc: VaultError) -> str:
    return {"refused": "vaultd_refused", "not_found": "vaultd_missing", "conflict": "vaultd_conflict"}.get(
        exc.code, "vaultd_error"
    )


# --- the four content-touching tools ---------------------------------------


async def update_note(
    session: AsyncSession,
    clock: Clock,
    client: VaultClient,
    connection_id: int,
    path: str,
    new_body: str,
    base_hash: str,
) -> dict:
    body_bytes = new_body.encode("utf-8")
    row = await _open_changeset(session, clock, connection_id)
    try:
        if len(body_bytes) > limits.BYTES_PER_FILE:
            raise Refused("cap_bytes_file")
        _check_content(new_body)
        await _check_day_caps(session, clock, connection_id, new_bytes=len(body_bytes), is_create=False)
        if row.files >= limits.FILES_PER_CHANGESET:
            raise Refused("cap_files")
        try:
            result = await client.put_knowledge(path, new_body, base_hash, row.vault_ref)
        except VaultError as exc:
            raise Refused(_map_vault_error(exc)) from exc
    except Refused as exc:
        await _bump_refused(session, row)
        _log("update_note", connection_id, outcome="refused", reason=exc.code)
        raise
    row.files += 1
    row.bytes += len(body_bytes)
    row.last_write_at = clock.now_utc()
    await session.commit()
    _log("update_note", connection_id, outcome="ok", bytes_=len(body_bytes))
    return {"path": path, "hash": result.sha256, "changeset_id": row.id}


async def create_note(
    session: AsyncSession,
    clock: Clock,
    client: VaultClient,
    connection_id: int,
    folder: str,
    title: str,
    body: str,
) -> dict:
    row = await _open_changeset(session, clock, connection_id)
    try:
        filename = sanitize_title(title)
        path = f"{sanitize_folder(folder)}/{filename}"
        body_bytes = body.encode("utf-8")
        if len(body_bytes) > limits.BYTES_PER_FILE:
            raise Refused("cap_bytes_file")
        _check_content(body)
        await _check_day_caps(session, clock, connection_id, new_bytes=len(body_bytes), is_create=True)
        if row.files >= limits.FILES_PER_CHANGESET:
            raise Refused("cap_files")
        if row.folders >= limits.FOLDERS_PER_CHANGESET:
            raise Refused("cap_folders")
        await _check_day_folder_cap(session, clock, connection_id)
        try:
            result = await client.put_knowledge(path, body, None, row.vault_ref)
        except VaultError as exc:
            raise Refused(_map_vault_error(exc)) from exc
    except Refused as exc:
        await _bump_refused(session, row)
        _log("create_note", connection_id, outcome="refused", reason=exc.code)
        raise
    row.files += 1
    row.created += 1
    row.folders += result.folders_created
    row.bytes += len(body_bytes)
    row.last_write_at = clock.now_utc()
    await session.commit()
    _log("create_note", connection_id, outcome="ok", bytes_=len(body_bytes))
    return {"path": path, "hash": result.sha256, "changeset_id": row.id}


async def rename_note(
    session: AsyncSession,
    clock: Clock,
    client: VaultClient,
    connection_id: int,
    path: str,
    new_path: str,
    base_hash: str,
) -> dict:
    row = await _open_changeset(session, clock, connection_id)
    try:
        # Rev. 3: a rename spends its own budget (moves), never the
        # content-write one (files) -- the moved note plus its
        # rewritten backlinks count against MOVE_FILES_PER_CHANGESET/
        # MOVES_PER_DAY, enforced authoritatively by vaultd itself
        # (app/core/claude_write_limits.py's own docstring). This is a
        # fast local rejection once the ledger already shows the
        # per-changeset budget spent; it is not an exact precheck of
        # this call's own file count, which the bot cannot know before
        # vaultd resolves the backlinks.
        if row.moves >= limits.MOVE_FILES_PER_CHANGESET:
            raise Refused("cap_moves")
        if row.folders >= limits.FOLDERS_PER_CHANGESET:
            raise Refused("cap_folders")
        await _check_day_move_cap(session, clock, connection_id)
        await _check_day_folder_cap(session, clock, connection_id)
        try:
            result = await client.rename_knowledge(path, new_path, base_hash, row.vault_ref)
        except VaultError as exc:
            raise Refused(_map_vault_error(exc)) from exc
    except Refused as exc:
        await _bump_refused(session, row)
        _log("rename_note", connection_id, outcome="refused", reason=exc.code)
        raise
    row.moves += result.files_moved
    row.folders += result.folders_created
    row.renamed += 1
    row.last_write_at = clock.now_utc()
    await session.commit()
    _log("rename_note", connection_id, outcome="ok")
    return {"path": result.path, "hash": result.sha256, "changeset_id": row.id, "relinked": result.relinked}


# --- read-only, still behind the write switch -------------------------------


async def get_note(client: VaultClient, path: str) -> dict:
    try:
        content = await client.get_knowledge(path)
    except VaultError as exc:
        raise Refused(_map_vault_error(exc)) from exc
    return {"path": content.path, "hash": content.sha256, "body": content.content}


async def list_tree(client: VaultClient) -> dict:
    """`list_tree()` (rev. 3, plan section 14, BUILD item 6): knowledge
    folders and note titles, no body text -- gated by the write switch,
    like `list_changes`, and Claude-only (never Grok's route)."""
    try:
        tree = await client.knowledge_tree()
    except VaultError as exc:
        raise Refused(_map_vault_error(exc)) from exc
    return {
        "folders": tree.folders,
        "notes": [{"path": n.path, "title": n.title} for n in tree.notes],
        "truncated": tree.truncated,
    }


async def _fetch_changes(client: VaultClient) -> list[ChangeEntry]:
    try:
        return await client.list_changes()
    except VaultError as exc:
        raise Refused(_map_vault_error(exc)) from exc


async def list_changes(session: AsyncSession, connection_id: int, client: VaultClient) -> dict:
    rows = (
        await session.execute(
            select(ClaudeChangeset)
            .where(ClaudeChangeset.connection_id == connection_id, ClaudeChangeset.kind == "write")
            .order_by(ClaudeChangeset.created_at.desc())
            .limit(20)
        )
    ).scalars().all()
    if not rows:
        return {"changes": []}
    vault_index = {c.id: c for c in await _fetch_changes(client)}
    out = []
    for row in rows:
        entry = vault_index.get(row.vault_ref)
        titles = [f.path.rsplit("/", 1)[-1].removesuffix(".md") for f in entry.files] if entry else []
        out.append(
            {
                "id": row.id,
                "time": row.created_at.isoformat(),
                "titles": titles,
                "undone": row.undone_at is not None,
            }
        )
    return {"changes": out}


async def changesets_between(
    session: AsyncSession, start: datetime.datetime, end: datetime.datetime
) -> list[ClaudeChangeset]:
    """Every write changeset (any connection) started in `[start, end)`,
    oldest first -- what the daily digest (app/tg/claude.py) sums and
    lists titles for."""
    rows = (
        await session.execute(
            select(ClaudeChangeset)
            .where(
                ClaudeChangeset.kind == "write",
                ClaudeChangeset.created_at >= start,
                ClaudeChangeset.created_at < end,
            )
            .order_by(ClaudeChangeset.created_at, ClaudeChangeset.id)
        )
    ).scalars().all()
    return list(rows)


async def undoable_changesets(
    session: AsyncSession, connection_id: int, *, since: datetime.datetime | None = None, limit: int | None = None
) -> list[ClaudeChangeset]:
    """Write changesets not yet undone, newest first -- what `/claude
    undo`/`undo all` and the digest's callback (app/tg/claude.py) walk."""
    query = select(ClaudeChangeset).where(
        ClaudeChangeset.connection_id == connection_id,
        ClaudeChangeset.kind == "write",
        ClaudeChangeset.undone_at.is_(None),
    )
    if since is not None:
        query = query.where(ClaudeChangeset.created_at >= since)
    query = query.order_by(ClaudeChangeset.created_at.desc(), ClaudeChangeset.id.desc())
    if limit is not None:
        query = query.limit(limit)
    return list((await session.execute(query)).scalars().all())


# --- undo (plan section 6.2; works with the write switch off) --------------


async def undo_changeset(
    session: AsyncSession, clock: Clock, client: VaultClient, connection_id: int, changeset_id: int
) -> dict:
    row = await session.get(ClaudeChangeset, changeset_id)
    if row is None or row.connection_id != connection_id or row.kind != "write" or row.undone_at is not None:
        raise Refused("no_such_changeset")
    count = await session.scalar(
        select(func.count())
        .select_from(ClaudeChangeset)
        .where(
            ClaudeChangeset.connection_id == connection_id,
            ClaudeChangeset.kind == "undo",
            ClaudeChangeset.created_at >= clock.now_utc() - datetime.timedelta(hours=1),
        )
    )
    if count >= limits.UNDOS_PER_HOUR:
        raise Refused("cap_undos")
    try:
        result = await client.undo_changeset(row.vault_ref)
    except VaultError as exc:
        raise Refused(_map_vault_error(exc)) from exc
    if result.restored > 0:
        now = clock.now_utc()
        row.undone_at = now
        session.add(
            ClaudeChangeset(
                connection_id=connection_id,
                vault_ref=row.vault_ref,
                kind="undo",
                files=result.restored,
                bytes=0,
                refused=result.refused,
                created_at=now,
                last_write_at=now,
            )
        )
    await session.commit()
    _log("undo_changeset", connection_id, outcome="ok")
    return {"restored": result.restored, "refused": result.refused}

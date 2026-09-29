"""The only code that touches the lens tables (anchor-lens-plan.md sections 5 and 11).

The lens is the set of knowledge notes the user chose as the frame for
Echo's self-improvement: `anchor: lens` on the note, or a
`lens_folders` rule in `Anchor/settings.md`. Membership is always the
user's -- vaultd reports it in the manifest, and nothing here decides
it. In L1 nothing in Echo reads the lens yet; Claude Code does, through
the `anchor_lens` role and the `lens` schema's two functions
(migration e4c7a2d9b1f3), which record each call in `lens_read`. A
caller that rolls its transaction back takes that row with it, but not
the id the call took first from the table's sequence: the gap it leaves
is what `unrecorded_reads` and `unrecorded_between` count.

This module owns four tables: `lens_note` (each lens note, whole),
`note_link` (the wikilinks out of knowledge and lens notes, from
vaultd's graph), `lens_version` (one row per distinct state of the
lens) and `lens_read` (the functions' own log). It also flips the
role's LOGIN for `/lens code on|off`.

**Who may import this module** is pinned by
tests/test_vault_notes_isolation.py: the sync pass (app/vault/sync.py)
writes, /lens (app/tg/lens.py) reads counts and flips the switch, and
the scheduler (app/core/scheduler.py) asks whether the door is open or
was read through, to queue the daily digest. Nothing else, and in particular no idle kind, research job, web route
or grant -- L2 onwards each adds itself there by one line, justified
against the plan's section 10.

**Consent is checked here too**, as app/vault/_chunks.py does for the
chunk tables: `store` refuses to write while `user_state.notes_consent`
is off. The sync pass checks consent and both flags first; this is the
floor under it.

Nothing here commits except `set_code_access`, whose ALTER ROLE is its
own transaction. Logs carry codes only -- never a title, a path or text.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
from collections.abc import Collection, Iterable
from dataclasses import dataclass

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import LensNote, LensRead, LensVersion, NoteLink, UserState
from app.vault._chunks import NotesConsentOff

__all__ = [
    "LENS_ROLE",
    "CodeAccess",
    "Link",
    "NotesConsentOff",
    "Stored",
    "code_access",
    "counts",
    "delete_all",
    "delete_for_file",
    "delete_links",
    "reads_between",
    "unrecorded_between",
    "unrecorded_reads",
    "record_version",
    "replace_links",
    "set_code_access",
    "store",
    "stored",
    "update_meta",
    "version_hash",
]

logger = logging.getLogger(__name__)

# Created NOLOGIN by migration e4c7a2d9b1f3, exactly as anchor_debug is.
LENS_ROLE = "anchor_lens"


@dataclass(frozen=True)
class Stored:
    """What the sync pass needs to know about a stored lens note to
    decide whether to fetch it again. No body."""

    kind: str
    title: str
    summary: str | None
    body_hash: str


@dataclass(frozen=True)
class Link:
    """One `note_link` row, by vault_file id. Exactly one target."""

    src_file_id: int
    dst_file_id: int | None = None
    unresolved_text: str | None = None
    outside: bool = False


async def _consented(session: AsyncSession) -> bool:
    return bool(
        (
            await session.execute(select(UserState.notes_consent).where(UserState.id == 1))
        ).scalar_one_or_none()
    )


# --- lens notes ----------------------------------------------------------------


async def stored(session: AsyncSession) -> dict[int, Stored]:
    """Every stored lens note, by its vault_file id."""
    rows = await session.execute(
        select(LensNote.vault_file_id, LensNote.kind, LensNote.title, LensNote.summary, LensNote.body_hash)
    )
    return {
        file_id: Stored(kind=kind, title=title, summary=summary, body_hash=body_hash)
        for file_id, kind, title, summary, body_hash in rows
    }


def _hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


async def store(
    session: AsyncSession,
    file_id: int,
    *,
    kind: str,
    title: str,
    summary: str | None,
    body: str,
    now: datetime.datetime,
) -> bool:
    """Insert or replace one lens note, whole. True if anything changed.
    Refuses without notes consent."""
    if not await _consented(session):
        raise NotesConsentOff
    body_hash = _hash(body)
    current = (
        await session.execute(select(LensNote).where(LensNote.vault_file_id == file_id))
    ).scalar_one_or_none()
    if current is None:
        session.add(
            LensNote(
                vault_file_id=file_id,
                kind=kind,
                title=title,
                summary=summary,
                body=body,
                body_hash=body_hash,
                chars=len(body),
                updated_at=now,
            )
        )
        await session.flush()
        return True
    if (current.kind, current.title, current.summary, current.body_hash) == (
        kind,
        title,
        summary,
        body_hash,
    ):
        return False
    current.kind, current.title, current.summary = kind, title, summary
    current.body, current.body_hash, current.chars = body, body_hash, len(body)
    current.updated_at = now
    await session.flush()
    return True


async def update_meta(
    session: AsyncSession,
    file_id: int,
    *,
    kind: str,
    title: str,
    summary: str | None,
    now: datetime.datetime,
) -> bool:
    """Kind, title and summary change without the file changing (a
    `lens_person_folders` edit, a frontmatter-only summary the graph
    reports): no fetch needed. True if anything changed."""
    current = (
        await session.execute(select(LensNote).where(LensNote.vault_file_id == file_id))
    ).scalar_one_or_none()
    if current is None or (current.kind, current.title, current.summary) == (kind, title, summary):
        return False
    current.kind, current.title, current.summary = kind, title, summary
    current.updated_at = now
    await session.flush()
    return True


async def delete_for_file(session: AsyncSession, file_id: int) -> int:
    result = await session.execute(delete(LensNote).where(LensNote.vault_file_id == file_id))
    return result.rowcount or 0


async def delete_all(session: AsyncSession) -> int:
    result = await session.execute(delete(LensNote))
    return result.rowcount or 0


async def counts(session: AsyncSession) -> dict[str, int]:
    """Stored lens notes per kind: {"person": n, "concept": m}."""
    rows = await session.execute(select(LensNote.kind, func.count()).group_by(LensNote.kind))
    out = {"person": 0, "concept": 0}
    out.update({kind: n for kind, n in rows})
    return out


# --- links ---------------------------------------------------------------------


def _key(link: Link) -> tuple:
    return (link.src_file_id, link.dst_file_id or 0, link.unresolved_text or "", link.outside)


async def replace_links(
    session: AsyncSession, links: Iterable[Link], *, keep: Collection[int] = ()
) -> bool:
    """Make `note_link` exactly `links` (as a multiset), plus every
    existing row that starts or ends at a file in `keep` (notes a
    truncated graph did not report). Rewrites only when something
    differs, so an unchanged vault costs one SELECT a pass. True if it
    rewrote."""
    current = sorted(
        (src, dst or 0, unresolved or "", outside)
        for src, dst, unresolved, outside in await session.execute(
            select(NoteLink.src_file_id, NoteLink.dst_file_id, NoteLink.unresolved_text, NoteLink.outside)
        )
    )
    kept = [row for row in current if row[0] in keep or (row[1] and row[1] in keep)]
    wanted = sorted([_key(link) for link in links] + kept)
    if wanted == current:
        return False
    await session.execute(delete(NoteLink))
    if wanted:
        await session.execute(
            insert(NoteLink),
            [
                {
                    "src_file_id": src,
                    "dst_file_id": dst or None,
                    "unresolved_text": unresolved or None,
                    "outside": outside,
                }
                for src, dst, unresolved, outside in wanted
            ],
        )
    await session.flush()
    return True


async def delete_links(session: AsyncSession) -> int:
    result = await session.execute(delete(NoteLink))
    return result.rowcount or 0


# --- versions --------------------------------------------------------------------


def version_hash(body_hashes: Iterable[str], edges: Iterable[tuple[str, str]]) -> str:
    """sha256 over the sorted body hashes and the sorted lens-to-lens
    edges (src title, dst title): the same lens in any order hashes the
    same (plan section 5)."""
    payload = json.dumps(
        {"notes": sorted(body_hashes), "edges": sorted([list(edge) for edge in edges])},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def record_version(session: AsyncSession) -> bool:
    """A `lens_version` row for the lens as it is now, unless one with
    the same hash already exists. True if a row was added."""
    body_hashes = list((await session.execute(select(LensNote.body_hash))).scalars())
    src, dst = aliased(LensNote), aliased(LensNote)
    edges = [
        (s, d)
        for s, d in await session.execute(
            select(src.title, dst.title)
            .select_from(NoteLink)
            .join(src, src.vault_file_id == NoteLink.src_file_id)
            .join(dst, dst.vault_file_id == NoteLink.dst_file_id)
        )
    ]
    stmt = (
        pg_insert(LensVersion)
        .values(hash=version_hash(body_hashes, edges), note_count=len(body_hashes))
        .on_conflict_do_nothing(index_elements=[LensVersion.hash])
        .returning(LensVersion.id)
    )
    return (await session.execute(stmt)).first() is not None


# --- reads -----------------------------------------------------------------------


async def reads_between(
    session: AsyncSession, start: datetime.datetime, end: datetime.datetime
) -> int:
    """How many times the `lens` functions were called in [start, end)."""
    return (
        await session.execute(
            select(func.count()).select_from(LensRead).where(LensRead.at >= start, LensRead.at < end)
        )
    ).scalar_one()


# A read whose `lens_read` row was rolled back still used its id (the
# functions take it first, from a sequence, which never rolls back), so
# `lens_read.id` has a gap for it. /delete's RESTART IDENTITY keeps the
# ids dense otherwise; a Postgres crash can also make a sequence skip.
_UNRECORDED_ALL = text(
    "select coalesce(pg_sequence_last_value("
    "pg_get_serial_sequence('public.lens_read', 'id')::regclass), 0)"
    " - (select count(*) from lens_read)"
)
_UNRECORDED_BETWEEN = text(
    "select case when w.k = 0 then 0 else greatest(w.m - p.prev - w.k, 0) end"
    " from (select count(*) as k, max(id) as m from lens_read"
    "       where at >= :start and at < :end) as w,"
    "      (select coalesce(max(id), 0) as prev from lens_read where at < :start) as p"
)


async def unrecorded_reads(session: AsyncSession) -> int | None:
    """Calls of the `lens` functions with no `lens_read` row -- rolled
    back by the caller -- since the table was last emptied. None when
    the bot's database user may not read the sequence."""
    try:
        async with session.begin_nested():
            return max(int((await session.execute(_UNRECORDED_ALL)).scalar_one()), 0)
    except DBAPIError as exc:
        logger.warning("lens unrecorded reads unknown", extra={"error_code": _sqlstate(exc)})
        return None


async def unrecorded_between(
    session: AsyncSession, start: datetime.datetime, end: datetime.datetime
) -> int:
    """Gaps in `lens_read.id` that end at a row recorded in [start, end):
    unrecorded calls made before a recorded one. A gap after the last
    recorded call has no row to date it by; `unrecorded_reads` still
    counts it. Each gap is counted in exactly one window."""
    return int(
        (await session.execute(_UNRECORDED_BETWEEN, {"start": start, "end": end})).scalar_one()
    )


# --- /lens code: the role's LOGIN ---------------------------------------------------


@dataclass(frozen=True)
class CodeAccess:
    """The outcome of `/lens code on|off`.

    `state` is `ok`, `missing` (no `anchor_lens` role in this cluster),
    `denied` (the bot's database user may not alter it: sqlstate 42501)
    or `error` (any other database error -- a lock timeout, a dropped
    connection -- where trying again may work).
    `terminate_failed` is set when `off` closed LOGIN but could not end
    the sessions already open.
    """

    state: str
    terminated: int = 0
    terminate_failed: bool = False


_INSUFFICIENT_PRIVILEGE = "42501"


def _sqlstate(exc: DBAPIError) -> str:
    orig = exc.orig
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return str(code) if code else "db_error"


async def code_access(session: AsyncSession) -> bool | None:
    """Whether `anchor_lens` may log in; None when the role does not exist."""
    return (
        await session.execute(
            text("select rolcanlogin from pg_roles where rolname = :role"), {"role": LENS_ROLE}
        )
    ).scalar_one_or_none()


async def set_code_access(session: AsyncSession, on: bool) -> CodeAccess:
    """`ALTER ROLE anchor_lens LOGIN|NOLOGIN`; `off` also ends the role's
    open sessions (plan section 11). Commits. Needs CREATEROLE (and, on
    Postgres 16+, ADMIN on the role -- the role's creator has it); a
    refusal is reported, never raised: the user does it by hand."""
    if await code_access(session) is None:
        return CodeAccess("missing")
    try:
        await session.execute(text(f"ALTER ROLE {LENS_ROLE} {'LOGIN' if on else 'NOLOGIN'}"))
        await session.commit()
    except DBAPIError as exc:
        await session.rollback()
        code = _sqlstate(exc)
        logger.warning("lens code access not changed", extra={"error_code": code})
        return CodeAccess("denied" if code == _INSUFFICIENT_PRIVILEGE else "error")
    if on:
        return CodeAccess("ok")
    try:
        terminated = (
            await session.execute(
                text(
                    "select count(*) filter (where pg_terminate_backend(pid)) "
                    "from pg_stat_activity where usename = :role"
                ),
                {"role": LENS_ROLE},
            )
        ).scalar_one()
        await session.commit()
    except DBAPIError as exc:
        await session.rollback()
        logger.warning("lens sessions not terminated", extra={"error_code": _sqlstate(exc)})
        return CodeAccess("ok", terminate_failed=True)
    return CodeAccess("ok", terminated=terminated)

"""The only code that touches the lens tables (anchor-lens-plan.md sections 5 and 11).

The lens is the set of knowledge notes the user chose as the frame for
Echo's self-improvement: `anchor: lens` on the note, or a
`lens_folders` rule in `Anchor/settings.md`. Membership is always the
user's -- vaultd reports it in the manifest, and nothing here decides
it. From L2 the weekly review reads it (app/core/lens_review.py, through
this module), and Claude Code does, through the `anchor_lens` role and
the `lens` schema's functions -- `notes()` and `graph()` (migration
e4c7a2d9b1f3), `rounds(n)` (c6d2e8a4f917) -- which record each call in
`lens_read`. A
caller that rolls its transaction back takes that row with it, but not
the id the call took first from the table's sequence: the gap it leaves
is what `unrecorded_reads` and `unrecorded_between` count.

This module owns five tables: `lens_note` (each lens note, whole),
`note_link` (the wikilinks out of knowledge and lens notes, from
vaultd's graph), `lens_version` (one row per distinct state of the
lens), `lens_read` (the functions' own log) and, from L2, `lens_round`
(one row per round of self-selection, plan section 7). It also flips
the role's LOGIN for `/lens code on|off`.

**L2: what the weekly review reads.** The selector and the grounding
call live in app/core/lens_review.py; everything they need from the
tables is here, so that module names none of them:
`lens_active` (is there a lens to use this round), `catalog` (one
entry per note: title, kind, summary or the start of the body, its
lens-to-lens links both ways, and how many rounds since it was last
picked), `bodies` (the picked notes, whole), `record_round` and
`attach_round_to_review` (the `lens_round` row), and, for the card and
/lens, `titles_for`, `round_rationale` and `last_round`. The catalog
and the bodies are lens notes only: a knowledge-only note never
reaches the review, not even as a linked title (plan section 10).

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

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.config import Settings
from app.db.models import LensNote, LensRead, LensRound, LensVersion, NoteLink, UserState
from app.vault._chunks import NotesConsentOff

__all__ = [
    "LENS_ROLE",
    "ROUND_CONSUMERS",
    "ROUND_OUTCOMES",
    "SUMMARY_FALLBACK_CHARS",
    "Body",
    "CatalogEntry",
    "CodeAccess",
    "LastRound",
    "Link",
    "NotesConsentOff",
    "Stored",
    "attach_round_to_review",
    "bodies",
    "catalog",
    "code_access",
    "counts",
    "delete_all",
    "delete_for_file",
    "delete_links",
    "last_round",
    "lens_active",
    "note_count",
    "record_round",
    "round_rationale",
    "titles_for",
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


async def _current_version(session: AsyncSession) -> tuple[str, int]:
    """The lens as it is now: its `version_hash` and its note count."""
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
    return version_hash(body_hashes, edges), len(body_hashes)


async def record_version(session: AsyncSession) -> bool:
    """A `lens_version` row for the lens as it is now, unless one with
    the same hash already exists. True if a row was added."""
    current, note_count = await _current_version(session)
    stmt = (
        pg_insert(LensVersion)
        .values(hash=current, note_count=note_count)
        .on_conflict_do_nothing(index_elements=[LensVersion.hash])
        .returning(LensVersion.id)
    )
    return (await session.execute(stmt)).first() is not None


# --- L2: the weekly review's round (plan section 7) ------------------------------

# ck_lens_round_consumer and ck_lens_round_outcome (migration c6d2e8a4f917).
ROUND_CONSUMERS = ("review",)
ROUND_OUTCOMES = ("grounded", "empty", "fallback")

# Plan section 7: a note without a frontmatter `summary` is described
# in the catalog by the start of its body, this long.
SUMMARY_FALLBACK_CHARS = 300


@dataclass(frozen=True)
class CatalogEntry:
    """One line of the selector's catalog (plan section 7).

    `summary` is the note's frontmatter summary, or else the first
    `SUMMARY_FALLBACK_CHARS` characters of its body, whitespace
    collapsed either way. `links` are the titles of the other lens notes
    this one links to or is linked from, sorted, never a knowledge-only
    note's. `rounds_since_used` counts the review rounds recorded since
    the note was last selected (0: picked in the latest round); None
    when it never was -- the prompt says «никогда».
    """

    id: int
    kind: str
    title: str
    summary: str
    links: tuple[str, ...]
    rounds_since_used: int | None


@dataclass(frozen=True)
class Body:
    """One selected note, whole, for the grounding call's lens block."""

    id: int
    title: str
    body: str
    chars: int


@dataclass(frozen=True)
class LastRound:
    """The newest round, for /lens: when and how it ended. No text."""

    id: int
    consumer: str
    outcome: str
    created_at: datetime.datetime


def _collapse(value: str) -> str:
    return " ".join(value.split())


async def note_count(session: AsyncSession) -> int:
    """How many lens notes are stored."""
    return (await session.execute(select(func.count()).select_from(LensNote))).scalar_one()


async def lens_active(session: AsyncSession, settings: Settings) -> bool:
    """Whether this round uses the lens at all: LENS_ENABLED, and between
    1 and LENS_CATALOG_MAX_NOTES notes stored (plan section 7: over the
    cap the lens is not used rather than silently cut). False means the
    review runs exactly as it did before L2. The sync pass keeps rows
    only under notes consent and VAULT_KNOWLEDGE_ENABLED too, and
    deletes them when either goes off."""
    if not settings.LENS_ENABLED:
        return False
    return 1 <= await note_count(session) <= settings.LENS_CATALOG_MAX_NOTES


async def _rounds_since_used(session: AsyncSession) -> dict[int, int]:
    """note id -> review rounds recorded after the latest one that selected it."""
    rows = await session.execute(
        text(
            "with picked as ("
            "  select u.note_id, max(r.id) as last_id"
            "  from lens_round as r, unnest(r.selected_note_ids) as u(note_id)"
            "  where r.consumer = 'review'"
            "  group by u.note_id"
            ")"
            " select p.note_id,"
            "        (select count(*) from lens_round as later"
            "         where later.consumer = 'review' and later.id > p.last_id)"
            " from picked as p"
        )
    )
    return {int(note_id): int(since) for note_id, since in rows}


async def catalog(session: AsyncSession) -> list[CatalogEntry]:
    """Every lens note as a catalog entry, ordered by title (then id)."""
    notes = (
        await session.execute(
            select(
                LensNote.id,
                LensNote.vault_file_id,
                LensNote.kind,
                LensNote.title,
                LensNote.summary,
                func.left(LensNote.body, SUMMARY_FALLBACK_CHARS * 4),
            ).order_by(LensNote.title, LensNote.id)
        )
    ).all()
    by_file = {file_id: title for _id, file_id, _kind, title, _summary, _head in notes}
    links: dict[int, set[str]] = {file_id: set() for file_id in by_file}
    for src, dst in await session.execute(
        select(NoteLink.src_file_id, NoteLink.dst_file_id).where(
            NoteLink.src_file_id.in_(by_file), NoteLink.dst_file_id.in_(by_file)
        )
    ):
        if src == dst:
            continue
        links[src].add(by_file[dst])
        links[dst].add(by_file[src])
    since = await _rounds_since_used(session)
    entries = []
    for note_id, file_id, kind, title, summary, head in notes:
        described = _collapse(summary or "") or _collapse(head)[:SUMMARY_FALLBACK_CHARS]
        entries.append(
            CatalogEntry(
                id=note_id,
                kind=kind,
                title=title,
                summary=described,
                links=tuple(sorted(links[file_id])),
                rounds_since_used=since.get(note_id),
            )
        )
    return entries


async def bodies(session: AsyncSession, ids: Iterable[int]) -> list[Body]:
    """The notes with these ids, whole, in the order given; an id that is
    not (or no longer) a lens note is skipped, a repeated one kept once.
    The character budget is the caller's (LENS_ROUND_MAX_CHARS)."""
    wanted = list(dict.fromkeys(ids))
    if not wanted:
        return []
    rows = {
        note_id: Body(id=note_id, title=title, body=body, chars=chars)
        for note_id, title, body, chars in await session.execute(
            select(LensNote.id, LensNote.title, LensNote.body, LensNote.chars).where(
                LensNote.id.in_(wanted)
            )
        )
    }
    return [rows[note_id] for note_id in wanted if note_id in rows]


async def titles_for(session: AsyncSession, ids: Iterable[int] | None) -> list[str]:
    """The current titles of these lens notes, in the order given; ids
    no longer in the lens are skipped, a repeated one kept once (the
    card's «основание»)."""
    wanted = list(dict.fromkeys(ids or ()))
    if not wanted:
        return []
    rows = dict(
        (
            await session.execute(
                select(LensNote.id, LensNote.title).where(LensNote.id.in_(wanted))
            )
        ).all()
    )
    return [rows[note_id] for note_id in wanted if note_id in rows]


async def record_round(
    session: AsyncSession,
    *,
    selected_note_ids: Iterable[int],
    rationale: str | None,
    outcome: str,
    consumer: str = "review",
    weekly_review_id: int | None = None,
) -> int:
    """Insert one `lens_round` row against the `lens_version` of the lens
    as it is now -- looked up by its hash, not by recency, since a lens
    that returns to an earlier state reuses that state's row (plan
    section 7: the round records the version it ran against; no such
    row yet: null) -- and return its id. `selected_note_ids` are stored as
    given (the caller has validated them against the catalog); an empty
    selection is a round too. Flushes, never commits."""
    if consumer not in ROUND_CONSUMERS:
        raise ValueError(f"unknown lens round consumer: {consumer!r}")
    if outcome not in ROUND_OUTCOMES:
        raise ValueError(f"unknown lens round outcome: {outcome!r}")
    current, _count = await _current_version(session)
    version_id = (
        await session.execute(select(LensVersion.id).where(LensVersion.hash == current))
    ).scalar_one_or_none()
    row = LensRound(
        consumer=consumer,
        weekly_review_id=weekly_review_id,
        lens_version_id=version_id,
        selected_note_ids=[int(i) for i in selected_note_ids],
        rationale=rationale,
        outcome=outcome,
    )
    session.add(row)
    await session.flush()
    logger.info(
        "lens round recorded",
        extra={"lens_round_id": row.id, "event": outcome, "count": len(row.selected_note_ids)},
    )
    return row.id


async def attach_round_to_review(session: AsyncSession, round_id: int, review_id: int) -> None:
    """Point a round at the `weekly_review` row it served, once that row
    exists (the round runs inside the analysis, before the review is
    stored). Flushes, never commits."""
    await session.execute(
        update(LensRound).where(LensRound.id == round_id).values(weekly_review_id=review_id)
    )
    await session.flush()


async def round_rationale(session: AsyncSession, round_id: int) -> str | None:
    """The selector's `why` for one round («почему эти заметки?»); None
    when the round is gone or had none."""
    return (
        await session.execute(select(LensRound.rationale).where(LensRound.id == round_id))
    ).scalar_one_or_none()


async def last_round(session: AsyncSession) -> LastRound | None:
    """The newest round of any consumer, or None before the first."""
    row = (
        await session.execute(
            select(LensRound.id, LensRound.consumer, LensRound.outcome, LensRound.created_at)
            .order_by(LensRound.created_at.desc(), LensRound.id.desc())
            .limit(1)
        )
    ).first()
    return None if row is None else LastRound(*row)


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

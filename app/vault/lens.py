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

This module owns seven tables: `lens_note` (each lens note, whole),
`note_link` (the wikilinks out of knowledge and lens notes, from
vaultd's graph), `lens_version` (one row per distinct state of the
lens), `lens_read` (the functions' own log), from L2 `lens_round`
(one row per round of self-selection, plan section 7), and from L3
`lens_garden_run` and `lens_gap` (the weekly garden, plan section 8).
It also flips the role's LOGIN for `/lens code on|off`.

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

**L3: the garden** (plan section 8; the L3 spec, with the owner's
amendments). A weekly idle job (app/core/idle/lens_garden.py) reads the
lens as a graph (`garden_facts` for its gate, `garden_view` for the
deterministic step, `known_gaps` for dedup and recheck) and writes one
run and its gaps in one transaction (`record_garden`). The Telegram
side (app/tg/garden.py) sends ONE message per run -- `unsent_run`, then
`mark_run_sent` -- and a tap on its keyboard goes through `decide_gap`,
after which `run_message_state` re-renders the same message. The sync
pass renders the run as a note in `Anchor/Reports` (`report_data`,
`report_path`), /lens shows `garden_status`, and the garden dies with
the lens (`delete_garden`, wherever `lens_note` is emptied). What the
garden reads is lens-only, plus knowledge notes as anonymous file ids
and, for local checks only, their titles (`GardenView`'s docstring):
no knowledge title may reach a model or Claude Code.

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
import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass

from sqlalchemy import delete, func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.config import Settings
from app.db.models import (
    LensGap,
    LensGardenRun,
    LensNote,
    LensRead,
    LensRound,
    LensVersion,
    NoteLink,
    UserState,
    VaultFile,
)
from app.vault._chunks import NotesConsentOff

__all__ = [
    "GAP_DECISIONS",
    "GAP_DETAIL_MAX",
    "GAP_KINDS",
    "GAP_STATUSES",
    "GAP_TITLE_MAX",
    "LENS_ROLE",
    "REPORT_DIR",
    "ROUND_CONSUMERS",
    "ROUND_OUTCOMES",
    "SUMMARY_FALLBACK_CHARS",
    "Body",
    "CatalogEntry",
    "CodeAccess",
    "GardenFacts",
    "GardenMessage",
    "GardenNote",
    "GardenRecord",
    "GardenStatus",
    "GardenView",
    "KnownGap",
    "LastRound",
    "Link",
    "MessageGap",
    "NewGap",
    "NotesConsentOff",
    "ReportData",
    "ReportGap",
    "Stored",
    "attach_round_to_review",
    "bodies",
    "catalog",
    "code_access",
    "counts",
    "decide_gap",
    "delete_all",
    "delete_for_file",
    "delete_garden",
    "delete_links",
    "garden_facts",
    "garden_status",
    "garden_view",
    "iso_week",
    "known_gaps",
    "last_round",
    "lens_active",
    "mark_run_sent",
    "message_run_id",
    "note_count",
    "record_garden",
    "report_data",
    "report_path",
    "run_message_state",
    "unsent_run",
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
    # L3: the frontmatter aliases, as last stored.
    aliases: tuple[str, ...] = ()


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
        select(
            LensNote.vault_file_id,
            LensNote.kind,
            LensNote.title,
            LensNote.summary,
            LensNote.body_hash,
            LensNote.aliases,
        )
    )
    return {
        file_id: Stored(
            kind=kind, title=title, summary=summary, body_hash=body_hash, aliases=tuple(aliases or ())
        )
        for file_id, kind, title, summary, body_hash, aliases in rows
    }


def _hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _aliases(values: Iterable[str]) -> list[str]:
    """Stripped, non-empty, each kept once, in the order given. Which
    aliases may be stored at all (`would_mask`) is the sync pass's call."""
    return list(dict.fromkeys(v.strip() for v in values if isinstance(v, str) and v.strip()))


async def store(
    session: AsyncSession,
    file_id: int,
    *,
    kind: str,
    title: str,
    summary: str | None,
    body: str,
    now: datetime.datetime,
    aliases: Iterable[str] | None = None,
) -> bool:
    """Insert or replace one lens note, whole. True if anything changed.
    Refuses without notes consent.

    `aliases` (L3) are the note's frontmatter aliases; None keeps the
    stored ones (a pass whose graph is missing or truncated says nothing
    about them), and a new note starts with none. An alias change alone
    counts as a change -- `updated_at` moves -- but is not part of the
    lens's version hash (`version_hash`)."""
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
                aliases=_aliases(aliases or ()),
                body=body,
                body_hash=body_hash,
                chars=len(body),
                updated_at=now,
            )
        )
        await session.flush()
        return True
    wanted = list(current.aliases or ()) if aliases is None else _aliases(aliases)
    if (current.kind, current.title, current.summary, current.body_hash, list(current.aliases or ())) == (
        kind,
        title,
        summary,
        body_hash,
        wanted,
    ):
        return False
    current.kind, current.title, current.summary = kind, title, summary
    current.body, current.body_hash, current.chars = body, body_hash, len(body)
    current.aliases = wanted
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
    aliases: Iterable[str] | None = None,
) -> bool:
    """Kind, title, summary and (L3) aliases change without the file
    changing (a `lens_person_folders` edit, frontmatter the graph
    reports): no fetch needed. `aliases=None` keeps the stored ones, as
    in `store`. True if anything changed."""
    current = (
        await session.execute(select(LensNote).where(LensNote.vault_file_id == file_id))
    ).scalar_one_or_none()
    if current is None:
        return False
    wanted = list(current.aliases or ()) if aliases is None else _aliases(aliases)
    if (current.kind, current.title, current.summary, list(current.aliases or ())) == (
        kind,
        title,
        summary,
        wanted,
    ):
        return False
    current.kind, current.title, current.summary = kind, title, summary
    current.aliases = wanted
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


# --- L3: the garden (plan section 8; the L3 spec) ------------------------------------

# ck_lens_gap_kind and ck_lens_gap_status (migration b3e9f5a1c7d2).
# `researched` is L4's; nothing in L3 sets it.
GAP_KINDS = ("link", "missing_note", "tension", "bridge")
GAP_STATUSES = ("open", "done", "dismissed", "resolved", "researched")
# What a tap may make of an open gap: «Сделал» and «Не нужно».
GAP_DECISIONS = ("done", "dismissed")
# ck_lens_gap_title_len and ck_lens_gap_detail_len.
GAP_TITLE_MAX = 80
GAP_DETAIL_MAX = 300
# vaultd's writable `Anchor/Reports` (L3 spec section 3).
REPORT_DIR = "Anchor/Reports"

_SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")
_ISO_WEEK_RE = re.compile(r"^[0-9]{4}-W[0-9]{2}$")


def iso_week(day: datetime.date) -> str:
    """The ISO week `day` falls in, as `lens_garden_run.iso_week` stores
    it: `2026-W40`. The caller passes the user's local date."""
    year, week, _weekday = day.isocalendar()
    return f"{year}-W{week:02d}"


def report_path(week: str, epoch: str) -> str:
    """The run's note: `Anchor/Reports/Lens garden 2026-W40-k3f7qa.md`.
    The vault epoch is in the name for the reason it is in every file
    Echo creates (app/vault/epoch.py): a pre-/delete copy re-uploaded by
    an offline device must never take the new file's path."""
    return f"{REPORT_DIR}/Lens garden {week}-{epoch}.md"


@dataclass(frozen=True)
class GardenFacts:
    """What the garden's gate needs (app/core/idle/lens_garden.py's
    `gate_facts`). No text.

    `version_id` is the `lens_version` row of the lens as it is now,
    found by its hash as `record_round` does (a lens back at an earlier
    state reuses that state's row, so "the newest row" would miss the
    change); None before the sync pass has recorded one. `done` counts
    the gaps the user marked done, which the next run must recheck."""

    notes: int
    last_run_at: datetime.datetime | None
    last_iso_week: str | None
    last_version_id: int | None
    version_id: int | None
    done: int


@dataclass(frozen=True)
class GardenNote:
    """One lens note as the garden's deterministic step sees it. The body
    is for mention matching and terms, in code only: it never reaches the
    model (L3 spec section 6)."""

    id: int
    file_id: int
    kind: str
    title: str
    aliases: tuple[str, ...]
    summary: str | None
    body: str
    updated_at: datetime.datetime


@dataclass(frozen=True)
class GardenView:
    """The lens as a graph, for step 1 (L3 spec section 5).

    - `notes`: every lens note, by title then id.
    - `edges`: every resolved link with at least one lens end, as
      (src file id, dst file id), each pair once, no self-links. A
      knowledge note appears here only as its file id: anonymous.
    - `outside`: lens file id -> links out of it to notes the bot may
      not see (counted, never named).
    - `unresolved`: (lens file id, target text) for links out of lens
      notes to no note at all, each pair once.
    - `knowledge_titles`: file id -> title (the file name) of every
      knowledge note that is not in the lens. For local checks only --
      a missing_note that already exists, a recheck -- and never for a
      prompt, a log, a gap's text or anything Claude Code can read.
    - `version_id`: as in `GardenFacts`.
    """

    notes: tuple[GardenNote, ...]
    edges: tuple[tuple[int, int], ...]
    outside: dict[int, int]
    unresolved: tuple[tuple[int, str], ...]
    knowledge_titles: dict[int, str]
    version_id: int | None


@dataclass(frozen=True)
class KnownGap:
    """A gap that is not resolved (open, done, dismissed or researched):
    what the next run dedups against, marks «уже предложено», and --
    open and done ones -- rechecks."""

    id: int
    garden_run_id: int
    kind: str
    note_ids: tuple[int, ...]
    titles: tuple[str, ...]
    title: str | None
    detail: str
    status: str
    signature: str
    recheck: dict
    reopened: int


@dataclass(frozen=True)
class NewGap:
    """One validated gap to insert. `signature` and `recheck` are
    app/core/lens_graph.py's (the L3 spec's section 7); `titles` are the
    notes' titles now, `title` the proposed note's (missing_note only)."""

    kind: str
    note_ids: tuple[int, ...]
    titles: tuple[str, ...]
    title: str | None
    detail: str
    signature: str
    recheck: dict


@dataclass(frozen=True)
class GardenRecord:
    """What `record_garden` did: the run's id, the ids of the gaps it
    inserted, how many proposed gaps an existing live signature
    swallowed, how many gaps it resolved and reopened, and how many
    never-sent open gaps it carried over from an earlier unsent run."""

    run_id: int
    new_ids: tuple[int, ...]
    deduped: int
    resolved: int
    reopened: int
    carried: int = 0


@dataclass(frozen=True)
class MessageGap:
    """One numbered item of a run's Telegram message.

    `status` is the gap's status now. `actionable` is whether this
    message's keyboard still has its row: it is open and its buttons are
    this message's (a gap a later run reopened has moved on to that
    run's message)."""

    id: int
    kind: str
    titles: tuple[str, ...]
    title: str | None
    detail: str
    reopened: int
    status: str
    actionable: bool


@dataclass(frozen=True)
class GardenMessage:
    """A run's one Telegram message (owner amendment (b)), before it is
    sent (`unsent_run`) or to re-render it after a tap
    (`run_message_state`).

    `new` and `reopened` count this message's gaps never reopened (raised
    by this run, or carried over from an unsent one) and reopened
    (`MessageGap.reopened > 0`); `older_open`
    counts the open gaps of earlier runs, whose buttons are in earlier
    messages. `report_path` is the run's note once the vault pass has
    written it, else None. `tg_message_id` is None until sent."""

    run_id: int
    iso_week: str
    new: int
    reopened: int
    older_open: int
    report_path: str | None
    gaps: tuple[MessageGap, ...]
    tg_message_id: int | None


@dataclass(frozen=True)
class ReportGap:
    """One gap in the Obsidian report, with its status now."""

    id: int
    kind: str
    note_ids: tuple[int, ...]
    titles: tuple[str, ...]
    title: str | None
    detail: str
    status: str
    reopened: int


@dataclass(frozen=True)
class ReportData:
    """Everything `render_report` needs for the latest run with gaps
    (L3 spec section 3): the run, its gaps, «Ещё открыто» (open gaps of
    earlier runs), its findings for «Структура», and the current lens
    titles by `lens_note` id -- the only notes the report may link to."""

    run_id: int
    iso_week: str
    created_at: datetime.datetime
    findings: dict
    gaps: tuple[ReportGap, ...]
    still_open: tuple[ReportGap, ...]
    lens_titles: dict[int, str]


@dataclass(frozen=True)
class GardenStatus:
    """/lens's «Сад: <дата>, открыто N»: the latest run and the open
    gaps across all runs."""

    last_run_at: datetime.datetime
    iso_week: str
    open: int


async def _current_version_id(session: AsyncSession) -> int | None:
    current, _count = await _current_version(session)
    return (
        await session.execute(select(LensVersion.id).where(LensVersion.hash == current))
    ).scalar_one_or_none()


async def garden_facts(session: AsyncSession) -> GardenFacts:
    """The gate's facts: how many lens notes, the last run (when, which
    week, which lens version), the lens version now, and the done gaps
    waiting for a recheck."""
    last = (
        await session.execute(
            select(LensGardenRun.created_at, LensGardenRun.iso_week, LensGardenRun.lens_version_id)
            .order_by(LensGardenRun.id.desc())
            .limit(1)
        )
    ).first()
    done = (
        await session.execute(
            select(func.count()).select_from(LensGap).where(LensGap.status == "done")
        )
    ).scalar_one()
    return GardenFacts(
        notes=await note_count(session),
        last_run_at=last[0] if last else None,
        last_iso_week=last[1] if last else None,
        last_version_id=last[2] if last else None,
        version_id=await _current_version_id(session),
        done=int(done),
    )


async def garden_view(session: AsyncSession) -> GardenView:
    """The lens as a graph (see `GardenView`). Reads every lens note
    whole, so call it once a run."""
    notes = tuple(
        GardenNote(
            id=note_id,
            file_id=file_id,
            kind=kind,
            title=title,
            aliases=tuple(aliases or ()),
            summary=summary,
            body=body,
            updated_at=updated_at,
        )
        for note_id, file_id, kind, title, aliases, summary, body, updated_at in await session.execute(
            select(
                LensNote.id,
                LensNote.vault_file_id,
                LensNote.kind,
                LensNote.title,
                LensNote.aliases,
                LensNote.summary,
                LensNote.body,
                LensNote.updated_at,
            ).order_by(LensNote.title, LensNote.id)
        )
    )
    lens_files = {note.file_id for note in notes}
    edges: set[tuple[int, int]] = set()
    outside: dict[int, int] = {}
    unresolved: set[tuple[int, str]] = set()
    if lens_files:
        rows = await session.execute(
            select(
                NoteLink.src_file_id, NoteLink.dst_file_id, NoteLink.unresolved_text, NoteLink.outside
            ).where(or_(NoteLink.src_file_id.in_(lens_files), NoteLink.dst_file_id.in_(lens_files)))
        )
        for src, dst, target, is_outside in rows:
            if dst is not None:
                if src != dst:
                    edges.add((src, dst))
            elif src in lens_files and is_outside:
                outside[src] = outside.get(src, 0) + 1
            elif src in lens_files and target is not None:
                unresolved.add((src, target))
    knowledge_titles = {
        file_id: _file_title(path)
        for file_id, path in await session.execute(
            select(VaultFile.id, VaultFile.path).where(
                VaultFile.role == "note",
                VaultFile.note_class == "knowledge",
                VaultFile.id.not_in(select(LensNote.vault_file_id)),
            )
        )
    }
    return GardenView(
        notes=notes,
        edges=tuple(sorted(edges)),
        outside=outside,
        unresolved=tuple(sorted(unresolved)),
        knowledge_titles=knowledge_titles,
        version_id=await _current_version_id(session),
    )


def _file_title(path: str) -> str:
    """A note's title is its file name without `.md` (app/vault/sync.py's
    `_note_title`, the plan's own rule)."""
    name = path.rsplit("/", 1)[-1]
    return name[: -len(".md")] if name.endswith(".md") else name


def _known(row: LensGap) -> KnownGap:
    return KnownGap(
        id=row.id,
        garden_run_id=row.garden_run_id,
        kind=row.kind,
        note_ids=tuple(row.note_ids or ()),
        titles=tuple(row.titles or ()),
        title=row.title,
        detail=row.detail,
        status=row.status,
        signature=row.signature,
        recheck=dict(row.recheck or {}),
        reopened=row.reopened,
    )


async def known_gaps(session: AsyncSession) -> list[KnownGap]:
    """Every gap that is not resolved, by id."""
    rows = (
        await session.execute(
            select(LensGap).where(LensGap.status != "resolved").order_by(LensGap.id)
        )
    ).scalars()
    return [_known(row) for row in rows]


def _check_new_gap(gap: NewGap) -> None:
    if gap.kind not in GAP_KINDS:
        raise ValueError(f"unknown lens gap kind: {gap.kind!r}")
    if not _SIGNATURE_RE.match(gap.signature):
        raise ValueError("lens gap signature must be 64 lowercase hex characters")
    if gap.title is not None and len(gap.title) > GAP_TITLE_MAX:
        raise ValueError(f"lens gap title over {GAP_TITLE_MAX} characters")
    if len(gap.detail) > GAP_DETAIL_MAX:
        raise ValueError(f"lens gap detail over {GAP_DETAIL_MAX} characters")


async def record_garden(
    session: AsyncSession,
    *,
    idle_run_id: int | None,
    iso_week: str,
    version_id: int | None,
    findings: dict,
    resolved_ids: Iterable[int],
    reopened_ids: Iterable[int],
    new: Iterable[NewGap],
    now: datetime.datetime | None = None,
) -> GardenRecord:
    """Write one garden run, all of it or nothing (the caller's one
    transaction; flushes, never commits):

    1. the `lens_garden_run` row (UNIQUE on `iso_week`: a second run in
       the same week fails with IntegrityError -- the gate prevents it);
    2. `resolved_ids`: open or done gaps whose recheck passed or whose
       note is gone become `resolved` (any other status is left alone);
    3. `reopened_ids`: done gaps whose recheck failed become open again,
       `reopened` + 1, their decision and message cleared, and move to
       this run -- so this run's message carries them, marked «снова»;
    4. open gaps of an earlier run whose message never went out (held
       by a long /quiet, a pause, failed sends) move to this run too, and
       that run is marked sent with no message: only the latest run's
       message is ever sent (`unsent_run`), and without the move those
       gaps would never get a keyboard row, while their live signatures
       kept them from being raised again (owner amendment (b): earlier
       gaps ride the new run's message);
    5. `new`: inserted with ON CONFLICT DO NOTHING on the partial unique
       index, so a signature that is open, done, dismissed or researched
       -- including one earlier in `new` -- is dropped and counted as
       deduped. Only a resolved signature may recur.

    Raises ValueError on an unknown kind, a malformed signature or week,
    or text over the columns' limits: step 2's validation should already
    have caught them. `now` stamps `resolved_at` (the database's clock
    when None)."""
    if not _ISO_WEEK_RE.match(iso_week):
        raise ValueError("iso_week must look like 2026-W40")
    if not isinstance(findings, dict):
        raise ValueError("findings must be a JSON object")
    gaps = list(new)
    for gap in gaps:
        _check_new_gap(gap)
    stamp = now if now is not None else func.now()

    run = LensGardenRun(
        idle_run_id=idle_run_id, iso_week=iso_week, lens_version_id=version_id, findings=findings
    )
    session.add(run)
    await session.flush()

    resolved = 0
    resolve = sorted({int(i) for i in resolved_ids})
    if resolve:
        result = await session.execute(
            update(LensGap)
            .where(LensGap.id.in_(resolve), LensGap.status.in_(("open", "done")))
            .values(status="resolved", resolved_at=stamp)
        )
        resolved = result.rowcount or 0

    reopened = 0
    reopen = sorted({int(i) for i in reopened_ids})
    if reopen:
        result = await session.execute(
            update(LensGap)
            .where(LensGap.id.in_(reopen), LensGap.status == "done")
            .values(
                status="open",
                reopened=LensGap.reopened + 1,
                decided_at=None,
                tg_message_id=None,
                garden_run_id=run.id,
            )
        )
        reopened = result.rowcount or 0

    unsent = (
        select(LensGardenRun.id)
        .where(LensGardenRun.id < run.id, LensGardenRun.sent_at.is_(None))
        .scalar_subquery()
    )
    carried = (
        await session.execute(
            update(LensGap)
            .where(
                LensGap.garden_run_id.in_(unsent),
                LensGap.status == "open",
                LensGap.tg_message_id.is_(None),
            )
            .values(garden_run_id=run.id)
        )
    ).rowcount or 0
    await session.execute(
        update(LensGardenRun)
        .where(LensGardenRun.id < run.id, LensGardenRun.sent_at.is_(None))
        .values(sent_at=stamp)
    )

    new_ids: list[int] = []
    for gap in gaps:
        stmt = (
            pg_insert(LensGap)
            .values(
                garden_run_id=run.id,
                kind=gap.kind,
                note_ids=[int(i) for i in gap.note_ids],
                titles=list(gap.titles),
                title=gap.title,
                detail=gap.detail,
                signature=gap.signature,
                recheck=dict(gap.recheck),
            )
            .on_conflict_do_nothing(
                # A literal, not a bound parameter: Postgres infers the
                # partial index only from a predicate it can match.
                index_elements=[LensGap.signature], index_where=text("status <> 'resolved'")
            )
            .returning(LensGap.id)
        )
        inserted = (await session.execute(stmt)).scalar_one_or_none()
        if inserted is not None:
            new_ids.append(inserted)
    await session.flush()
    record = GardenRecord(
        run_id=run.id,
        new_ids=tuple(new_ids),
        deduped=len(gaps) - len(new_ids),
        resolved=resolved,
        reopened=reopened,
        carried=carried,
    )
    logger.info(
        "lens garden recorded",
        extra={
            "garden_run_id": run.id,
            "count": len(new_ids),
            "dropped": record.deduped,
            "closed": resolved,
            "updated": reopened,
        },
    )
    return record


async def _epoch(session: AsyncSession) -> str | None:
    return (
        await session.execute(select(UserState.vault_epoch).where(UserState.id == 1))
    ).scalar_one_or_none()


async def _report_written(session: AsyncSession, week: str) -> str | None:
    """The run's note path, once vaultd has confirmed the note: a
    `report` row alone is not enough, since `_render_reports` commits it
    before the create, and a create that fails with anything but
    REFUSED or CONFLICT leaves it behind with no file. `disk_sha256` is
    set only by a confirmed write (or the manifest finding one)."""
    epoch = await _epoch(session)
    if not epoch:
        return None
    path = report_path(week, epoch)
    found = (
        await session.execute(
            select(VaultFile.id).where(
                VaultFile.path == path,
                VaultFile.role == "report",
                VaultFile.state == "ok",
                VaultFile.disk_sha256.is_not(None),
            )
        )
    ).scalar_one_or_none()
    return path if found is not None else None


async def _older_open(session: AsyncSession, run_id: int) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(LensGap)
                .where(LensGap.status == "open", LensGap.garden_run_id < run_id)
            )
        ).scalar_one()
    )


def _message_gap(row: LensGap, message_id: int | None, reopened: int | None = None) -> MessageGap:
    return MessageGap(
        id=row.id,
        kind=row.kind,
        titles=tuple(row.titles or ()),
        title=row.title,
        detail=row.detail,
        reopened=row.reopened if reopened is None else reopened,
        status=row.status,
        actionable=row.status == "open"
        and message_id is not None
        and row.tg_message_id == message_id,
    )


async def _unsent_gaps(session: AsyncSession, run_id: int) -> list[LensGap]:
    return list(
        (
            await session.execute(
                select(LensGap)
                .where(
                    LensGap.garden_run_id == run_id,
                    LensGap.status == "open",
                    LensGap.tg_message_id.is_(None),
                )
                .order_by(LensGap.id)
            )
        ).scalars()
    )


async def unsent_run(session: AsyncSession) -> GardenMessage | None:
    """The latest run, if its message has not been sent (None otherwise,
    or before the first run). There is no earlier unsent run:
    `record_garden` moves an unsent run's open gaps into the next run
    and marks it sent with no message, so the latest message carries
    them.

    Its gaps are the run's open gaps without a message -- the new ones,
    the ones it reopened and the ones it carried over -- by id. A run with none still returns
    (`gaps` empty): the caller decides whether a message with nothing to
    tap is worth sending, and marks the run sent either way
    (`mark_run_sent` with `message_id=None`)."""
    run = (
        await session.execute(select(LensGardenRun).order_by(LensGardenRun.id.desc()).limit(1))
    ).scalar_one_or_none()
    if run is None or run.sent_at is not None:
        return None
    rows = await _unsent_gaps(session, run.id)
    return GardenMessage(
        run_id=run.id,
        iso_week=run.iso_week,
        new=sum(1 for row in rows if row.reopened == 0),
        reopened=sum(1 for row in rows if row.reopened > 0),
        older_open=await _older_open(session, run.id),
        report_path=await _report_written(session, run.iso_week),
        gaps=tuple(_message_gap(row, None) for row in rows),
        tg_message_id=None,
    )


async def mark_run_sent(
    session: AsyncSession,
    run_id: int,
    message_id: int | None,
    *,
    gap_ids: Iterable[int] | None = None,
    now: datetime.datetime | None = None,
) -> bool:
    """Record that the run's message went out: `sent_at`, the message's
    id, and the gaps it lists in order (`gap_ids`, by default exactly
    the ones `unsent_run` listed) on the run; the message's id on each
    of those gaps, which is what makes their buttons live. Only the
    run's open gaps without a message are stamped. `message_id=None`
    marks the run sent with nothing sent. False (and nothing written)
    when the run is gone or already marked. Flushes, never commits."""
    run = await session.get(LensGardenRun, run_id)
    if run is None or run.sent_at is not None:
        return False
    if gap_ids is None:
        listed = [row.id for row in await _unsent_gaps(session, run_id)]
    else:
        listed = list(dict.fromkeys(int(i) for i in gap_ids))
    if message_id is None:
        listed = []
    counts = dict(
        (
            await session.execute(select(LensGap.id, LensGap.reopened).where(LensGap.id.in_(listed)))
        ).all()
    ) if listed else {}
    run.sent_at = now if now is not None else func.now()
    run.tg_message_id = message_id
    run.sent_gap_ids = listed
    # Each gap's `reopened` as this message shows it, for re-renders.
    run.sent_reopened = [int(counts.get(gap_id, 0)) for gap_id in listed]
    if message_id is not None and listed:
        await session.execute(
            update(LensGap)
            .where(
                LensGap.id.in_(listed),
                LensGap.garden_run_id == run_id,
                LensGap.status == "open",
                LensGap.tg_message_id.is_(None),
            )
            .values(tg_message_id=message_id)
        )
    await session.flush()
    logger.info(
        "lens garden message sent",
        extra={"garden_run_id": run_id, "count": len(run.sent_gap_ids)},
    )
    return True


async def message_run_id(session: AsyncSession, message_id: int) -> int | None:
    """The run whose message this is, for a tap's re-render."""
    return (
        await session.execute(
            select(LensGardenRun.id).where(LensGardenRun.tg_message_id == message_id)
        )
    ).scalar_one_or_none()


async def run_message_state(session: AsyncSession, run_id: int) -> GardenMessage | None:
    """The run's sent message as it should read now: the same gaps in the
    same order (`sent_gap_ids`, so the numbering never shifts), each with
    its status now and whether its row is still on the keyboard. The
    header's counts and each item's «снова» are as sent
    (`sent_reopened`): a gap a later run reopened has moved on to that
    run's message, and this one still says what it delivered. None
    when the run is gone or was never sent with a message."""
    run = await session.get(LensGardenRun, run_id)
    if run is None or run.tg_message_id is None:
        return None
    listed = list(run.sent_gap_ids or ())
    rows = {
        row.id: row
        for row in (
            await session.execute(select(LensGap).where(LensGap.id.in_(listed)))
        ).scalars()
    } if listed else {}
    sent = dict(zip(listed, run.sent_reopened or ()))
    gaps = [
        _message_gap(rows[gap_id], run.tg_message_id, sent.get(gap_id))
        for gap_id in listed
        if gap_id in rows
    ]
    return GardenMessage(
        run_id=run.id,
        iso_week=run.iso_week,
        new=sum(1 for gap in gaps if gap.reopened == 0),
        reopened=sum(1 for gap in gaps if gap.reopened > 0),
        older_open=await _older_open(session, run.id),
        report_path=await _report_written(session, run.iso_week),
        gaps=tuple(gaps),
        tg_message_id=run.tg_message_id,
    )


async def decide_gap(
    session: AsyncSession,
    gap_id: int,
    epoch: str,
    action: str,
    now: datetime.datetime,
    *,
    message_id: int | None = None,
) -> str:
    """A tap on «Сделал» (`done`) or «Не нужно» (`dismissed`): `ok` when
    it moved an open gap there, `stale` otherwise -- an unknown or
    decided gap, one never sent, the wrong vault epoch (a button from
    before /delete, whose identities restarted), or, when `message_id`
    is given, a button on a message that no longer carries the gap (a
    later run reopened it into its own message). A replayed tap is
    stale: the update only matches an open gap. Raises ValueError on
    any other action (`lg:r:` is the caller's to call stale). Flushes,
    never commits."""
    if action not in GAP_DECISIONS:
        raise ValueError(f"unknown lens gap decision: {action!r}")
    if epoch != await _epoch(session):
        return "stale"
    conditions = [LensGap.id == gap_id, LensGap.status == "open", LensGap.tg_message_id.is_not(None)]
    if message_id is not None:
        conditions.append(LensGap.tg_message_id == message_id)
    decided = (
        await session.execute(
            update(LensGap).where(*conditions).values(status=action, decided_at=now).returning(LensGap.id)
        )
    ).scalar_one_or_none()
    await session.flush()
    outcome = "ok" if decided is not None else "stale"
    logger.info(
        "lens gap decided", extra={"gap_id": gap_id, "event": action if decided else "stale"}
    )
    return outcome


def _report_gap(row: LensGap) -> ReportGap:
    return ReportGap(
        id=row.id,
        kind=row.kind,
        note_ids=tuple(row.note_ids or ()),
        titles=tuple(row.titles or ()),
        title=row.title,
        detail=row.detail,
        status=row.status,
        reopened=row.reopened,
    )


async def report_data(session: AsyncSession) -> ReportData | None:
    """The latest run that has gaps, for the Obsidian report (None before
    one exists): its gaps by id, whatever their status; the open gaps of
    earlier runs; its findings as stored; and the current lens titles."""
    run = (
        await session.execute(
            select(LensGardenRun)
            .where(select(LensGap.id).where(LensGap.garden_run_id == LensGardenRun.id).exists())
            .order_by(LensGardenRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if run is None:
        return None
    own = (
        await session.execute(
            select(LensGap).where(LensGap.garden_run_id == run.id).order_by(LensGap.id)
        )
    ).scalars()
    older = (
        await session.execute(
            select(LensGap)
            .where(LensGap.garden_run_id < run.id, LensGap.status == "open")
            .order_by(LensGap.id)
        )
    ).scalars()
    titles = dict((await session.execute(select(LensNote.id, LensNote.title))).all())
    return ReportData(
        run_id=run.id,
        iso_week=run.iso_week,
        created_at=run.created_at,
        findings=dict(run.findings or {}),
        gaps=tuple(_report_gap(row) for row in own),
        still_open=tuple(_report_gap(row) for row in older),
        lens_titles=titles,
    )


async def garden_status(session: AsyncSession) -> GardenStatus | None:
    """The latest run and the open gaps, for /lens; None before the first run."""
    last = (
        await session.execute(
            select(LensGardenRun.created_at, LensGardenRun.iso_week)
            .order_by(LensGardenRun.id.desc())
            .limit(1)
        )
    ).first()
    if last is None:
        return None
    open_count = (
        await session.execute(
            select(func.count()).select_from(LensGap).where(LensGap.status == "open")
        )
    ).scalar_one()
    return GardenStatus(last_run_at=last[0], iso_week=last[1], open=int(open_count))


async def delete_garden(session: AsyncSession) -> int:
    """Every gap and run: the garden dies with the lens. Called wherever
    `lens_note` is emptied -- a flag or consent going off (consent's own
    cascade deletes the notes without `delete_all`, so that path needs
    this call too) -- and /delete truncates both tables itself. The
    returned count is the gaps'. Flushes, never commits."""
    gaps = (await session.execute(delete(LensGap))).rowcount or 0
    await session.execute(delete(LensGardenRun))
    await session.flush()
    return gaps


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

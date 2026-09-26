"""The code both note access modules share (8e plan section 6).

**This module never names a chunk table.** Every function takes the
model as an argument, and only notes_personal.py and notes_knowledge.py
pass one, each its own (tests/test_vault_notes_isolation.py). That keeps
"only the access module touches its table" checkable by a scan of names,
while the two modules stay line-for-line the same.

**Consent is checked here, not only by callers.** `search` returns
nothing, in the same query, unless `user_state.notes_consent` is on,
and `replace_chunks` refuses to write while it is off. 8d's sync pass
and turn.py check it too; this is the floor under them
(docs/decisions.md, "8e -- consent is checked inside the access
modules").

**No rank threshold yet.** `search` returns every match, best first,
up to `limit`. The thresholds (PERSONAL_MIN_RANK, KNOWLEDGE_MIN_RANK)
are measured per class in 8d, before anything calls this; 8e ships no
caller.

Nothing here commits: the caller owns the transaction, so a file's row
and its chunks change together. Nothing here logs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UserState


@dataclass(frozen=True)
class Chunk:
    heading: str | None
    text: str


class NotesConsentOff(Exception):
    """A write to a chunk table while notes consent is off."""


async def _consented(session: AsyncSession) -> bool:
    return bool(
        (await session.execute(select(UserState.notes_consent).where(UserState.id == 1))).scalar_one_or_none()
    )


async def replace_chunks(session: AsyncSession, model: type, file_id: int, chunks: Sequence[Chunk]) -> None:
    """Replace a file's chunks with `chunks`, in order. Refuses without consent.

    The model's composite foreign key refuses a chunk under a file of
    the other class; that error propagates.
    """
    if not await _consented(session):
        raise NotesConsentOff
    await session.execute(delete(model).where(model.file_id == file_id))
    session.add_all(
        model(file_id=file_id, ord=i, heading=chunk.heading, text=chunk.text) for i, chunk in enumerate(chunks)
    )
    await session.flush()


async def delete_for_file(session: AsyncSession, model: type, file_id: int) -> int:
    result = await session.execute(delete(model).where(model.file_id == file_id))
    return result.rowcount or 0


async def search(session: AsyncSession, model: type, user_text: str, limit: int) -> list[str]:
    """Up to `limit` matching chunks as «heading»: text, best first. Strings only.

    The query is the phase-8 plan's: the user text's own Russian
    lexemes OR-ed into one tsquery, ranked with ts_rank_cd. Text with no
    lexemes (all stopwords, or empty) gives a NULL query, which matches
    nothing, rather than an error.
    """
    if limit <= 0 or not user_text.strip():
        return []
    table = model.__tablename__
    rows = (
        await session.execute(
            text(
                f"""
                WITH q AS (
                    SELECT to_tsquery('russian', (
                        SELECT string_agg(quote_literal(lexeme), ' | ')
                        FROM unnest(to_tsvector('russian', :user_text))
                    )) AS query
                )
                SELECT c.heading, c.text
                FROM {table} AS c, q, user_state AS s
                WHERE s.id = 1 AND s.notes_consent AND c.tsv @@ q.query
                ORDER BY ts_rank_cd(c.tsv, q.query, 32) DESC, c.id
                LIMIT :limit
                """
            ),
            {"user_text": user_text, "limit": limit},
        )
    ).all()
    return [f"«{heading}»: {body}" if heading else body for heading, body in rows]


async def search_ranked(
    session: AsyncSession, model: type, user_text: str, limit: int, *, min_matched: int = 0
) -> list[tuple[str | None, str, float, int]]:
    """Like `search`, but `(heading, text, rank, matched)` instead of a formatted string.

    Exists for milestone 8d's measurement (scripts/measure_note_rank.py,
    called with `min_matched` left at its default of 0) and, from C3,
    for `notes_knowledge.search_library`'s own floor. Picking
    `PERSONAL_MIN_RANK`/`KNOWLEDGE_MIN_RANK` needs the raw `ts_rank_cd`
    score, which `search` deliberately never exposes to a caller (its
    own callers get strings only, never a score or an id -- see the
    module docstring); consent is still enforced the same way.

    `matched` is the count of *distinct* query lexemes the chunk's tsv
    actually contains -- a lexical gate candidate alongside the rank
    floor (a chunk can rank respectably on `ts_rank_cd` off a single
    rare, heavily-weighted lexeme; `matched` tells the caller how many
    of the query's own words it is really about). `min_matched > 0`
    applies that floor inside this same statement, in the `WHERE` of
    the outer query over the `scored` CTE, *before* `ORDER BY ...
    LIMIT` -- not by asking for more rows and filtering in Python
    afterwards, which would silently drop a chunk that clears the floor
    but ranks below `limit` other chunks that do not (a real bug this
    project shipped once and fixed: see notes_knowledge.py's C3 note).
    """
    if limit <= 0 or not user_text.strip():
        return []
    table = model.__tablename__
    rows = (
        await session.execute(
            text(
                f"""
                WITH q AS (
                    SELECT
                        to_tsquery('russian', (
                            SELECT string_agg(quote_literal(lexeme), ' | ')
                            FROM unnest(to_tsvector('russian', :user_text))
                        )) AS query,
                        (
                            SELECT array_agg(DISTINCT lexeme)
                            FROM unnest(to_tsvector('russian', :user_text))
                        ) AS lexemes
                ),
                scored AS (
                    SELECT
                        c.id,
                        c.heading,
                        c.text,
                        ts_rank_cd(c.tsv, q.query, 32) AS rank,
                        (
                            SELECT count(*)
                            FROM unnest(tsvector_to_array(c.tsv)) AS w
                            WHERE w = ANY (q.lexemes)
                        ) AS matched
                    FROM {table} AS c, q, user_state AS s
                    WHERE s.id = 1 AND s.notes_consent AND c.tsv @@ q.query
                )
                SELECT heading, text, rank, matched
                FROM scored
                WHERE matched >= :min_matched
                ORDER BY rank DESC, id
                LIMIT :limit
                """
            ),
            {"user_text": user_text, "limit": limit, "min_matched": min_matched},
        )
    ).all()
    return [(heading, body, float(rank), int(matched)) for heading, body, rank, matched in rows]

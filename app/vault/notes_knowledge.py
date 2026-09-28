"""The only code that touches `note_chunk_knowledge` (8e plan sections 6-8).

A knowledge note is generic: true whoever reads it (a note on CCRU, on
hyperstition, on GCP IAM). It is reference material, not evidence that
the user holds the view, and 8d frames it that way in the prompt. In 8e
its text reaches only the persona's chat turn, like a personal note;
widening that takes a plan that cites the 8e plan's section 7.

**Who may import this module** is pinned by
tests/test_vault_notes_isolation.py: `app/core/turn.py` and the rest of
`app/vault/` (the sync pass, 8d). A later consumer adds itself there by
one line, justified against section 7. No other module may name the
table.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NoteChunkKnowledge, VaultFile
from app.vault import _chunks
from app.vault._chunks import Chunk, NotesConsentOff

__all__ = [
    "Chunk",
    "LibraryRow",
    "NotesConsentOff",
    "LIBRARY_MAX_CHUNKS",
    "LIBRARY_MIN_MATCHED",
    "delete_for_file",
    "replace_chunks",
    "search",
    "search_library",
    "search_library_rows",
]


@dataclass(frozen=True)
class LibraryRow:
    """One `search_library` result, with the path and hash W2b's write
    switch needs to let Claude name the note it read (plan section 3).
    `sha256` is the vault's last-synced hash of the file
    (`vault_file.disk_sha256`), a hint for which note to `get_note` --
    never treated as a fresh CAS `base_hash` on its own; a write tool
    always re-reads first."""

    heading: str | None
    text: str
    path: str
    sha256: str | None

# Connector plan section 9 (C3), the user's fixed decision -- not a
# setting, so a deploy cannot loosen either number:
# docs/decisions.md, "C3 -- search_library without the failed
# threshold".
LIBRARY_MAX_CHUNKS = 6
LIBRARY_MIN_MATCHED = 2


async def replace_chunks(session: AsyncSession, file_id: int, chunks: Sequence[Chunk]) -> None:
    await _chunks.replace_chunks(session, NoteChunkKnowledge, file_id, chunks)


async def delete_for_file(session: AsyncSession, file_id: int) -> int:
    return await _chunks.delete_for_file(session, NoteChunkKnowledge, file_id)


async def search(session: AsyncSession, user_text: str, limit: int) -> list[str]:
    return await _chunks.search(session, NoteChunkKnowledge, user_text, limit)


async def search_library(
    session: AsyncSession,
    user_text: str,
    *,
    limit: int = LIBRARY_MAX_CHUNKS,
    min_matched: int = LIBRARY_MIN_MATCHED,
) -> list[str]:
    """The Claude connector's `search_library` tool (connector plan
    section 9, C3): up to `limit` knowledge chunks as plain strings,
    best `ts_rank_cd` first, keeping only chunks sharing at least
    `min_matched` distinct lexemes with the query. No rank threshold --
    the user's decision was matched-lexeme count, not a score.

    The floor is applied inside `search_ranked`'s own SQL, in the
    `WHERE` over its `scored` CTE, *before* `ORDER BY ... LIMIT` --
    not by asking for a fixed-size pool of top-ranked candidates and
    filtering by `matched` afterwards in Python. That shape shipped
    once and was a real bug: if the top-of-pool rows by rank all fail
    the floor, a valid, lower-ranked chunk that clears it can fall
    outside the pool and never be seen at all, regardless of how large
    the pool is made -- there is always a query that defeats a fixed
    pool size. Filtering in the same statement, before the limit, has
    no such ceiling.

    Consent is enforced inside `search_ranked`'s own SQL statement too,
    the same floor every other reader of this table stands on. Never
    exposes a chunk id or a file path -- only `search_ranked`'s
    `(heading, text)`, formatted as a string, exactly like `search`
    above.
    """
    rows = await _chunks.search_ranked(
        session, NoteChunkKnowledge, user_text, limit, min_matched=min_matched
    )
    return [f"«{heading}»: {body}" if heading else body for heading, body, _rank, _matched in rows]


async def search_library_rows(
    session: AsyncSession,
    user_text: str,
    *,
    limit: int = LIBRARY_MAX_CHUNKS,
    min_matched: int = LIBRARY_MIN_MATCHED,
) -> list[LibraryRow]:
    """Like `search_library`, but structured, with each chunk's file
    path and last-synced hash (W2b, plan section 3): `mcp_core.py`
    builds the "with path+hash" or plain text shape depending on
    whether the write switch is on, in one place, rather than this
    module knowing about that switch at all.
    """
    rows = await _chunks.search_ranked_with_file(
        session, NoteChunkKnowledge, user_text, limit, min_matched=min_matched
    )
    if not rows:
        return []
    file_ids = {file_id for _h, _t, _r, _m, file_id in rows}
    result = await session.execute(
        select(VaultFile.id, VaultFile.path, VaultFile.disk_sha256).where(VaultFile.id.in_(file_ids))
    )
    by_id = {fid: (path, sha) for fid, path, sha in result.all()}
    out = []
    for heading, body, _rank, _matched, file_id in rows:
        path, sha = by_id.get(file_id, (None, None))
        if path is None:
            continue
        out.append(LibraryRow(heading=heading, text=body, path=path, sha256=sha))
    return out

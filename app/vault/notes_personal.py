"""The only code that touches `note_chunk_personal` (8e plan sections 6-8).

A personal note is about the user: their life, health, relationships,
plans and feelings. Its text reaches exactly one consumer, the
persona's chat turn (8d), and never a search engine, a research or idle
model, a notebook, a grant, a web panel or any query that leaves the
system -- in this phase or any later one.

**Who may import this module** is pinned by
tests/test_vault_notes_isolation.py: `app/core/turn.py` and the rest of
`app/vault/` (the sync pass, 8d). Nothing else, and no other module may
name the table. 8e ships this module and its tests; 8d wires it in.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NoteChunkPersonal
from app.vault import _chunks
from app.vault._chunks import Chunk, NotesConsentOff

__all__ = ["Chunk", "NotesConsentOff", "delete_for_file", "replace_chunks", "search"]


async def replace_chunks(session: AsyncSession, file_id: int, chunks: Sequence[Chunk]) -> None:
    await _chunks.replace_chunks(session, NoteChunkPersonal, file_id, chunks)


async def delete_for_file(session: AsyncSession, file_id: int) -> int:
    return await _chunks.delete_for_file(session, NoteChunkPersonal, file_id)


async def search(session: AsyncSession, user_text: str, limit: int) -> list[str]:
    return await _chunks.search(session, NoteChunkPersonal, user_text, limit)

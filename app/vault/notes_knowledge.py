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

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NoteChunkKnowledge
from app.vault import _chunks
from app.vault._chunks import Chunk, NotesConsentOff

__all__ = ["Chunk", "NotesConsentOff", "delete_for_file", "replace_chunks", "search"]


async def replace_chunks(session: AsyncSession, file_id: int, chunks: Sequence[Chunk]) -> None:
    await _chunks.replace_chunks(session, NoteChunkKnowledge, file_id, chunks)


async def delete_for_file(session: AsyncSession, file_id: int) -> int:
    return await _chunks.delete_for_file(session, NoteChunkKnowledge, file_id)


async def search(session: AsyncSession, user_text: str, limit: int) -> list[str]:
    return await _chunks.search(session, NoteChunkKnowledge, user_text, limit)

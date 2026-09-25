"""The two note access modules, against the database (8e plan section 6).

8e ships them and tests them directly; 8d wires the sync pass and
turn.py to them. Synthetic notes only.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from app.db.models import UserState, VaultFile
from app.vault import notes_knowledge, notes_personal
from app.vault._chunks import Chunk, NotesConsentOff

MODULES = {"personal": notes_personal, "knowledge": notes_knowledge}


async def _seed(sessionmaker, *, consent: bool) -> dict[str, int]:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, notes_consent=consent))
        personal = VaultFile(path="Жизнь/Бег.md", role="note", note_class="personal")
        knowledge = VaultFile(path="Library/CCRU.md", role="note", note_class="knowledge")
        session.add_all([personal, knowledge])
        await session.commit()
        return {"personal": personal.id, "knowledge": knowledge.id}


async def _set_consent(sessionmaker, on: bool) -> None:
    async with sessionmaker() as session:
        await session.execute(update(UserState).values(notes_consent=on))
        await session.commit()


@pytest.mark.parametrize("note_class", sorted(MODULES))
async def test_replace_and_search(sessionmaker, note_class):
    files = await _seed(sessionmaker, consent=True)
    module = MODULES[note_class]
    async with sessionmaker() as session:
        await module.replace_chunks(
            session,
            files[note_class],
            [
                Chunk("Бег", "Бегаю по утрам в парке."),
                Chunk(None, "В парке много деревьев, парк большой."),
                Chunk("Сон", "Ложусь поздно."),
            ],
        )
        await session.commit()
    async with sessionmaker() as session:
        found = await module.search(session, "люблю гулять в парке по утрам", 5)
        assert found == ["«Бег»: Бегаю по утрам в парке.", "В парке много деревьев, парк большой."]
        assert await module.search(session, "люблю гулять в парке по утрам", 1) == found[:1]
        assert await module.search(session, "парк", 0) == []
        # Replacing is a replacement, not an append.
        await module.replace_chunks(session, files[note_class], [Chunk("Сон", "Ложусь поздно.")])
        await session.commit()
        assert await module.search(session, "парк", 5) == []


async def test_search_returns_strings_only_and_never_the_other_class(sessionmaker):
    files = await _seed(sessionmaker, consent=True)
    async with sessionmaker() as session:
        await notes_personal.replace_chunks(session, files["personal"], [Chunk("Бег", "Бегаю в парке.")])
        await notes_knowledge.replace_chunks(session, files["knowledge"], [Chunk("CCRU", "Парк культуры.")])
        await session.commit()
    async with sessionmaker() as session:
        personal = await notes_personal.search(session, "парк", 5)
        knowledge = await notes_knowledge.search(session, "парк", 5)
    assert personal == ["«Бег»: Бегаю в парке."]
    assert knowledge == ["«CCRU»: Парк культуры."]
    assert all(isinstance(item, str) for item in personal + knowledge)


@pytest.mark.parametrize("user_text", ["", "   ", "и в на с", "а"])
async def test_text_without_lexemes_finds_nothing(sessionmaker, user_text):
    files = await _seed(sessionmaker, consent=True)
    async with sessionmaker() as session:
        await notes_personal.replace_chunks(session, files["personal"], [Chunk(None, "и в на с парк")])
        await session.commit()
        assert await notes_personal.search(session, user_text, 5) == []


@pytest.mark.parametrize("note_class", sorted(MODULES))
async def test_without_consent_nothing_is_written_or_found(sessionmaker, note_class):
    files = await _seed(sessionmaker, consent=True)
    module = MODULES[note_class]
    async with sessionmaker() as session:
        await module.replace_chunks(session, files[note_class], [Chunk("Бег", "Бегаю в парке.")])
        await session.commit()
    await _set_consent(sessionmaker, False)
    async with sessionmaker() as session:
        assert await module.search(session, "парк", 5) == []
        with pytest.raises(NotesConsentOff):
            await module.replace_chunks(session, files[note_class], [Chunk("Бег", "x")])


async def test_a_module_cannot_file_a_chunk_under_the_other_class(sessionmaker):
    files = await _seed(sessionmaker, consent=True)
    async with sessionmaker() as session:
        with pytest.raises(IntegrityError, match="fk_note_chunk_personal_file_class"):
            await notes_personal.replace_chunks(session, files["knowledge"], [Chunk(None, "личное")])
    async with sessionmaker() as session:
        with pytest.raises(IntegrityError, match="fk_note_chunk_knowledge_file_class"):
            await notes_knowledge.replace_chunks(session, files["personal"], [Chunk(None, "CCRU")])


@pytest.mark.parametrize("note_class", sorted(MODULES))
async def test_delete_for_file(sessionmaker, note_class):
    files = await _seed(sessionmaker, consent=True)
    module = MODULES[note_class]
    async with sessionmaker() as session:
        await module.replace_chunks(session, files[note_class], [Chunk(None, "a"), Chunk(None, "b")])
        await session.commit()
        assert await module.delete_for_file(session, files[note_class]) == 2
        await session.commit()
        table = f"note_chunk_{note_class}"
        assert (await session.execute(text(f"select count(*) from {table}"))).scalar_one() == 0
        # The file row stays: deleting it is the caller's decision.
        assert (await session.execute(select(VaultFile.id).where(VaultFile.id == files[note_class]))).first()

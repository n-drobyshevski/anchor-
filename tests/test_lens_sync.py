"""The lens in the sync pass (anchor-lens-plan.md sections 3-5, milestone L1).

The real pass against the throwaway database and tests/vault_fake.py's
in-memory vault, which now answers the graph route too. Every note here
is synthetic. What is pinned:

- a lens note is indexed as knowledge always, and kept whole in the lens
  table only under notes consent + VAULT_KNOWLEDGE_ENABLED +
  LENS_ENABLED; any gate off deletes those rows;
- the kind comes from the manifest, the summary from the graph;
- joining or leaving the lens with an unchanged file still takes effect;
- links are refreshed from the graph, with `outside` links stored as a
  bare flag and unresolved ones with their target text;
- a lens version is recorded only when the lens changed, and its hash
  does not depend on order.
"""

from __future__ import annotations

import datetime
import hashlib

from sqlalchemy import select, text

from app.config import Settings
from app.core.clock import FrozenClock
from app.db.models import LensNote, LensVersion, NoteChunkKnowledge, NoteLink, UserState, VaultFile
from app.vault import consent, errors, lens
from app.vault.client import Graph, GraphEdge, GraphNode
from app.vault.errors import VaultError
from app.vault import sync as sync_module
from app.vault.sync import run_vault_sync
from vault_fake import FakeVault

TOKEN = "vault-token-" + "v" * 32
NOW = datetime.datetime(2026, 9, 29, 10, 0, tzinfo=datetime.timezone.utc)

ASHBY = "# Requisite variety\n\nOnly variety can absorb variety. See [[Beer]] and [[Viable system model]].\n"
BEER = "---\nsummary: Cybernetician of management.\n---\n# Beer\n\nWrote on [[Requisite variety]].\n"


def _settings(*, knowledge: bool = True, lens_on: bool = True) -> Settings:
    return Settings(
        VAULT_MODE="mirror",
        VAULT_API_TOKEN=TOKEN,
        VAULT_KNOWLEDGE_ENABLED=knowledge,
        LENS_ENABLED=lens_on,
    )


async def _seed(sessionmaker, *, notes_consent: bool = True) -> None:
    async with sessionmaker() as session:
        session.add(
            UserState(
                id=1, chat_id=555, timezone="Europe/Paris", vault_epoch="abcdef",
                notes_consent=notes_consent,
            )
        )
        await session.commit()


async def _pass(sessionmaker, vault, settings=None):
    async with sessionmaker() as session:
        return await run_vault_sync(session, settings or _settings(), FrozenClock(NOW), vault)


async def _lens_rows(sessionmaker) -> dict[str, LensNote]:
    async with sessionmaker() as session:
        rows = await session.execute(
            select(VaultFile.path, LensNote).join(LensNote, LensNote.vault_file_id == VaultFile.id)
        )
        return {path: note for path, note in rows}


async def _links(sessionmaker) -> list[tuple]:
    async with sessionmaker() as session:
        src = select(VaultFile.path).where(VaultFile.id == NoteLink.src_file_id).scalar_subquery()
        dst = select(VaultFile.path).where(VaultFile.id == NoteLink.dst_file_id).scalar_subquery()
        rows = await session.execute(select(src, dst, NoteLink.unresolved_text, NoteLink.outside))
        return sorted((tuple(row) for row in rows), key=_link_key)


def _link_key(link: tuple) -> tuple:
    return tuple("" if part is None else str(part) for part in link)


async def _versions(sessionmaker) -> list[LensVersion]:
    async with sessionmaker() as session:
        return list((await session.execute(select(LensVersion).order_by(LensVersion.id))).scalars())


def _lens_vault() -> FakeVault:
    vault = FakeVault()
    vault.notes["Lens/Concepts/Requisite variety.md"] = ("lens", ASHBY)
    vault.notes["Lens/People/Beer.md"] = ("lens", BEER)
    vault.lens_kinds["Lens/People/Beer.md"] = "person"
    vault.summaries["Lens/People/Beer.md"] = "Cybernetician of management."
    return vault


# --- the three gates -----------------------------------------------------------


async def test_lens_notes_are_stored_whole_under_all_three_gates(sessionmaker):
    await _seed(sessionmaker)
    vault = _lens_vault()
    result = await _pass(sessionmaker, vault)
    assert result.indexed == 2 and result.lens_stored == 2

    rows = await _lens_rows(sessionmaker)
    beer = rows["Lens/People/Beer.md"]
    variety = rows["Lens/Concepts/Requisite variety.md"]
    assert (beer.kind, beer.title, beer.summary) == ("person", "Beer", "Cybernetician of management.")
    assert (variety.kind, variety.title, variety.summary) == ("concept", "Requisite variety", None)
    # The body is the note as the chunker cleans it: no frontmatter, links
    # read as their labels, secrets masked (notes_text.prepare_body).
    assert "summary:" not in beer.body and "[[" not in beer.body
    assert beer.body.startswith("# Beer") and "Wrote on Requisite variety." in beer.body
    assert beer.body_hash == hashlib.sha256(beer.body.encode()).hexdigest()
    assert beer.chars == len(beer.body)

    # Lens is a kind of knowledge: both notes are indexed under knowledge rows.
    async with sessionmaker() as session:
        classes = set((await session.execute(select(VaultFile.note_class))).scalars())
        chunks = (await session.execute(select(NoteChunkKnowledge))).scalars().all()
    assert classes == {"knowledge"}
    assert len({c.file_id for c in chunks}) == 2


async def test_knowledge_notes_never_enter_the_lens_table(sessionmaker):
    await _seed(sessionmaker)
    vault = FakeVault()
    vault.notes["Library/CCRU.md"] = ("knowledge", "# CCRU\n\nHyperstition.")
    vault.notes["Life/Diary.md"] = ("personal", "# Diary\n\nЛичное.")
    result = await _pass(sessionmaker, vault)
    assert result.indexed == 1
    assert await _lens_rows(sessionmaker) == {}
    assert ("get", "Life/Diary.md") not in vault.calls


async def test_lens_flag_off_stores_nothing_and_deletes_what_was_stored(sessionmaker):
    await _seed(sessionmaker)
    vault = _lens_vault()
    result = await _pass(sessionmaker, vault, _settings(lens_on=False))
    assert result.indexed == 2 and result.lens_stored == 0
    assert await _lens_rows(sessionmaker) == {}

    await _pass(sessionmaker, vault)
    assert len(await _lens_rows(sessionmaker)) == 2

    result = await _pass(sessionmaker, vault, _settings(lens_on=False))
    assert result.lens_removed == 2
    assert await _lens_rows(sessionmaker) == {}
    # The knowledge index and the links stay: only the lens is off.
    async with sessionmaker() as session:
        assert len((await session.execute(select(VaultFile))).scalars().all()) == 2
    assert await _links(sessionmaker) != []
    assert await _versions(sessionmaker) != []


async def test_knowledge_flag_off_deletes_lens_rows_and_links(sessionmaker):
    await _seed(sessionmaker)
    vault = _lens_vault()
    await _pass(sessionmaker, vault)
    assert await _lens_rows(sessionmaker) and await _links(sessionmaker)

    vault.calls.clear()
    result = await _pass(sessionmaker, vault, _settings(knowledge=False))
    assert result.lens_removed == 2 and result.removed == 2
    assert await _lens_rows(sessionmaker) == {}
    assert await _links(sessionmaker) == []
    # The graph is behind the same gates as the knowledge index.
    assert ("graph", "") not in vault.calls


async def test_consent_off_reads_nothing_and_the_off_switch_cascades(sessionmaker):
    await _seed(sessionmaker, notes_consent=False)
    vault = _lens_vault()
    result = await _pass(sessionmaker, vault)
    assert result.indexed == 0
    assert ("graph", "") not in vault.calls
    assert await _lens_rows(sessionmaker) == {}

    async with sessionmaker() as session:
        await consent.set_notes_consent(session, True)
    await _pass(sessionmaker, vault)
    assert len(await _lens_rows(sessionmaker)) == 2

    async with sessionmaker() as session:
        await consent.set_notes_consent(session, False)
    assert await _lens_rows(sessionmaker) == {}
    assert await _links(sessionmaker) == []


async def test_store_refuses_without_consent(sessionmaker):
    await _seed(sessionmaker, notes_consent=False)
    async with sessionmaker() as session:
        row = VaultFile(path="Lens/X.md", role="note", note_class="knowledge")
        session.add(row)
        await session.flush()
        try:
            await lens.store(
                session, row.id, kind="concept", title="X", summary=None, body="x", now=NOW
            )
        except lens.NotesConsentOff:
            pass
        else:
            raise AssertionError("store wrote without consent")


# --- membership and kind changes with the file unchanged -------------------------


async def test_joining_the_lens_without_editing_the_file_fetches_it_once(sessionmaker):
    await _seed(sessionmaker)
    vault = FakeVault()
    vault.notes["Library/Ashby.md"] = ("knowledge", ASHBY)
    await _pass(sessionmaker, vault)
    assert await _lens_rows(sessionmaker) == {}

    # The user adds a lens_folders rule: same bytes, new class.
    vault.notes["Library/Ashby.md"] = ("lens", ASHBY)
    vault.calls.clear()
    result = await _pass(sessionmaker, vault)
    assert ("get", "Library/Ashby.md") in vault.calls
    assert result.lens_stored == 1
    assert (await _lens_rows(sessionmaker))["Library/Ashby.md"].kind == "concept"

    vault.calls.clear()
    await _pass(sessionmaker, vault)
    assert ("get", "Library/Ashby.md") not in vault.calls

    # And out again (an `anchor: knowledge` override): the row goes, the
    # knowledge index stays.
    vault.notes["Library/Ashby.md"] = ("knowledge", ASHBY)
    result = await _pass(sessionmaker, vault)
    assert result.lens_removed == 1
    assert await _lens_rows(sessionmaker) == {}
    async with sessionmaker() as session:
        assert (await session.execute(select(VaultFile.path))).scalars().all() == ["Library/Ashby.md"]


async def test_kind_comes_from_the_manifest_and_follows_it_without_a_fetch(sessionmaker):
    await _seed(sessionmaker)
    vault = _lens_vault()
    await _pass(sessionmaker, vault)
    assert (await _lens_rows(sessionmaker))["Lens/People/Beer.md"].kind == "person"

    vault.lens_kinds["Lens/People/Beer.md"] = "concept"
    vault.summaries["Lens/People/Beer.md"] = "Management cybernetics."
    vault.calls.clear()
    result = await _pass(sessionmaker, vault)
    assert ("get", "Lens/People/Beer.md") not in vault.calls
    assert result.lens_stored == 1
    beer = (await _lens_rows(sessionmaker))["Lens/People/Beer.md"]
    assert (beer.kind, beer.summary) == ("concept", "Management cybernetics.")


async def test_an_edited_lens_note_replaces_its_body(sessionmaker):
    await _seed(sessionmaker)
    vault = _lens_vault()
    await _pass(sessionmaker, vault)
    before = (await _lens_rows(sessionmaker))["Lens/Concepts/Requisite variety.md"]

    vault.notes["Lens/Concepts/Requisite variety.md"] = ("lens", "# Requisite variety\n\nRewritten.\n")
    result = await _pass(sessionmaker, vault)
    assert result.lens_stored == 1
    after = (await _lens_rows(sessionmaker))["Lens/Concepts/Requisite variety.md"]
    assert after.id == before.id
    assert after.body == "# Requisite variety\n\nRewritten."
    assert after.body_hash != before.body_hash


# --- links ------------------------------------------------------------------------


async def test_links_are_refreshed_with_outside_and_unresolved(sessionmaker):
    await _seed(sessionmaker)
    vault = _lens_vault()
    vault.notes["Life/Private person.md"] = ("personal", "# Private person\n\nЛичное.")
    vault.notes["Lens/Concepts/Requisite variety.md"] = (
        "lens",
        ASHBY + "\nAlso [[Private person]].\n",
    )
    result = await _pass(sessionmaker, vault)
    assert result.links_rewritten is True
    assert await _links(sessionmaker) == sorted(
        [
            ("Lens/Concepts/Requisite variety.md", "Lens/People/Beer.md", None, False),
            ("Lens/Concepts/Requisite variety.md", None, "Viable system model", False),
            ("Lens/Concepts/Requisite variety.md", None, None, True),
            ("Lens/People/Beer.md", "Lens/Concepts/Requisite variety.md", None, False),
        ],
        key=_link_key,
    )
    # The personal note was never fetched, and nothing names it.
    assert ("get", "Life/Private person.md") not in vault.calls
    async with sessionmaker() as session:
        texts = (await session.execute(select(NoteLink.unresolved_text))).scalars().all()
    assert "Private person" not in texts

    # An unchanged graph rewrites nothing.
    again = await _pass(sessionmaker, vault)
    assert again.links_rewritten is False


async def test_only_edges_from_tracked_notes_are_kept(sessionmaker):
    await _seed(sessionmaker)
    vault = _lens_vault()
    vault.graph = Graph(
        nodes=[],
        edges=[
            GraphEdge(src="Lens/People/Beer.md", dst="Lens/Concepts/Requisite variety.md"),
            GraphEdge(src="Nowhere/Unknown.md", unresolved="Ghost"),
            GraphEdge(src="Lens/People/Beer.md", dst="Nowhere/Unknown.md"),
        ],
        truncated=False,
    )
    await _pass(sessionmaker, vault)
    assert await _links(sessionmaker) == [
        ("Lens/People/Beer.md", "Lens/Concepts/Requisite variety.md", None, False)
    ]


async def test_a_secret_shaped_unresolved_target_is_not_stored(sessionmaker):
    """The floor under vaultd's aside and code stripping: what the notes
    mask would hide never reaches `note_link` either."""
    await _seed(sessionmaker)
    vault = _lens_vault()
    vault.notes["Lens/Concepts/Requisite variety.md"] = (
        "lens",
        ASHBY + "\nWrite to [[maria@example.org]].\n",
    )
    await _pass(sessionmaker, vault)
    async with sessionmaker() as session:
        texts = (await session.execute(select(NoteLink.unresolved_text))).scalars().all()
    assert [t for t in texts if t] == ["Viable system model"]


def _replace_chunks_failing_on(monkeypatch, failing: set[str]) -> None:
    real = sync_module.notes_knowledge.replace_chunks

    async def replace_chunks(session, file_id, chunks):
        if any(title in str(chunks) for title in failing):
            # A real database error, so the pass's own rollback runs.
            await session.execute(text("select 1/0"))
        return await real(session, file_id, chunks)

    monkeypatch.setattr(sync_module.notes_knowledge, "replace_chunks", replace_chunks)


async def test_one_failing_note_is_skipped_and_the_pass_still_completes(sessionmaker, monkeypatch):
    """A per-note rollback expires every ORM row in the session; the pass
    must not read one afterwards (a new note's rolled-back row, or a
    tracked note sorted after the failure)."""
    await _seed(sessionmaker)
    vault = _lens_vault()
    _replace_chunks_failing_on(monkeypatch, {"Wrote on"})
    result = await _pass(sessionmaker, vault)
    assert (result.indexed, result.skipped) == (1, 1)
    assert set(await _lens_rows(sessionmaker)) == {"Lens/Concepts/Requisite variety.md"}
    # Links are refreshed for what did commit; the failed note's are not.
    assert await _links(sessionmaker) == [
        ("Lens/Concepts/Requisite variety.md", None, "Viable system model", False)
    ]
    assert len(await _versions(sessionmaker)) == 1

    # Next pass: everything indexed; then an edit to the first note fails
    # while the second, tracked and unchanged, sorts after it.
    monkeypatch.undo()
    assert (await _pass(sessionmaker, vault)).indexed == 1
    vault.notes["Lens/Concepts/Requisite variety.md"] = ("lens", ASHBY + "\nOne more line.\n")
    _replace_chunks_failing_on(monkeypatch, {"One more line"})
    vault.summaries["Lens/People/Beer.md"] = "Management cybernetics."
    result = await _pass(sessionmaker, vault)
    assert (result.indexed, result.skipped) == (0, 1)
    assert (await _lens_rows(sessionmaker))["Lens/People/Beer.md"].summary == "Management cybernetics."
    assert len(await _links(sessionmaker)) == 3


async def test_a_truncated_graph_keeps_what_it_did_not_report(sessionmaker):
    await _seed(sessionmaker)
    vault = _lens_vault()
    await _pass(sessionmaker, vault)
    links_before = await _links(sessionmaker)
    assert len(links_before) == 3

    # vaultd's cap cut Beer off: no node, no edge out of it or into it.
    vault.graph = Graph(
        nodes=[
            GraphNode(
                path="Lens/Concepts/Requisite variety.md", title="Requisite variety", note_class="lens",
                lens_kind="concept", aliases=(), tags=(), summary=None, chars=10,
            )
        ],
        edges=[GraphEdge(src="Lens/Concepts/Requisite variety.md", unresolved="Viable system model")],
        truncated=True,
    )
    vault.notes["Lens/People/Beer.md"] = ("lens", BEER + "\nEdited.\n")
    await _pass(sessionmaker, vault)
    assert await _links(sessionmaker) == links_before
    beer = (await _lens_rows(sessionmaker))["Lens/People/Beer.md"]
    assert "Edited." in beer.body
    assert beer.summary == "Cybernetician of management."


async def test_a_refused_graph_skips_links_but_not_indexing(sessionmaker):
    await _seed(sessionmaker)
    vault = _lens_vault()
    await _pass(sessionmaker, vault)
    links_before = await _links(sessionmaker)

    vault.graph_error = VaultError(errors.NOT_FOUND)
    vault.notes["Library/New.md"] = ("knowledge", "# New\n\nText.")
    result = await _pass(sessionmaker, vault)
    assert result.unavailable is False
    assert result.indexed == 1
    assert await _links(sessionmaker) == links_before
    # Summaries keep last pass's value rather than being wiped.
    beer = (await _lens_rows(sessionmaker))["Lens/People/Beer.md"]
    assert beer.summary == "Cybernetician of management."


async def test_an_unreachable_graph_stops_the_pass_like_any_request(sessionmaker):
    await _seed(sessionmaker)
    vault = _lens_vault()
    vault.graph_error = VaultError(errors.UNAVAILABLE)
    result = await _pass(sessionmaker, vault)
    assert result.unavailable is True


# --- versions -----------------------------------------------------------------------


def test_version_hash_ignores_order():
    a = lens.version_hash(["h1", "h2"], [("A", "B"), ("B", "A")])
    b = lens.version_hash(["h2", "h1"], [("B", "A"), ("A", "B")])
    assert a == b
    assert a != lens.version_hash(["h1", "h2"], [("A", "B")])
    assert a != lens.version_hash(["h1", "h3"], [("A", "B"), ("B", "A")])


async def test_a_version_is_recorded_only_when_the_lens_changes(sessionmaker):
    await _seed(sessionmaker)
    vault = _lens_vault()
    first = await _pass(sessionmaker, vault)
    assert first.lens_changed is True
    [version] = await _versions(sessionmaker)
    assert version.note_count == 2

    second = await _pass(sessionmaker, vault)
    assert second.lens_changed is False
    assert len(await _versions(sessionmaker)) == 1

    # A knowledge-only change is not a lens change.
    vault.notes["Library/CCRU.md"] = ("knowledge", "# CCRU\n\nText.")
    third = await _pass(sessionmaker, vault)
    assert third.lens_changed is False

    # A lens-to-lens link removed is.
    vault.notes["Lens/People/Beer.md"] = ("lens", "# Beer\n\nNo links now.\n")
    fourth = await _pass(sessionmaker, vault)
    assert fourth.lens_changed is True
    assert len(await _versions(sessionmaker)) == 2


async def test_no_version_while_the_lens_is_off(sessionmaker):
    await _seed(sessionmaker)
    await _pass(sessionmaker, _lens_vault(), _settings(lens_on=False))
    assert await _versions(sessionmaker) == []

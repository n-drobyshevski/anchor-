"""Claude Code's door to the lens, asserted in SQL (anchor-lens-plan.md section 11).

Migration e4c7a2d9b1f3 creates `anchor_lens` NOLOGIN, a `lens` schema of
SECURITY DEFINER functions, and content-free debug views over the lens
tables. The role boundary is what must hold even if every client-side
guard is bypassed, so, like tests/test_debug_views.py, this runs as the
role itself (`set local role`) against the throwaway database:

- `anchor_lens` can call `lens.notes()` and `lens.graph()`, and each
  call leaves a `lens_read` row with its name and row count;
- it can select nothing else: no lens table, no `public` table, no
  `debug` view;
- `lens.graph()` returns lens-to-lens edges and unresolved targets out
  of lens notes only -- never a knowledge-only note, never an outside
  link;
- `anchor_debug` reads the lens views, which carry no title, summary,
  body or link target, and may not call the lens functions.

All notes are synthetic.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.db.models import LensNote, NoteLink, VaultFile
from app.vault import lens
from tests.test_vault_notes_migration import _alembic, _run, scratch_database  # noqa: F401

BEFORE = "8b4f1d3a9e26"
AFTER = "e4c7a2d9b1f3"

SECRET_TITLE = "Секретная концепция"
SECRET_BODY = "Тело заметки линзы, которое видит только anchor_lens."
SECRET_SUMMARY = "Краткое содержание."
SECRET_TARGET = "Ненаписанная заметка"


async def _seed(sessionmaker) -> dict[str, int]:
    """Two lens notes, one knowledge-only note, and every kind of link."""
    async with sessionmaker() as session:
        files = {
            name: VaultFile(path=f"Lens/{name}.md", role="note", note_class="knowledge")
            for name in (SECRET_TITLE, "Beer", "CCRU")
        }
        session.add_all(files.values())
        await session.flush()
        ids = {name: row.id for name, row in files.items()}
        session.add_all(
            [
                LensNote(
                    vault_file_id=ids[SECRET_TITLE], kind="concept", title=SECRET_TITLE,
                    summary=SECRET_SUMMARY, body=SECRET_BODY, body_hash="a" * 64,
                    chars=len(SECRET_BODY),
                ),
                LensNote(
                    vault_file_id=ids["Beer"], kind="person", title="Beer", summary=None,
                    body="Beer.", body_hash="b" * 64, chars=5,
                ),
                # lens -> lens
                NoteLink(src_file_id=ids[SECRET_TITLE], dst_file_id=ids["Beer"]),
                # lens -> unresolved
                NoteLink(src_file_id=ids[SECRET_TITLE], unresolved_text=SECRET_TARGET),
                # lens -> outside
                NoteLink(src_file_id=ids["Beer"], outside=True),
                # lens -> knowledge-only, and knowledge-only -> anything
                NoteLink(src_file_id=ids["Beer"], dst_file_id=ids["CCRU"]),
                NoteLink(src_file_id=ids["CCRU"], dst_file_id=ids["Beer"]),
                NoteLink(src_file_id=ids["CCRU"], unresolved_text="Knowledge-only target"),
            ]
        )
        await session.commit()
    return ids


async def _as(sessionmaker, role: str, sql: str):
    async with sessionmaker() as session:
        await session.execute(text(f"set local role {role}"))
        rows = (await session.execute(text(sql))).all()
        await session.commit()
        return rows


async def test_the_role_exists_nologin(sessionmaker):
    async with sessionmaker() as session:
        row = (
            await session.execute(
                text("select rolcanlogin, rolsuper, rolcreaterole from pg_roles where rolname = 'anchor_lens'")
            )
        ).one()
    assert tuple(row) == (False, False, False)


async def test_lens_role_reads_notes_and_each_call_is_counted(sessionmaker):
    await _seed(sessionmaker)
    rows = await _as(sessionmaker, "anchor_lens", "select id, kind, title, summary, body, chars from lens.notes()")
    assert [(r.kind, r.title) for r in rows] == [("person", "Beer"), ("concept", SECRET_TITLE)]
    secret = rows[1]
    assert (secret.summary, secret.body, secret.chars) == (SECRET_SUMMARY, SECRET_BODY, len(SECRET_BODY))

    await _as(sessionmaker, "anchor_lens", "select * from lens.graph()")
    async with sessionmaker() as session:
        reads = (await session.execute(text("select fn, rows from lens_read order by id"))).all()
    assert [tuple(r) for r in reads] == [("notes", 2), ("graph", 2)]


async def test_graph_is_lens_to_lens_and_unresolved_only(sessionmaker):
    await _seed(sessionmaker)
    rows = await _as(sessionmaker, "anchor_lens", "select src_title, dst_title, unresolved from lens.graph()")
    assert [tuple(r) for r in rows] == [
        (SECRET_TITLE, "Beer", None),
        (SECRET_TITLE, None, SECRET_TARGET),
    ]
    assert "CCRU" not in repr(rows)
    assert "Knowledge-only target" not in repr(rows)


async def test_a_rolled_back_read_leaves_a_gap_that_is_counted(sessionmaker):
    """The `lens_read` row is in the caller's transaction, so a rollback
    takes it back -- but not the id the call took first from the
    table's sequence. The gap is the read's trace."""
    await _seed(sessionmaker)
    for undo in ("rollback", "savepoint"):
        async with sessionmaker() as session:
            await session.execute(text("set local role anchor_lens"))
            if undo == "savepoint":
                await session.execute(text("savepoint s"))
            rows = (await session.execute(text("select body from lens.notes()"))).all()
            assert SECRET_BODY in repr(rows)
            if undo == "savepoint":
                await session.execute(text("rollback to savepoint s"))
                await session.commit()
            else:
                await session.rollback()
    async with sessionmaker() as session:
        assert (await session.execute(text("select count(*) from lens_read"))).scalar_one() == 0
        assert await lens.unrecorded_reads(session) == 2

    # A recorded read after them dates the gap for the digest's window.
    await _as(sessionmaker, "anchor_lens", "select * from lens.graph()")
    async with sessionmaker() as session:
        now = (await session.execute(text("select now()"))).scalar_one()
        day = datetime.timedelta(days=1)
        assert await lens.unrecorded_between(session, now - day, now + day) == 2
        assert await lens.unrecorded_between(session, now + day, now + 2 * day) == 0
        assert await lens.unrecorded_reads(session) == 2


async def test_an_empty_lens_still_counts_the_read(sessionmaker):
    assert await _as(sessionmaker, "anchor_lens", "select * from lens.notes()") == []
    async with sessionmaker() as session:
        reads = (await session.execute(text("select fn, rows from lens_read"))).all()
    assert [tuple(r) for r in reads] == [("notes", 0)]


@pytest.mark.parametrize(
    "table",
    [
        "public.lens_note",
        "public.note_link",
        "public.lens_read",
        "public.lens_version",
        "public.lens_round",
        "public.review_proposal",
        "public.vault_file",
        "public.message",
        "public.memory",
        "public.note_chunk_knowledge",
        "public.note_chunk_personal",
        "debug.lens_note",
        "debug.note_link",
        "debug.lens_round",
        "debug.review_proposal",
        "debug.message",
        "debug.vault_file",
    ],
)
async def test_lens_role_can_select_nothing_else(sessionmaker, table):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        await session.execute(text("set local role anchor_lens"))
        with pytest.raises(ProgrammingError, match="permission denied"):
            await session.execute(text(f"select * from {table}"))


async def test_lens_role_cannot_write_its_own_log(sessionmaker):
    async with sessionmaker() as session:
        await session.execute(text("set local role anchor_lens"))
        with pytest.raises(ProgrammingError, match="permission denied"):
            await session.execute(text("insert into public.lens_read (fn, rows) values ('x', 0)"))


async def test_lens_role_has_no_grant_outside_its_schema(sessionmaker):
    """The catalogue agrees with the probes above: every table privilege
    and every routine privilege anchor_lens holds, listed."""
    async with sessionmaker() as session:
        tables = (
            await session.execute(
                text(
                    "select table_schema, table_name from information_schema.role_table_grants "
                    "where grantee = 'anchor_lens'"
                )
            )
        ).all()
        routines = (
            await session.execute(
                text(
                    "select routine_schema, routine_name from information_schema.role_routine_grants "
                    "where grantee = 'anchor_lens' order by routine_name"
                )
            )
        ).all()
    assert tables == []
    # L2 (c6d2e8a4f917) adds lens.rounds(); tests/test_lens_round_db.py.
    assert [tuple(r) for r in routines] == [("lens", "graph"), ("lens", "notes"), ("lens", "rounds")]


async def test_the_functions_are_not_public(sessionmaker):
    await _seed(sessionmaker)
    for fn in ("lens.notes()", "lens.graph()"):
        async with sessionmaker() as session:
            await session.execute(text("set local role anchor_debug"))
            with pytest.raises(ProgrammingError, match="permission denied"):
                await session.execute(text(f"select * from {fn}"))


async def test_debug_views_carry_no_lens_text(sessionmaker):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        await session.execute(text("insert into lens_version (hash, note_count) values (:h, 2)"), {"h": "c" * 64})
        await session.execute(text("insert into lens_read (fn, rows) values ('notes', 2)"))
        await session.commit()
    for view, expected in (("lens_note", 2), ("note_link", 6), ("lens_version", 1), ("lens_read", 1)):
        rows = await _as(sessionmaker, "anchor_debug", f"select * from debug.{view}")
        assert len(rows) == expected, view
        dumped = repr(rows)
        for secret in (SECRET_TITLE, SECRET_BODY, SECRET_SUMMARY, SECRET_TARGET, "Knowledge-only target", ".md"):
            assert secret not in dumped, (view, secret)
    unresolved = await _as(
        sessionmaker, "anchor_debug", "select count(*) filter (where unresolved), count(*) filter (where outside) from debug.note_link"
    )
    assert tuple(unresolved[0]) == (2, 1)


async def test_note_link_allows_exactly_one_target(sessionmaker):
    from sqlalchemy.exc import IntegrityError

    ids = await _seed(sessionmaker)
    for kwargs in (
        {},
        {"dst_file_id": ids["Beer"], "outside": True},
        {"unresolved_text": "x", "outside": True},
        {"dst_file_id": ids["Beer"], "unresolved_text": "x"},
    ):
        async with sessionmaker() as session:
            session.add(NoteLink(src_file_id=ids["CCRU"], **kwargs))
            with pytest.raises(IntegrityError, match="ck_note_link_one_target"):
                await session.commit()


async def test_lens_rows_cascade_with_their_file(sessionmaker):
    ids = await _seed(sessionmaker)
    async with sessionmaker() as session:
        await session.execute(text("delete from vault_file where id = :id"), {"id": ids[SECRET_TITLE]})
        await session.commit()
        notes = (await session.execute(text("select title from lens_note"))).scalars().all()
        links = (await session.execute(text("select count(*) from note_link"))).scalar_one()
    assert notes == ["Beer"]
    assert links == 4


# --- the migration itself ---------------------------------------------------------


def test_migration_upgrades_and_downgrades_cleanly(scratch_database):  # noqa: F811
    _alembic(scratch_database, "upgrade", AFTER)
    tables = {
        r["table_name"]
        for r in _run(
            scratch_database,
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'",
        )
    }
    assert {"lens_note", "note_link", "lens_version", "lens_read"} <= tables
    views = {
        r["table_name"]
        for r in _run(
            scratch_database,
            "SELECT table_name FROM information_schema.views WHERE table_schema = 'debug'",
        )
    }
    assert {"lens_note", "note_link", "lens_version", "lens_read"} <= views
    functions = {
        r["proname"]
        for r in _run(
            scratch_database,
            "SELECT p.proname, p.prosecdef FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'lens' AND p.prosecdef",
        )
    }
    assert functions == {"notes", "graph"}

    _alembic(scratch_database, "downgrade", BEFORE)
    tables = {
        r["table_name"]
        for r in _run(
            scratch_database,
            "SELECT table_name FROM information_schema.tables WHERE table_schema IN ('public', 'debug')",
        )
    }
    assert not {"lens_note", "note_link", "lens_version", "lens_read"} & tables
    schemas = {r["nspname"] for r in _run(scratch_database, "SELECT nspname FROM pg_namespace")}
    assert "lens" not in schemas
    assert "debug" in schemas
    _alembic(scratch_database, "upgrade", "head")

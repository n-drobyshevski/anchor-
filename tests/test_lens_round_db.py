"""L2's data layer: `lens_round`, the review's lens reads, and `lens.rounds()`.

anchor-lens-plan.md sections 5, 7 and 11, milestone L2. Migration
c6d2e8a4f917 adds `lens_round`, review_proposal's `lens_round_id` and
`lens_note_ids`, two content-free debug views and a third SECURITY
DEFINER function for Claude Code's door. app/vault/lens.py gains what
the review's selector (app/core/lens_review.py) reads and writes. This
file asserts, against the throwaway database:

- `anchor_lens` may call `lens.rounds(n)`, which logs its read like the
  other two and resolves the selected ids to current titles, never
  returns the rationale (written from the week, so it stays with the
  user), and still may not select `lens_round` itself; nobody else may
  call it;
- `debug.lens_round` has no `rationale`, `debug.review_proposal` has
  the two new columns and no proposal text;
- the catalog is lens notes only, with lens-to-lens links both ways and
  a correct `rounds_since_used`;
- the round's own writes, and the two settings' bounds.

L5 (migration 3d3efa0cbc9a, the L5 spec section 2) adds, here too:

- `reflect` is a consumer (`critique` is not), and each consumer keeps
  to its own link (`ck_lens_round_link`, and `record_round`'s own
  check), with no rationale on a reflect round;
- `lens_round.idle_run_id` and `notebook_entry.lens_round_id` are SET
  NULL, and `notebook_entry.lens_note_ids` defaults to `'{}'`;
- rotation (`rounds_since_used`) is per consumer, in both directions,
  and `last_round` answers per consumer;
- `lens.rounds(n)` shows reflect rounds, and `debug.lens_round` still
  leaves the new column out;
- /delete takes a reflect round an entry and an idle run point at;
- the migration upgrades and downgrades cleanly (reflect rounds go).

All notes are synthetic.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from app.config import Settings, check_vault_settings
from app.db.models import (
    IdleRun,
    LensNote,
    LensRound,
    LensVersion,
    NotebookEntry,
    NoteLink,
    ReviewProposal,
    UserState,
    VaultFile,
    WeeklyReview,
)
from app.vault import lens
from tests.test_vault_notes_migration import _alembic, _run, scratch_database  # noqa: F401

BEFORE = "e4c7a2d9b1f3"
AFTER = "c6d2e8a4f917"
L5_BEFORE = "e9a4c2f7b1d8"
L5_AFTER = "3d3efa0cbc9a"

RATIONALE = "Неделя однообразных ответов: Эшби о необходимом разнообразии."
PROPOSAL_TEXT = "Отвечать по-разному на разные дни"
PROPOSAL_REASON = "Одинаковые ответы не работали"


async def _note(session, title: str, *, body: str = "", summary: str | None = None, kind: str = "concept") -> LensNote:
    file = VaultFile(path=f"Lens/{title}.md", role="note", note_class="knowledge")
    session.add(file)
    await session.flush()
    note = LensNote(
        vault_file_id=file.id, kind=kind, title=title, summary=summary, body=body or f"{title}.",
        body_hash=title.ljust(64, "x")[:64], chars=len(body or f"{title}."),
    )
    session.add(note)
    await session.flush()
    return note


async def _seed(sessionmaker) -> dict[str, LensNote]:
    """Ashby -> Beer, Wiener -> Ashby, Beer -> CCRU (knowledge only),
    CCRU -> Wiener, and each odd link shape out of Ashby."""
    async with sessionmaker() as session:
        notes = {
            "Ashby": await _note(session, "Ashby", summary="  Необходимое\n разнообразие  "),
            "Beer": await _note(session, "Beer", kind="person", body="Стаффорд   Бир.\n\n" + "я" * 400),
            "Wiener": await _note(session, "Wiener"),
        }
        ccru = VaultFile(path="Library/CCRU.md", role="note", note_class="knowledge")
        session.add(ccru)
        await session.flush()
        f = {name: note.vault_file_id for name, note in notes.items()}
        session.add_all(
            [
                NoteLink(src_file_id=f["Ashby"], dst_file_id=f["Beer"]),
                NoteLink(src_file_id=f["Wiener"], dst_file_id=f["Ashby"]),
                NoteLink(src_file_id=f["Ashby"], dst_file_id=f["Ashby"]),
                NoteLink(src_file_id=f["Ashby"], unresolved_text="Ненаписанная"),
                NoteLink(src_file_id=f["Ashby"], outside=True),
                NoteLink(src_file_id=f["Beer"], dst_file_id=ccru.id),
                NoteLink(src_file_id=ccru.id, dst_file_id=f["Wiener"]),
            ]
        )
        await session.commit()
        return notes


async def _round(sessionmaker, ids: list[int], outcome: str = "grounded", rationale: str | None = RATIONALE) -> int:
    async with sessionmaker() as session:
        round_id = await lens.record_round(
            session, selected_note_ids=ids, rationale=rationale, outcome=outcome
        )
        await session.commit()
        return round_id


async def _idle_run(sessionmaker) -> int:
    async with sessionmaker() as session:
        run = IdleRun(kind="reflect", local_date=datetime.date(2026, 9, 30), status="done")
        session.add(run)
        await session.commit()
        return run.id


async def _reflect(sessionmaker, ids: list[int], outcome: str = "grounded", idle_run_id: int | None = None) -> int:
    async with sessionmaker() as session:
        round_id = await lens.record_round(
            session, selected_note_ids=ids, rationale=None, outcome=outcome,
            consumer="reflect", idle_run_id=idle_run_id,
        )
        await session.commit()
        return round_id


async def _as(sessionmaker, role: str, sql: str):
    async with sessionmaker() as session:
        await session.execute(text(f"set local role {role}"))
        rows = (await session.execute(text(sql))).all()
        await session.commit()
        return rows


# --- settings -------------------------------------------------------------------


def test_round_settings_default_to_the_plan():
    settings = Settings()
    assert (settings.LENS_ROUND_MAX_NOTES, settings.LENS_ROUND_MAX_CHARS) == (6, 24000)
    check_vault_settings(settings)


@pytest.mark.parametrize(
    ("name", "value", "ok"),
    [
        ("LENS_ROUND_MAX_NOTES", 0, False),
        ("LENS_ROUND_MAX_NOTES", 1, True),
        ("LENS_ROUND_MAX_NOTES", 12, True),
        ("LENS_ROUND_MAX_NOTES", 13, False),
        ("LENS_ROUND_MAX_CHARS", 1999, False),
        ("LENS_ROUND_MAX_CHARS", 2000, True),
        ("LENS_ROUND_MAX_CHARS", 100000, True),
        ("LENS_ROUND_MAX_CHARS", 100001, False),
    ],
)
def test_round_settings_are_bounded(name, value, ok):
    settings = Settings(**{name: value})
    if ok:
        check_vault_settings(settings)
    else:
        with pytest.raises(SystemExit, match=f"{name} must be between"):
            check_vault_settings(settings)


# --- lens_active ------------------------------------------------------------------


async def test_the_lens_is_active_only_when_on_and_within_the_catalog_cap(sessionmaker):
    async with sessionmaker() as session:
        assert not await lens.lens_active(session, Settings(LENS_ENABLED=True))
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        assert not await lens.lens_active(session, Settings(LENS_ENABLED=False))
        assert await lens.lens_active(session, Settings(LENS_ENABLED=True))
        assert await lens.lens_active(session, Settings(LENS_ENABLED=True, LENS_CATALOG_MAX_NOTES=3))
        assert not await lens.lens_active(session, Settings(LENS_ENABLED=True, LENS_CATALOG_MAX_NOTES=2))
        assert await lens.note_count(session) == 3


# --- the catalog ------------------------------------------------------------------


async def test_catalog_is_lens_notes_with_lens_links_both_ways(sessionmaker):
    notes = await _seed(sessionmaker)
    async with sessionmaker() as session:
        entries = await lens.catalog(session)
    assert [(e.title, e.kind) for e in entries] == [
        ("Ashby", "concept"), ("Beer", "person"), ("Wiener", "concept")
    ]
    by_title = {e.title: e for e in entries}
    assert by_title["Ashby"].id == notes["Ashby"].id
    # Out (Beer) and in (Wiener); not itself, not the unresolved target.
    assert by_title["Ashby"].links == ("Beer", "Wiener")
    # Beer -> CCRU and CCRU -> Wiener go through a knowledge-only note:
    # no edge, and CCRU is named nowhere.
    assert by_title["Beer"].links == ("Ashby",)
    assert by_title["Wiener"].links == ("Ashby",)
    assert "CCRU" not in repr(entries)
    assert "Ненаписанная" not in repr(entries)


async def test_catalog_summary_is_the_summary_or_the_start_of_the_body(sessionmaker):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        by_title = {e.title: e for e in await lens.catalog(session)}
    assert by_title["Ashby"].summary == "Необходимое разнообразие"
    beer = by_title["Beer"].summary
    assert len(beer) == lens.SUMMARY_FALLBACK_CHARS
    assert beer.startswith("Стаффорд Бир. яяя")
    assert by_title["Wiener"].summary == "Wiener."


async def test_rounds_since_used_counts_review_rounds_after_the_last_pick(sessionmaker):
    notes = await _seed(sessionmaker)
    ashby, beer, wiener = (notes[t].id for t in ("Ashby", "Beer", "Wiener"))

    async with sessionmaker() as session:
        assert {e.title: e.rounds_since_used for e in await lens.catalog(session)} == {
            "Ashby": None, "Beer": None, "Wiener": None
        }

    await _round(sessionmaker, [ashby, beer])
    await _round(sessionmaker, [ashby])
    await _round(sessionmaker, [], outcome="empty")
    await _round(sessionmaker, [ashby], outcome="fallback")

    async with sessionmaker() as session:
        since = {e.title: e.rounds_since_used for e in await lens.catalog(session)}
    # Ashby: picked in the latest round. Beer: three rounds since (the
    # empty and the fallback round count too). Wiener: never.
    assert since == {"Ashby": 0, "Beer": 3, "Wiener": None}
    assert wiener not in (ashby, beer)


# --- bodies, titles, the round ----------------------------------------------------


async def test_bodies_keep_the_given_order_and_skip_unknown_ids(sessionmaker):
    notes = await _seed(sessionmaker)
    ashby, beer, wiener = (notes[t].id for t in ("Ashby", "Beer", "Wiener"))
    async with sessionmaker() as session:
        got = await lens.bodies(session, [wiener, 9999, ashby, wiener])
        assert [(b.id, b.title, b.body, b.chars) for b in got] == [
            (wiener, "Wiener", "Wiener.", 7),
            (ashby, "Ashby", "Ashby.", 6),
        ]
        assert await lens.bodies(session, []) == []
        assert await lens.titles_for(session, [beer, 9999, ashby]) == ["Beer", "Ashby"]
        assert await lens.titles_for(session, None) == []


async def _record_version(sessionmaker) -> int:
    """Record the lens as it is now; the id of its version row."""
    async with sessionmaker() as session:
        await lens.record_version(session)
        await session.commit()
        current, _count = await lens._current_version(session)
        return (
            await session.execute(text("select id from lens_version where hash = :h"), {"h": current})
        ).scalar_one()


async def _set_body_hash(sessionmaker, note_id: int, body_hash: str) -> None:
    async with sessionmaker() as session:
        await session.execute(
            text("update lens_note set body_hash = :h where id = :i"), {"h": body_hash, "i": note_id}
        )
        await session.commit()


async def test_record_round_points_at_the_version_of_the_current_lens(sessionmaker):
    notes = await _seed(sessionmaker)
    ashby = notes["Ashby"].id
    none_yet = await _round(sessionmaker, [ashby])
    async with sessionmaker() as session:
        # A version that is not the current lens is never picked, however new.
        session.add(LensVersion(hash="f" * 64, note_count=9))
        await session.commit()
    no_match = await _round(sessionmaker, [ashby])
    version_a = await _record_version(sessionmaker)
    at_a = await _round(sessionmaker, [ashby], outcome="grounded")

    async with sessionmaker() as session:
        rows = {
            r.id: r
            for r in (await session.execute(text("select * from lens_round"))).mappings()
        }
    assert rows[none_yet]["lens_version_id"] is None
    assert rows[no_match]["lens_version_id"] is None
    assert rows[at_a]["lens_version_id"] == version_a
    assert rows[at_a]["consumer"] == "review"
    assert rows[at_a]["selected_note_ids"] == [ashby]
    assert rows[at_a]["weekly_review_id"] is None


async def test_a_lens_back_at_an_earlier_state_records_that_states_version(sessionmaker):
    """A -> B -> A: the lens's return to A adds no `lens_version` row (one
    row per distinct hash), so the newest row stays B's; the round must
    still record A's, the version it ran against."""
    notes = await _seed(sessionmaker)
    ashby = notes["Ashby"]
    original = ashby.body_hash
    version_a = await _record_version(sessionmaker)
    await _set_body_hash(sessionmaker, ashby.id, "e" * 64)
    version_b = await _record_version(sessionmaker)
    at_b = await _round(sessionmaker, [ashby.id])
    await _set_body_hash(sessionmaker, ashby.id, original)
    assert await _record_version(sessionmaker) == version_a
    back_at_a = await _round(sessionmaker, [ashby.id])

    assert version_a != version_b
    async with sessionmaker() as session:
        rows = dict((await session.execute(text("select id, lens_version_id from lens_round"))).all())
        newest = (
            await session.execute(text("select id from lens_version order by created_at desc, id desc limit 1"))
        ).scalar_one()
    assert newest == version_b
    assert rows[at_b] == version_b
    assert rows[back_at_a] == version_a


async def test_record_round_refuses_unknown_outcomes_and_consumers(sessionmaker):
    """L5: `reflect` joins `review`; `critique` records ids elsewhere
    (idle_run.summary) and never a round."""
    async with sessionmaker() as session:
        with pytest.raises(ValueError):
            await lens.record_round(session, selected_note_ids=[], rationale=None, outcome="maybe")
        with pytest.raises(ValueError):
            await lens.record_round(
                session, selected_note_ids=[], rationale=None, outcome="empty", consumer="critique"
            )
    assert lens.ROUND_CONSUMERS == ("review", "reflect")
    # And the table's own CHECKs, under the module.
    for kwargs in ({"consumer": "critique", "outcome": "empty"}, {"consumer": "review", "outcome": "maybe"}):
        async with sessionmaker() as session:
            session.add(LensRound(**kwargs))
            with pytest.raises(IntegrityError, match="ck_lens_round_"):
                await session.commit()
    async with sessionmaker() as session:
        session.add(LensRound(consumer="reflect", outcome="empty"))
        await session.commit()


async def test_attach_rationale_and_last_round(sessionmaker):
    async with sessionmaker() as session:
        assert await lens.last_round(session) is None
    first = await _round(sessionmaker, [], outcome="empty", rationale=None)
    second = await _round(sessionmaker, [], outcome="fallback")
    async with sessionmaker() as session:
        review = WeeklyReview(week_start=datetime.date(2026, 9, 21), analysis={})
        session.add(review)
        await session.flush()
        await lens.attach_round_to_review(session, second, review.id)
        await session.commit()
        attached = (
            await session.execute(text("select weekly_review_id from lens_round where id = :i"), {"i": second})
        ).scalar_one()
        assert attached == review.id
        assert await lens.round_rationale(session, second) == RATIONALE
        assert await lens.round_rationale(session, first) is None
        assert await lens.round_rationale(session, 9999) is None
        last = await lens.last_round(session)
    assert (last.id, last.consumer, last.outcome) == (second, "review", "fallback")
    assert isinstance(last.created_at, datetime.datetime)


async def test_foreign_keys_cascade_and_set_null(sessionmaker):
    async with sessionmaker() as session:
        version = LensVersion(hash="c" * 64, note_count=1)
        review = WeeklyReview(week_start=datetime.date(2026, 9, 21), analysis={})
        session.add_all([version, review])
        await session.flush()
        kept = LensRound(consumer="review", lens_version_id=version.id, outcome="empty")
        tied = LensRound(consumer="review", weekly_review_id=review.id, outcome="grounded", selected_note_ids=[1])
        session.add_all([kept, tied])
        await session.flush()
        other = WeeklyReview(week_start=datetime.date(2026, 9, 14), analysis={})
        session.add(other)
        await session.flush()
        proposal = ReviewProposal(
            review_id=other.id, kind="persona_note", text=PROPOSAL_TEXT,
            lens_round_id=kept.id, lens_note_ids=[1, 2],
        )
        session.add(proposal)
        await session.commit()
        ids = (kept.id, tied.id, proposal.id, version.id, review.id)

    kept_id, tied_id, proposal_id, version_id, review_id = ids
    async with sessionmaker() as session:
        await session.execute(text("delete from lens_version where id = :i"), {"i": version_id})
        await session.execute(text("delete from weekly_review where id = :i"), {"i": review_id})
        await session.commit()
        rounds = (await session.execute(text("select id, lens_version_id from lens_round"))).all()
        assert [tuple(r) for r in rounds] == [(kept_id, None)]
        await session.execute(text("delete from lens_round where id = :i"), {"i": kept_id})
        await session.commit()
        row = (
            await session.execute(
                text("select lens_round_id, lens_note_ids from review_proposal where id = :i"),
                {"i": proposal_id},
            )
        ).one()
    assert tuple(row) == (None, [1, 2])
    assert tied_id != kept_id


# --- Claude Code's door: lens.rounds() ---------------------------------------------


async def test_lens_role_reads_rounds_with_current_titles_and_each_call_is_counted(sessionmaker):
    notes = await _seed(sessionmaker)
    ashby, beer, wiener = (notes[t].id for t in ("Ashby", "Beer", "Wiener"))
    older = await _round(sessionmaker, [wiener, 9999, ashby])
    newer = await _round(sessionmaker, [beer], outcome="empty", rationale=None)

    rows = await _as(sessionmaker, "anchor_lens", "select * from lens.rounds(10)")
    assert list(rows[0]._fields) == ["id", "consumer", "outcome", "created_at", "titles"]
    assert [(r.id, r.consumer, r.outcome, r.titles) for r in rows] == [
        (newer, "review", "empty", ["Beer"]),
        (older, "review", "grounded", ["Wiener", "Ashby"]),
    ]
    assert RATIONALE not in repr(rows)
    assert await _as(sessionmaker, "anchor_lens", "select id from lens.rounds(1)") == [(newer,)]
    assert await _as(sessionmaker, "anchor_lens", "select id from lens.rounds(-5)") == []
    assert await _as(sessionmaker, "anchor_lens", "select id from lens.rounds(null)") == []

    async with sessionmaker() as session:
        reads = (await session.execute(text("select fn, rows from lens_read order by id"))).all()
    assert [tuple(r) for r in reads] == [("rounds", 2), ("rounds", 1), ("rounds", 0), ("rounds", 0)]


async def test_lens_rounds_is_capped_at_fifty(sessionmaker):
    async with sessionmaker() as session:
        session.add_all([LensRound(consumer="review", outcome="empty") for _ in range(55)])
        await session.commit()
    rows = await _as(sessionmaker, "anchor_lens", "select id from lens.rounds(1000)")
    assert len(rows) == 50
    assert rows[0].id == 55


async def test_a_rolled_back_rounds_read_leaves_a_gap(sessionmaker):
    await _round(sessionmaker, [], outcome="empty")
    async with sessionmaker() as session:
        await session.execute(text("set local role anchor_lens"))
        rows = (await session.execute(text("select id from lens.rounds(5)"))).all()
        assert len(rows) == 1
        await session.rollback()
    async with sessionmaker() as session:
        assert (await session.execute(text("select count(*) from lens_read"))).scalar_one() == 0
        assert await lens.unrecorded_reads(session) == 1


@pytest.mark.parametrize("table", ["public.lens_round", "public.review_proposal", "debug.lens_round"])
async def test_lens_role_may_not_select_the_round_table(sessionmaker, table):
    await _round(sessionmaker, [], outcome="empty")
    async with sessionmaker() as session:
        await session.execute(text("set local role anchor_lens"))
        with pytest.raises(ProgrammingError, match="permission denied"):
            await session.execute(text(f"select * from {table}"))


async def test_lens_rounds_has_no_rationale_column_and_the_table_stays_closed(sessionmaker):
    """The rationale is model text written from the user's week: derived
    from conversation data, so it is for the user (the Telegram card)
    and never for Claude Code. `lens.rounds()` declares no such column,
    selecting it fails, and `anchor_lens` still cannot reach it through
    the table."""
    await _round(sessionmaker, [])
    async with sessionmaker() as session:
        columns = (
            await session.execute(
                text(
                    "select p.proargnames, p.proargmodes::text[]"
                    " from pg_proc p join pg_namespace n on n.oid = p.pronamespace"
                    " where n.nspname = 'lens' and p.proname = 'rounds'"
                )
            )
        ).one()
    names, modes = columns
    returned = [name for name, mode in zip(names, modes) if mode == "t"]
    assert returned == ["id", "consumer", "outcome", "created_at", "titles"]
    assert "rationale" not in names

    async with sessionmaker() as session:
        await session.execute(text("set local role anchor_lens"))
        with pytest.raises(ProgrammingError, match="rationale"):
            await session.execute(text("select rationale from lens.rounds(5)"))
    async with sessionmaker() as session:
        await session.execute(text("set local role anchor_lens"))
        with pytest.raises(ProgrammingError, match="permission denied"):
            await session.execute(text("select rationale from public.lens_round"))
    async with sessionmaker() as session:
        await session.execute(text("set local role anchor_lens"))
        with pytest.raises(ProgrammingError, match="permission denied"):
            await session.execute(text("select * from public.lens_round"))


async def test_nobody_else_may_call_lens_rounds(sessionmaker):
    async with sessionmaker() as session:
        await session.execute(text("set local role anchor_debug"))
        with pytest.raises(ProgrammingError, match="permission denied"):
            await session.execute(text("select * from lens.rounds(5)"))
    async with sessionmaker() as session:
        acl = (
            await session.execute(
                text(
                    "select p.prosecdef, p.proconfig, has_function_privilege('public', p.oid, 'execute')"
                    " from pg_proc p join pg_namespace n on n.oid = p.pronamespace"
                    " where n.nspname = 'lens' and p.proname = 'rounds'"
                )
            )
        ).one()
    assert acl[0] is True
    assert acl[1] == ["search_path=pg_catalog, public"]
    assert acl[2] is False


# --- debug views --------------------------------------------------------------------


async def test_debug_views_carry_no_rationale_and_no_proposal_text(sessionmaker):
    notes = await _seed(sessionmaker)
    ashby = notes["Ashby"].id
    round_id = await _round(sessionmaker, [ashby])
    async with sessionmaker() as session:
        review = WeeklyReview(week_start=datetime.date(2026, 9, 21), analysis={})
        session.add(review)
        await session.flush()
        session.add(
            ReviewProposal(
                review_id=review.id, kind="standing_order", text=PROPOSAL_TEXT,
                reason=PROPOSAL_REASON, lens_round_id=round_id, lens_note_ids=[ashby],
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        columns = {
            (t, c)
            for t, c in await session.execute(
                text(
                    "select table_name, column_name from information_schema.columns"
                    " where table_schema = 'debug' and table_name in ('lens_round', 'review_proposal')"
                )
            )
        }
    lens_round_cols = {c for t, c in columns if t == "lens_round"}
    proposal_cols = {c for t, c in columns if t == "review_proposal"}
    assert lens_round_cols == {
        "id", "consumer", "weekly_review_id", "lens_version_id", "selected_note_ids", "outcome", "created_at"
    }
    assert {"lens_round_id", "lens_note_ids"} <= proposal_cols
    assert not {"text", "reason"} & proposal_cols

    rounds = await _as(sessionmaker, "anchor_debug", "select * from debug.lens_round")
    proposals = await _as(
        sessionmaker, "anchor_debug", "select lens_round_id, lens_note_ids, text_len from debug.review_proposal"
    )
    assert [(r.id, r.selected_note_ids, r.outcome) for r in rounds] == [(round_id, [ashby], "grounded")]
    assert [tuple(p) for p in proposals] == [(round_id, [ashby], len(PROPOSAL_TEXT))]
    dumped = repr(rounds) + repr(proposals)
    for secret in (RATIONALE, PROPOSAL_TEXT, PROPOSAL_REASON, "Ashby"):
        assert secret not in dumped


# --- L5: the reflect round -----------------------------------------------------------


async def test_the_link_rule_holds_in_the_module_and_in_the_table(sessionmaker):
    """A review round never names an idle run and a reflect round never a
    weekly review (`ck_lens_round_link`); a reflect round stores no
    rationale (the L5 spec's deviation 3)."""
    run_id = await _idle_run(sessionmaker)
    async with sessionmaker() as session:
        review = WeeklyReview(week_start=datetime.date(2026, 9, 21), analysis={})
        session.add(review)
        await session.commit()
        review_id = review.id

    async with sessionmaker() as session:
        with pytest.raises(ValueError, match="idle run"):
            await lens.record_round(
                session, selected_note_ids=[], rationale=None, outcome="empty", idle_run_id=run_id
            )
        with pytest.raises(ValueError, match="weekly review"):
            await lens.record_round(
                session, selected_note_ids=[], rationale=None, outcome="empty",
                consumer="reflect", weekly_review_id=review_id,
            )
        with pytest.raises(ValueError, match="rationale"):
            await lens.record_round(
                session, selected_note_ids=[], rationale=RATIONALE, outcome="empty", consumer="reflect"
            )
        assert (await session.execute(text("select count(*) from lens_round"))).scalar_one() == 0

    for kwargs in (
        {"consumer": "review", "idle_run_id": run_id},
        {"consumer": "reflect", "weekly_review_id": review_id},
        {"consumer": "reflect", "weekly_review_id": review_id, "idle_run_id": run_id},
    ):
        async with sessionmaker() as session:
            session.add(LensRound(outcome="empty", **kwargs))
            with pytest.raises(IntegrityError, match="ck_lens_round_link"):
                await session.commit()

    # Attaching a reflect round to a review is refused by the table too.
    reflect_id = await _reflect(sessionmaker, [], outcome="empty", idle_run_id=run_id)
    async with sessionmaker() as session:
        with pytest.raises(IntegrityError, match="ck_lens_round_link"):
            await lens.attach_round_to_review(session, reflect_id, review_id)


async def test_a_reflect_round_records_its_run_and_outlives_it(sessionmaker):
    """SET NULL, not CASCADE (the L5 spec section 2): rotation is counted
    from rounds, and the rounds are the selection's audit trail."""
    notes = await _seed(sessionmaker)
    ashby = notes["Ashby"].id
    run_id = await _idle_run(sessionmaker)
    version = await _record_version(sessionmaker)
    round_id = await _reflect(sessionmaker, [ashby], idle_run_id=run_id)

    async with sessionmaker() as session:
        row = (
            await session.execute(text("select * from lens_round where id = :i"), {"i": round_id})
        ).mappings().one()
        assert (row["consumer"], row["idle_run_id"], row["weekly_review_id"]) == ("reflect", run_id, None)
        assert (row["selected_note_ids"], row["rationale"], row["lens_version_id"]) == ([ashby], None, version)
        await session.execute(text("delete from idle_run where id = :i"), {"i": run_id})
        await session.commit()
        kept = (
            await session.execute(text("select id, idle_run_id from lens_round"))
        ).all()
    assert [tuple(r) for r in kept] == [(round_id, None)]


async def test_notebook_entry_lens_columns_default_empty_and_the_round_is_set_null(sessionmaker):
    round_id = await _reflect(sessionmaker, [1, 2])
    async with sessionmaker() as session:
        plain = NotebookEntry(kind="open_thread", text="вернуться к плану", source="anchor")
        grounded = NotebookEntry(
            kind="open_thread", text="разнообразить вечерние ответы", source="anchor",
            lens_round_id=round_id, lens_note_ids=[2, 1],
        )
        session.add_all([plain, grounded])
        await session.flush()
        await session.execute(
            text("insert into notebook_entry (kind, text, source) values ('observation', 'сырой', 'anchor')")
        )
        await session.commit()
        ids = (plain.id, grounded.id)

    async with sessionmaker() as session:
        rows = {
            r.text: (r.lens_round_id, r.lens_note_ids)
            for r in await session.execute(text("select text, lens_round_id, lens_note_ids from notebook_entry"))
        }
        assert rows == {
            "вернуться к плану": (None, []),
            "разнообразить вечерние ответы": (round_id, [2, 1]),
            "сырой": (None, []),
        }
        with pytest.raises(IntegrityError, match="lens_note_ids"):
            await session.execute(
                text("update notebook_entry set lens_note_ids = null where id = :i"), {"i": ids[0]}
            )
    async with sessionmaker() as session:
        await session.execute(text("delete from lens_round where id = :i"), {"i": round_id})
        await session.commit()
        row = (
            await session.execute(
                text("select lens_round_id, lens_note_ids from notebook_entry where id = :i"), {"i": ids[1]}
            )
        ).one()
    assert tuple(row) == (None, [2, 1])


async def test_rotation_is_per_consumer_in_both_directions(sessionmaker):
    """Daily reflect rounds never age the weekly review's counts, and
    review rounds never age reflect's (the L5 spec section 2)."""
    notes = await _seed(sessionmaker)
    ashby, beer = notes["Ashby"].id, notes["Beer"].id

    await _round(sessionmaker, [ashby])
    await _reflect(sessionmaker, [beer])
    await _reflect(sessionmaker, [], outcome="empty")
    await _reflect(sessionmaker, [ashby], outcome="fallback")

    async def since(consumer: str) -> dict[str, int | None]:
        async with sessionmaker() as session:
            return {e.title: e.rounds_since_used for e in await lens.catalog(session, consumer=consumer)}

    # The review's own count: three reflect rounds later, Ashby is still
    # its latest pick, and Beer (reflect's) is new to it.
    assert await since("review") == {"Ashby": 0, "Beer": None, "Wiener": None}
    async with sessionmaker() as session:
        assert {e.title: e.rounds_since_used for e in await lens.catalog(session)} == {
            "Ashby": 0, "Beer": None, "Wiener": None
        }
    # Reflect's: Beer two of its rounds ago, Ashby its latest.
    assert await since("reflect") == {"Ashby": 0, "Beer": 2, "Wiener": None}

    await _round(sessionmaker, [], outcome="empty")
    await _round(sessionmaker, [beer])
    assert await since("review") == {"Ashby": 2, "Beer": 0, "Wiener": None}
    assert await since("reflect") == {"Ashby": 0, "Beer": 2, "Wiener": None}

    async with sessionmaker() as session:
        with pytest.raises(ValueError):
            await lens.catalog(session, consumer="critique")


async def test_last_round_answers_per_consumer(sessionmaker):
    async with sessionmaker() as session:
        assert await lens.last_round(session, consumer="review") is None
        assert await lens.last_round(session, consumer="reflect") is None
    review_id = await _round(sessionmaker, [], outcome="grounded")
    async with sessionmaker() as session:
        assert await lens.last_round(session, consumer="reflect") is None
    first = await _reflect(sessionmaker, [], outcome="fallback")
    second = await _reflect(sessionmaker, [], outcome="empty")
    async with sessionmaker() as session:
        newest = await lens.last_round(session)
        review_last = await lens.last_round(session, consumer="review")
        reflect_last = await lens.last_round(session, consumer="reflect")
        with pytest.raises(ValueError):
            await lens.last_round(session, consumer="critique")
    assert (newest.id, newest.consumer) == (second, "reflect")
    assert (review_last.id, review_last.consumer, review_last.outcome) == (review_id, "review", "grounded")
    assert (reflect_last.id, reflect_last.consumer, reflect_last.outcome) == (second, "reflect", "empty")
    assert first != second


async def test_lens_rounds_shows_reflect_rounds_and_debug_leaves_the_run_out(sessionmaker):
    notes = await _seed(sessionmaker)
    ashby, wiener = notes["Ashby"].id, notes["Wiener"].id
    run_id = await _idle_run(sessionmaker)
    review_id = await _round(sessionmaker, [ashby])
    reflect_id = await _reflect(sessionmaker, [wiener, ashby], idle_run_id=run_id)

    rows = await _as(sessionmaker, "anchor_lens", "select * from lens.rounds(10)")
    assert [(r.id, r.consumer, r.outcome, r.titles) for r in rows] == [
        (reflect_id, "reflect", "grounded", ["Wiener", "Ashby"]),
        (review_id, "review", "grounded", ["Ashby"]),
    ]
    debug = await _as(sessionmaker, "anchor_debug", "select * from debug.lens_round order by id")
    assert "idle_run_id" not in debug[0]._fields
    assert [(r.id, r.consumer) for r in debug] == [(review_id, "review"), (reflect_id, "reflect")]


async def test_delete_takes_a_reflect_round_an_entry_and_a_run_point_at(sessionmaker):
    """/delete's one TRUNCATE: notebook_entry -> lens_round -> idle_run
    are all in PURGED_TABLES, so no foreign key stops it."""
    from app.core import purge
    from app.core.clock import FrozenClock

    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=1))
        await session.commit()
    run_id = await _idle_run(sessionmaker)
    round_id = await _reflect(sessionmaker, [1], idle_run_id=run_id)
    async with sessionmaker() as session:
        session.add(
            NotebookEntry(
                kind="open_thread", text="разнообразить ответы", source="anchor",
                lens_round_id=round_id, lens_note_ids=[1],
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        await purge.delete_everything(
            session, Settings(), FrozenClock(datetime.datetime(2026, 9, 30, 12, 0, tzinfo=datetime.timezone.utc))
        )
        await session.commit()
    async with sessionmaker() as session:
        for table in ("lens_round", "notebook_entry", "idle_run"):
            assert (await session.execute(text(f"select count(*) from {table}"))).scalar_one() == 0, table


# --- the migration itself ------------------------------------------------------------


def test_migration_upgrades_and_downgrades_cleanly(scratch_database):  # noqa: F811
    _alembic(scratch_database, "upgrade", AFTER)
    tables = {
        r["table_name"]
        for r in _run(
            scratch_database,
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'",
        )
    }
    assert "lens_round" in tables
    proposal_cols = {
        r["column_name"]
        for r in _run(
            scratch_database,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'review_proposal'",
        )
    }
    assert {"lens_round_id", "lens_note_ids"} <= proposal_cols
    views = {
        r["table_name"]
        for r in _run(
            scratch_database,
            "SELECT table_name FROM information_schema.views WHERE table_schema = 'debug'",
        )
    }
    assert {"lens_round", "review_proposal"} <= views
    functions = {
        r["proname"]
        for r in _run(
            scratch_database,
            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'lens' AND p.prosecdef",
        )
    }
    assert functions == {"notes", "graph", "rounds"}

    _alembic(scratch_database, "downgrade", BEFORE)
    tables = {
        r["table_name"]
        for r in _run(
            scratch_database,
            "SELECT table_name FROM information_schema.tables WHERE table_schema IN ('public', 'debug')",
        )
    }
    assert "lens_round" not in tables
    proposal_cols = {
        r["column_name"]
        for r in _run(
            scratch_database,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'review_proposal'",
        )
    }
    assert not {"lens_round_id", "lens_note_ids"} & proposal_cols
    views = {
        r["table_name"]
        for r in _run(
            scratch_database,
            "SELECT table_name FROM information_schema.views WHERE table_schema = 'debug'",
        )
    }
    assert not {"lens_round", "review_proposal"} & views
    functions = {
        r["proname"]
        for r in _run(
            scratch_database,
            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'lens'",
        )
    }
    assert functions == {"notes", "graph"}
    _alembic(scratch_database, "upgrade", "head")


def test_l5_migration_upgrades_and_downgrades_cleanly(scratch_database):  # noqa: F811
    """3d3efa0cbc9a: the reflect consumer, `lens_round.idle_run_id`, the
    link check and the two notebook columns; downgrade deletes the
    reflect rounds and restores L2's constraint."""
    _alembic(scratch_database, "upgrade", L5_AFTER)
    _run(
        scratch_database,
        "INSERT INTO idle_run (kind, local_date, status) VALUES ('reflect', '2026-09-30', 'done')",
        "INSERT INTO lens_round (consumer, outcome, idle_run_id)"
        " SELECT 'reflect', 'grounded', id FROM idle_run",
        "INSERT INTO lens_round (consumer, outcome) VALUES ('review', 'empty')",
        "INSERT INTO notebook_entry (kind, text, source, lens_round_id, lens_note_ids)"
        " SELECT 'open_thread', 'т', 'anchor', id, '{3}' FROM lens_round WHERE consumer = 'reflect'",
        "INSERT INTO notebook_entry (kind, text, source) VALUES ('observation', 'о', 'anchor')",
    )
    entries = _run(scratch_database, "SELECT text, lens_round_id IS NOT NULL AS linked, lens_note_ids FROM notebook_entry ORDER BY id")
    assert [(r["text"], r["linked"], r["lens_note_ids"]) for r in entries] == [("т", True, [3]), ("о", False, [])]
    indexes = {
        r["indexname"]
        for r in _run(scratch_database, "SELECT indexname FROM pg_indexes WHERE tablename = 'lens_round'")
    }
    assert "ix_lens_round_idle_run_id" in indexes
    with pytest.raises(Exception, match="ck_lens_round_link"):
        _run(
            scratch_database,
            "INSERT INTO lens_round (consumer, outcome, idle_run_id) SELECT 'review', 'empty', id FROM idle_run",
        )

    _alembic(scratch_database, "downgrade", L5_BEFORE)
    rounds = _run(scratch_database, "SELECT consumer FROM lens_round")
    assert [r["consumer"] for r in rounds] == ["review"]
    columns = {
        (r["table_name"], r["column_name"])
        for r in _run(
            scratch_database,
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name IN ('lens_round', 'notebook_entry')",
        )
    }
    assert not {
        ("lens_round", "idle_run_id"), ("notebook_entry", "lens_round_id"), ("notebook_entry", "lens_note_ids")
    } & columns
    assert [r["text"] for r in _run(scratch_database, "SELECT text FROM notebook_entry ORDER BY id")] == ["т", "о"]
    with pytest.raises(Exception, match="ck_lens_round_consumer"):
        _run(scratch_database, "INSERT INTO lens_round (consumer, outcome) VALUES ('reflect', 'empty')")
    _alembic(scratch_database, "upgrade", "head")

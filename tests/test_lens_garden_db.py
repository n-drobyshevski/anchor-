"""L3's data layer: the garden's runs and gaps, aliases, and `lens.gaps()`.

anchor-lens-plan.md sections 5, 8 and 11, milestone L3, with the
owner's two amendments to the L3 spec: `lens.gaps()` shows `resolved`
(and L4's `researched`) as `closed`, and Telegram gets one message per
run, whose keyboard rows are tracked through `tg_message_id` on each
gap and `sent_*` on the run. Migration b3e9f5a1c7d2 adds the two
tables, `lens_note.aliases`, the `report` vault file, the `lens_garden`
idle kind, two debug views and a fourth SECURITY DEFINER function.
This file asserts, against the throwaway database:

- the schema's own rules: the partial unique index on live signatures,
  the CHECKs, the foreign keys, the new vault role and idle kind;
- app/vault/lens.py's garden API: the gate's facts, the graph view
  (knowledge notes anonymous), recording a run (dedup, resolve,
  reopen), the one message (unsent, sent, re-rendered after a tap),
  taps (ok, stale epoch, replay, wrong message), the report's data,
  /lens's status and `delete_garden`; aliases in `store`/`update_meta`;
- `anchor_lens` may call `lens.gaps(n)`, logged in `lens_read`, with the
  amended statuses and no `resolved_at`, and still selects no table;
- the debug views carry no gap text, signature, recheck or findings;
- the migration round-trips.

All notes are synthetic.
"""

from __future__ import annotations

import datetime
import hashlib

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from app.config import Settings, check_vault_settings
from app.db.models import (
    IdleRun,
    LensGap,
    LensGardenRun,
    LensNote,
    LensVersion,
    NoteLink,
    UserState,
    VaultFile,
)
from app.vault import lens
from tests.test_vault_notes_migration import _alembic, _run, scratch_database  # noqa: F401

BEFORE = "c6d2e8a4f917"
AFTER = "b3e9f5a1c7d2"

NOW = datetime.datetime(2026, 9, 30, 12, 0, tzinfo=datetime.timezone.utc)
EPOCH = "k3f7qa"
DETAIL = "Эшби и Бир говорят о разнообразии, но не ссылаются друг на друга."
PROPOSED = "Необходимое разнообразие"
FINDINGS_TEXT = "Ненаписанная заметка о гомеостате"
KNOWLEDGE_TITLE = "Тайная библиотечная заметка"


def _sig(*parts: str) -> str:
    return hashlib.sha256("|".join(("v1",) + parts).encode("utf-8")).hexdigest()


def _gap(kind: str = "link", note_ids=(1, 2), titles=("Ashby", "Beer"), *, title=None, detail=DETAIL, key=None) -> lens.NewGap:
    return lens.NewGap(
        kind=kind,
        note_ids=tuple(note_ids),
        titles=tuple(titles),
        title=title,
        detail=detail,
        signature=_sig(kind, *(key or titles)),
        recheck={"titles": list(titles)},
    )


async def _state(sessionmaker, *, consent: bool = True) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, notes_consent=consent, vault_epoch=EPOCH))
        await session.commit()


async def _note(session, title: str, *, kind: str = "concept", aliases=(), body: str | None = None) -> LensNote:
    file = VaultFile(path=f"Lens/{title}.md", role="note", note_class="knowledge")
    session.add(file)
    await session.flush()
    text_ = body or f"{title}."
    note = LensNote(
        vault_file_id=file.id, kind=kind, title=title, summary=f"{title} summary", body=text_,
        body_hash=hashlib.sha256(text_.encode()).hexdigest(), chars=len(text_), aliases=list(aliases),
    )
    session.add(note)
    await session.flush()
    return note


async def _seed(sessionmaker) -> dict[str, LensNote]:
    """Ashby -> Beer, Beer -> CCRU (knowledge only) -> Wiener, Ashby ->
    unresolved and outside twice, and one knowledge note with no links."""
    async with sessionmaker() as session:
        notes = {
            "Ashby": await _note(session, "Ashby", aliases=("Эшби",)),
            "Beer": await _note(session, "Beer", kind="person"),
            "Wiener": await _note(session, "Wiener"),
        }
        ccru = VaultFile(path="Library/CCRU.md", role="note", note_class="knowledge")
        lonely = VaultFile(path=f"Library/{KNOWLEDGE_TITLE}.md", role="note", note_class="knowledge")
        personal = VaultFile(path="Diary/Private.md", role="note", note_class="personal")
        session.add_all([ccru, lonely, personal])
        await session.flush()
        f = {name: note.vault_file_id for name, note in notes.items()}
        session.add_all(
            [
                NoteLink(src_file_id=f["Ashby"], dst_file_id=f["Beer"]),
                NoteLink(src_file_id=f["Ashby"], dst_file_id=f["Beer"]),
                NoteLink(src_file_id=f["Ashby"], dst_file_id=f["Ashby"]),
                NoteLink(src_file_id=f["Ashby"], unresolved_text=FINDINGS_TEXT),
                NoteLink(src_file_id=f["Ashby"], outside=True),
                NoteLink(src_file_id=f["Ashby"], outside=True),
                NoteLink(src_file_id=f["Beer"], dst_file_id=ccru.id),
                NoteLink(src_file_id=ccru.id, dst_file_id=f["Wiener"]),
                NoteLink(src_file_id=ccru.id, unresolved_text="Knowledge-only target"),
                NoteLink(src_file_id=ccru.id, dst_file_id=lonely.id),
            ]
        )
        await session.commit()
        notes["_ccru_file"] = ccru.id  # type: ignore[assignment]
        notes["_lonely_file"] = lonely.id  # type: ignore[assignment]
        return notes


async def _record(sessionmaker, week: str = "2026-W40", *, new=(), resolved=(), reopened=(), idle_run_id=None, version_id=None, findings=None) -> lens.GardenRecord:
    async with sessionmaker() as session:
        record = await lens.record_garden(
            session,
            idle_run_id=idle_run_id,
            iso_week=week,
            version_id=version_id,
            findings=findings if findings is not None else {"orphans": [3], "wanted": [FINDINGS_TEXT]},
            resolved_ids=resolved,
            reopened_ids=reopened,
            new=list(new),
            now=NOW,
        )
        await session.commit()
        return record


async def _send(sessionmaker, message_id: int = 7001) -> lens.GardenMessage:
    async with sessionmaker() as session:
        message = await lens.unsent_run(session)
        assert message is not None
        assert await lens.mark_run_sent(session, message.run_id, message_id, now=NOW)
        await session.commit()
        return message


async def _gap_row(sessionmaker, gap_id: int):
    async with sessionmaker() as session:
        return (
            await session.execute(text("select * from lens_gap where id = :i"), {"i": gap_id})
        ).mappings().one()


async def _as(sessionmaker, role: str, sql: str):
    async with sessionmaker() as session:
        await session.execute(text(f"set local role {role}"))
        rows = (await session.execute(text(sql))).all()
        await session.commit()
        return rows


# --- settings ---------------------------------------------------------------------


def test_the_garden_is_off_by_default_with_its_own_token_cap():
    settings = Settings(_env_file=None)
    assert settings.LENS_GARDEN_ENABLED is False
    assert settings.GARDEN_MAX_TOKENS == 2000
    check_vault_settings(settings)


@pytest.mark.parametrize(("value", "ok"), [(999, False), (1000, True), (8000, True), (8001, False)])
def test_garden_max_tokens_is_bounded(value, ok):
    settings = Settings(_env_file=None, GARDEN_MAX_TOKENS=value)
    if ok:
        check_vault_settings(settings)
    else:
        with pytest.raises(SystemExit, match="GARDEN_MAX_TOKENS must be between"):
            check_vault_settings(settings)


def test_iso_week_and_report_path():
    assert lens.iso_week(datetime.date(2026, 9, 28)) == "2026-W40"
    assert lens.iso_week(datetime.date(2027, 1, 1)) == "2026-W53"
    assert lens.iso_week(datetime.date(2026, 1, 5)) == "2026-W02"
    assert lens.report_path("2026-W40", EPOCH) == "Anchor/Reports/Lens garden 2026-W40-k3f7qa.md"


# --- aliases ----------------------------------------------------------------------


async def test_store_and_update_meta_keep_aliases_out_of_the_version_hash(sessionmaker):
    await _state(sessionmaker)
    async with sessionmaker() as session:
        file = VaultFile(path="Lens/Ashby.md", role="note", note_class="knowledge")
        session.add(file)
        await session.flush()
        fid = file.id
        assert await lens.store(
            session, fid, kind="concept", title="Ashby", summary=None, body="B.", now=NOW,
            aliases=[" Эшби ", "", "Эшби", "W. Ross Ashby"],
        )
        await session.commit()
        assert (await lens.stored(session))[fid].aliases == ("Эшби", "W. Ross Ashby")
        assert await lens.record_version(session)
        await session.commit()

        # None keeps what is stored; the same list is no change.
        assert not await lens.store(session, fid, kind="concept", title="Ashby", summary=None, body="B.", now=NOW)
        assert not await lens.store(
            session, fid, kind="concept", title="Ashby", summary=None, body="B.", now=NOW,
            aliases=["Эшби", "W. Ross Ashby"],
        )
        assert not await lens.update_meta(session, fid, kind="concept", title="Ashby", summary=None, now=NOW)

        later = NOW + datetime.timedelta(days=1)
        assert await lens.update_meta(
            session, fid, kind="concept", title="Ashby", summary=None, now=later, aliases=["Эшби"]
        )
        await session.commit()
        assert (await lens.stored(session))[fid].aliases == ("Эшби",)
        # An alias change is a change to the note, not to the lens's version.
        assert not await lens.record_version(session)
        assert await lens.store(
            session, fid, kind="concept", title="Ashby", summary=None, body="B.", now=later, aliases=[]
        )
        await session.commit()
        assert (await lens.stored(session))[fid].aliases == ()


# --- the schema -------------------------------------------------------------------


async def test_the_live_signature_is_unique_but_a_resolved_one_may_recur(sessionmaker):
    async with sessionmaker() as session:
        run = LensGardenRun(iso_week="2026-W40")
        session.add(run)
        await session.flush()
        run_id = run.id
        session.add(LensGap(garden_run_id=run_id, kind="link", detail="d", signature="a" * 64))
        await session.commit()
    for status in ("open", "done", "dismissed", "researched"):
        async with sessionmaker() as session:
            session.add(
                LensGap(garden_run_id=run_id, kind="link", detail="d", signature="a" * 64, status=status)
            )
            with pytest.raises(IntegrityError, match="ux_lens_gap_signature_live"):
                await session.commit()
    async with sessionmaker() as session:
        await session.execute(text("update lens_gap set status = 'resolved', resolved_at = now()"))
        session.add(LensGap(garden_run_id=run_id, kind="link", detail="d", signature="a" * 64))
        session.add(
            LensGap(
                garden_run_id=run_id, kind="link", detail="d", signature="a" * 64,
                status="resolved", resolved_at=NOW,
            )
        )
        await session.commit()
        assert (await session.execute(text("select count(*) from lens_gap"))).scalar_one() == 3


@pytest.mark.parametrize(
    ("kwargs", "constraint"),
    [
        ({"kind": "rename"}, "ck_lens_gap_kind"),
        ({"status": "maybe"}, "ck_lens_gap_status"),
        ({"title": "т" * 81}, "ck_lens_gap_title_len"),
        ({"detail": "д" * 301}, "ck_lens_gap_detail_len"),
        ({"signature": "A" * 64}, "ck_lens_gap_signature"),
        ({"signature": "a" * 63}, "ck_lens_gap_signature"),
        ({"reopened": -1}, "ck_lens_gap_reopened"),
        ({"status": "resolved"}, "ck_lens_gap_resolved_at"),
        ({"resolved_at": NOW}, "ck_lens_gap_resolved_at"),
    ],
)
async def test_gap_checks(sessionmaker, kwargs, constraint):
    async with sessionmaker() as session:
        run = LensGardenRun(iso_week="2026-W40")
        session.add(run)
        await session.flush()
        values = {"garden_run_id": run.id, "kind": "link", "detail": "d", "signature": "b" * 64}
        values.update(kwargs)
        session.add(LensGap(**values))
        with pytest.raises(IntegrityError, match=constraint):
            await session.commit()


@pytest.mark.parametrize(
    ("kwargs", "constraint"),
    [
        ({"iso_week": "2026-40"}, "ck_lens_garden_run_iso_week"),
        ({"iso_week": "2026-W40", "tg_message_id": 5}, "ck_lens_garden_run_sent"),
    ],
)
async def test_run_checks(sessionmaker, kwargs, constraint):
    async with sessionmaker() as session:
        session.add(LensGardenRun(**kwargs))
        with pytest.raises(IntegrityError, match=constraint):
            await session.commit()


async def test_the_report_role_and_the_garden_idle_kind(sessionmaker):
    async with sessionmaker() as session:
        session.add(VaultFile(path=lens.report_path("2026-W40", EPOCH), role="report"))
        session.add(IdleRun(kind="lens_garden", local_date=datetime.date(2026, 9, 30)))
        await session.commit()
    for kwargs in (
        {"role": "report", "note_class": "knowledge"},
        {"role": "report", "local_date": datetime.date(2026, 9, 30)},
    ):
        async with sessionmaker() as session:
            session.add(VaultFile(path="Anchor/Reports/x.md", **kwargs))
            with pytest.raises(IntegrityError, match="ck_vault_file_role_columns"):
                await session.commit()
    async with sessionmaker() as session:
        session.add(VaultFile(path="Anchor/Reports/y.md", role="digest"))
        with pytest.raises(IntegrityError, match="ck_vault_file_role"):
            await session.commit()
    async with sessionmaker() as session:
        session.add(IdleRun(kind="lens_research", local_date=datetime.date(2026, 9, 30)))
        with pytest.raises(IntegrityError, match="ck_idle_run_kind"):
            await session.commit()


async def test_foreign_keys_cascade_and_set_null(sessionmaker):
    async with sessionmaker() as session:
        idle = IdleRun(kind="lens_garden", local_date=datetime.date(2026, 9, 30))
        version = LensVersion(hash="c" * 64, note_count=3)
        session.add_all([idle, version])
        await session.flush()
        ids = (idle.id, version.id)
        await session.commit()
    record = await _record(sessionmaker, new=[_gap()], idle_run_id=ids[0], version_id=ids[1])
    async with sessionmaker() as session:
        await session.execute(text("delete from idle_run"))
        await session.execute(text("delete from lens_version"))
        await session.commit()
        run = (await session.execute(text("select idle_run_id, lens_version_id from lens_garden_run"))).one()
        assert tuple(run) == (None, None)
        assert (await session.execute(text("select count(*) from lens_gap"))).scalar_one() == 1
        await session.execute(text("delete from lens_garden_run where id = :i"), {"i": record.run_id})
        await session.commit()
        assert (await session.execute(text("select count(*) from lens_gap"))).scalar_one() == 0


# --- the gate's facts and the graph ------------------------------------------------


async def test_garden_facts(sessionmaker):
    async with sessionmaker() as session:
        facts = await lens.garden_facts(session)
    assert facts == lens.GardenFacts(
        notes=0, last_run_at=None, last_iso_week=None, last_version_id=None, version_id=None, done=0
    )
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        await lens.record_version(session)
        await session.commit()
        version_id = (await session.execute(text("select id from lens_version"))).scalar_one()
    record = await _record(sessionmaker, new=[_gap(), _gap("tension", key=("t",))], version_id=version_id)
    async with sessionmaker() as session:
        await session.execute(
            text("update lens_gap set status = 'done', decided_at = now() where id = :i"),
            {"i": record.new_ids[0]},
        )
        await session.commit()
        facts = await lens.garden_facts(session)
    assert (facts.notes, facts.last_iso_week, facts.last_version_id, facts.version_id, facts.done) == (
        3, "2026-W40", version_id, version_id, 1
    )
    assert isinstance(facts.last_run_at, datetime.datetime)


async def test_garden_view_is_the_lens_with_knowledge_notes_anonymous(sessionmaker):
    notes = await _seed(sessionmaker)
    f = {name: notes[name].vault_file_id for name in ("Ashby", "Beer", "Wiener")}
    ccru, lonely = notes["_ccru_file"], notes["_lonely_file"]
    async with sessionmaker() as session:
        view = await lens.garden_view(session)
    assert [(n.title, n.kind, n.aliases, n.file_id) for n in view.notes] == [
        ("Ashby", "concept", ("Эшби",), f["Ashby"]),
        ("Beer", "person", (), f["Beer"]),
        ("Wiener", "concept", (), f["Wiener"]),
    ]
    assert view.notes[0].body == "Ashby."
    # Each pair once, no self-link, and the knowledge note only as an id;
    # CCRU -> lonely has no lens end and is not here.
    assert set(view.edges) == {(f["Ashby"], f["Beer"]), (f["Beer"], ccru), (ccru, f["Wiener"])}
    assert len(view.edges) == 3
    assert view.outside == {f["Ashby"]: 2}
    # Only unresolved text out of lens notes.
    assert view.unresolved == ((f["Ashby"], FINDINGS_TEXT),)
    # Every knowledge note outside the lens, by file name; no personal one.
    assert view.knowledge_titles == {ccru: "CCRU", lonely: KNOWLEDGE_TITLE}
    assert view.version_id is None
    async with sessionmaker() as session:
        await lens.record_version(session)
        await session.commit()
        assert (await lens.garden_view(session)).version_id is not None


# --- recording a run ---------------------------------------------------------------


async def test_record_garden_dedups_resolves_and_reopens(sessionmaker):
    link, tension = _gap(), _gap("tension", (2, 3), ("Beer", "Wiener"))
    missing = _gap("missing_note", (1,), ("Ashby",), title=PROPOSED, key=(PROPOSED,))
    first = await _record(sessionmaker, "2026-W40", new=[link, tension, missing, link])
    assert len(first.new_ids) == 3
    assert (first.deduped, first.resolved, first.reopened) == (1, 0, 0)
    link_id, tension_id, missing_id = first.new_ids

    async with sessionmaker() as session:
        known = await lens.known_gaps(session)
    assert [(g.id, g.kind, g.status, g.note_ids, g.titles, g.title) for g in known] == [
        (link_id, "link", "open", (1, 2), ("Ashby", "Beer"), None),
        (tension_id, "tension", "open", (2, 3), ("Beer", "Wiener"), None),
        (missing_id, "missing_note", "open", (1,), ("Ashby",), PROPOSED),
    ]
    assert known[0].signature == link.signature and known[0].recheck == {"titles": ["Ashby", "Beer"]}

    async with sessionmaker() as session:
        await session.execute(
            text("update lens_gap set status = 'done', decided_at = now(), tg_message_id = 9 where id in (:a, :b)"),
            {"a": link_id, "b": tension_id},
        )
        await session.execute(
            text("update lens_gap set status = 'dismissed', decided_at = now() where id = :i"), {"i": missing_id}
        )
        await session.commit()

    # The link passed its recheck; the tension did not; the dismissed
    # gap is neither resolved nor reopened; the dismissed and the
    # tension's signatures are still live, so re-proposing them is a no-op.
    second = await _record(
        sessionmaker, "2026-W41",
        new=[missing, tension, link], resolved=[link_id, missing_id], reopened=[tension_id, missing_id, 9999],
    )
    assert (second.resolved, second.reopened, second.deduped) == (1, 1, 2)
    # The resolved link's signature may recur.
    assert len(second.new_ids) == 1
    relinked = second.new_ids[0]
    assert relinked not in (link_id, tension_id, missing_id)

    link_row, tension_row, missing_row = [
        await _gap_row(sessionmaker, i) for i in (link_id, tension_id, missing_id)
    ]
    assert (link_row["status"], link_row["resolved_at"]) == ("resolved", NOW)
    assert link_row["garden_run_id"] == first.run_id
    assert tension_row["status"] == "open"
    assert tension_row["reopened"] == 1
    assert (tension_row["decided_at"], tension_row["tg_message_id"]) == (None, None)
    assert tension_row["garden_run_id"] == second.run_id
    assert (missing_row["status"], missing_row["garden_run_id"]) == ("dismissed", first.run_id)

    async with sessionmaker() as session:
        assert [g.id for g in await lens.known_gaps(session)] == [tension_id, missing_id, relinked]


async def test_record_garden_refuses_bad_input_and_a_second_run_in_a_week(sessionmaker):
    async with sessionmaker() as session:
        for kwargs in (
            {"iso_week": "2026-40"},
            {"findings": ["not", "a", "dict"]},
            {"new": [_gap("rename")]},
            {"new": [lens.NewGap("link", (1, 2), ("a", "b"), None, "d", "X" * 64, {})]},
            {"new": [_gap(detail="д" * 301)]},
            {"new": [_gap("missing_note", title="т" * 81)]},
        ):
            args = {
                "idle_run_id": None, "iso_week": "2026-W40", "version_id": None, "findings": {},
                "resolved_ids": [], "reopened_ids": [], "new": [],
            }
            args.update(kwargs)
            with pytest.raises(ValueError):
                await lens.record_garden(session, **args)
        await session.rollback()
    await _record(sessionmaker, "2026-W40")
    with pytest.raises(IntegrityError, match="uq_lens_garden_run_iso_week"):
        await _record(sessionmaker, "2026-W40")


# --- the one message, taps ---------------------------------------------------------


async def test_one_message_per_run_then_taps(sessionmaker):
    await _state(sessionmaker)
    async with sessionmaker() as session:
        assert await lens.unsent_run(session) is None
    record = await _record(sessionmaker, new=[_gap(), _gap("tension", key=("t",))])
    a, b = record.new_ids

    async with sessionmaker() as session:
        message = await lens.unsent_run(session)
    assert message.run_id == record.run_id
    assert (message.iso_week, message.new, message.reopened, message.older_open) == ("2026-W40", 2, 0, 0)
    assert message.report_path is None
    assert [g.id for g in message.gaps] == [a, b]
    assert not any(g.actionable for g in message.gaps)
    assert message.gaps[0].detail == DETAIL

    # Once the vault pass has written the note, the header names it --
    # not before vaultd confirms it: `_render_reports` commits the row
    # before the create, and a failed create leaves it with no file.
    async with sessionmaker() as session:
        file = VaultFile(path=lens.report_path("2026-W40", EPOCH), role="report")
        session.add(file)
        await session.commit()
        assert (await lens.unsent_run(session)).report_path is None
        file.disk_sha256 = "0" * 64
        await session.commit()
        assert (await lens.unsent_run(session)).report_path == lens.report_path("2026-W40", EPOCH)

    # Nothing sent yet: a tap on a gap without a message is stale.
    async with sessionmaker() as session:
        assert await lens.decide_gap(session, a, EPOCH, "done", NOW) == "stale"

    await _send(sessionmaker, 7001)
    async with sessionmaker() as session:
        assert await lens.unsent_run(session) is None
        assert not await lens.mark_run_sent(session, record.run_id, 7002)
        assert await lens.message_run_id(session, 7001) == record.run_id
        assert await lens.message_run_id(session, 7002) is None
        state = await lens.run_message_state(session, record.run_id)
    assert state.tg_message_id == 7001
    assert [(g.id, g.status, g.actionable) for g in state.gaps] == [(a, "open", True), (b, "open", True)]
    assert (await _gap_row(sessionmaker, a))["tg_message_id"] == 7001

    async with sessionmaker() as session:
        with pytest.raises(ValueError):
            await lens.decide_gap(session, a, EPOCH, "researched", NOW)
        assert await lens.decide_gap(session, a, "zzzzzz", "done", NOW) == "stale"
        assert await lens.decide_gap(session, a, EPOCH, "done", NOW, message_id=7002) == "stale"
        assert await lens.decide_gap(session, 9999, EPOCH, "done", NOW) == "stale"
        assert await lens.decide_gap(session, a, EPOCH, "done", NOW, message_id=7001) == "ok"
        await session.commit()
    async with sessionmaker() as session:
        # A replay, and a second answer to the same gap.
        assert await lens.decide_gap(session, a, EPOCH, "done", NOW) == "stale"
        assert await lens.decide_gap(session, a, EPOCH, "dismissed", NOW) == "stale"
        assert await lens.decide_gap(session, b, EPOCH, "dismissed", NOW) == "ok"
        await session.commit()
        state = await lens.run_message_state(session, record.run_id)
    assert [(g.id, g.status, g.actionable) for g in state.gaps] == [
        (a, "done", False), (b, "dismissed", False)
    ]
    row = await _gap_row(sessionmaker, a)
    assert (row["status"], row["decided_at"], row["tg_message_id"]) == ("done", NOW, 7001)


async def test_a_reopened_gap_moves_to_the_new_runs_message(sessionmaker):
    await _state(sessionmaker)
    first = await _record(sessionmaker, "2026-W40", new=[_gap(), _gap("tension", key=("t",)), _gap("bridge", key=("b",))])
    done, still_open, dismissed = first.new_ids
    await _send(sessionmaker, 7001)
    async with sessionmaker() as session:
        assert await lens.decide_gap(session, done, EPOCH, "done", NOW) == "ok"
        assert await lens.decide_gap(session, dismissed, EPOCH, "dismissed", NOW) == "ok"
        await session.commit()

    second = await _record(sessionmaker, "2026-W41", new=[_gap("missing_note", title=PROPOSED, key=("m",))], reopened=[done])
    async with sessionmaker() as session:
        message = await lens.unsent_run(session)
    assert message.run_id == second.run_id
    # The new gap and the reopened one, by id; the still-open one lives in
    # the first message and is counted, not listed.
    assert [(g.id, g.reopened) for g in message.gaps] == [(done, 1), (second.new_ids[0], 0)]
    assert (message.new, message.reopened, message.older_open) == (1, 1, 1)

    await _send(sessionmaker, 7002)
    async with sessionmaker() as session:
        old = await lens.run_message_state(session, first.run_id)
        new = await lens.run_message_state(session, second.run_id)
        # The old message's button for the reopened gap is dead.
        assert await lens.decide_gap(session, done, EPOCH, "done", NOW, message_id=7001) == "stale"
        assert await lens.decide_gap(session, still_open, EPOCH, "dismissed", NOW, message_id=7001) == "ok"
        await session.commit()
    # Same gaps, same order, in the old message; the reopened one has
    # moved on, so its row is gone there and live in the new one.
    assert [(g.id, g.status, g.actionable) for g in old.gaps] == [
        (done, "open", False), (still_open, "open", True), (dismissed, "dismissed", False)
    ]
    # And the old message still says what it delivered: three new, none
    # reopened, no «снова» on the gap that moved on.
    assert (old.new, old.reopened) == (3, 0)
    assert [g.reopened for g in old.gaps] == [0, 0, 0]
    assert (new.new, new.reopened) == (1, 1)
    assert [g.reopened for g in new.gaps] == [1, 0]
    assert [(g.id, g.actionable) for g in new.gaps] == [(done, True), (second.new_ids[0], True)]


async def test_a_run_with_nothing_to_say_is_marked_sent_without_a_message(sessionmaker):
    await _state(sessionmaker)
    await _record(sessionmaker, "2026-W40", new=[_gap()])
    await _send(sessionmaker, 7001)
    record = await _record(sessionmaker, "2026-W41")
    async with sessionmaker() as session:
        message = await lens.unsent_run(session)
        assert (message.run_id, message.gaps, message.older_open) == (record.run_id, (), 1)
        assert await lens.mark_run_sent(session, record.run_id, None)
        await session.commit()
        assert await lens.unsent_run(session) is None
        assert await lens.run_message_state(session, record.run_id) is None


async def test_an_unsent_runs_gaps_ride_the_next_runs_message(sessionmaker):
    """A run still unsent when the next is recorded (a long /quiet, say):
    its open gaps move to the new run, which carries them with live
    rows, and the earlier run is marked sent with no message."""
    await _state(sessionmaker)
    earlier = await _record(sessionmaker, "2026-W40", new=[_gap(), _gap("bridge", key=("b",))])
    old_gap, dismissed = earlier.new_ids
    async with sessionmaker() as session:
        # Never sent, so never tappable; a decided one stays where it is.
        await session.execute(
            text("update lens_gap set status = 'dismissed' where id = :i"), {"i": dismissed}
        )
        await session.commit()
    later = await _record(sessionmaker, "2026-W41", new=[_gap("tension", key=("t",))])
    assert later.carried == 1
    async with sessionmaker() as session:
        message = await lens.unsent_run(session)
        run = await session.get(LensGardenRun, earlier.run_id)
        assert (run.sent_at, run.tg_message_id, run.sent_gap_ids) == (NOW, None, [])
    assert message.run_id == later.run_id
    assert [g.id for g in message.gaps] == [old_gap, later.new_ids[0]]
    assert (message.new, message.reopened, message.older_open) == (2, 0, 0)
    assert (await _gap_row(sessionmaker, dismissed))["garden_run_id"] == earlier.run_id

    await _send(sessionmaker, 7001)
    async with sessionmaker() as session:
        assert await lens.decide_gap(session, old_gap, EPOCH, "done", NOW, message_id=7001) == "ok"
        await session.commit()


# --- the report, /lens, and the lens going away --------------------------------------


async def test_report_data_status_and_delete_garden(sessionmaker):
    notes = await _seed(sessionmaker)
    async with sessionmaker() as session:
        assert await lens.report_data(session) is None
        assert await lens.garden_status(session) is None
    first = await _record(sessionmaker, "2026-W40", new=[_gap(), _gap("tension", key=("t",))])
    await _send(sessionmaker, 7001)  # sent: its open gaps stay its own
    second = await _record(
        sessionmaker, "2026-W41", new=[_gap("bridge", key=("b",))], resolved=[first.new_ids[0]],
        findings={"clusters": [{"id": 1, "name": "Кибернетика"}]},
    )
    await _send(sessionmaker, 7002)
    await _record(sessionmaker, "2026-W42")  # no gaps: not the report's run

    async with sessionmaker() as session:
        data = await lens.report_data(session)
        status = await lens.garden_status(session)
    assert (data.run_id, data.iso_week) == (second.run_id, "2026-W41")
    assert data.findings == {"clusters": [{"id": 1, "name": "Кибернетика"}]}
    assert [(g.id, g.kind, g.status) for g in data.gaps] == [(second.new_ids[0], "bridge", "open")]
    assert [(g.id, g.status) for g in data.still_open] == [(first.new_ids[1], "open")]
    assert data.lens_titles == {n.id: n.title for k, n in notes.items() if not k.startswith("_")}
    assert (status.iso_week, status.open) == ("2026-W42", 2)

    async with sessionmaker() as session:
        assert await lens.delete_garden(session) == 3
        await session.commit()
        assert await lens.garden_status(session) is None
        assert (await session.execute(text("select count(*) from lens_garden_run"))).scalar_one() == 0
        # The lens itself is untouched.
        assert await lens.note_count(session) == 3


# --- Claude Code's door: lens.gaps() -------------------------------------------------


async def test_lens_role_reads_gaps_with_amended_statuses_and_each_call_is_counted(sessionmaker):
    notes = await _seed(sessionmaker)
    ashby, beer = notes["Ashby"].id, notes["Beer"].id
    first = await _record(
        sessionmaker, "2026-W40",
        new=[
            _gap(note_ids=(ashby, beer)),
            _gap("missing_note", (ashby,), ("Ashby",), title=PROPOSED, key=("m",)),
            _gap("tension", (ashby, beer), key=("t",)),
            _gap("missing_note", (beer,), ("Beer",), title="Открытая", key=("m2",)),
        ],
    )
    link_id, missing_id, tension_id, open_missing_id = first.new_ids
    async with sessionmaker() as session:
        await session.execute(
            text("update lens_gap set status = 'done', decided_at = now() where id = :i"), {"i": link_id}
        )
        await session.execute(text("update lens_gap set status = 'researched' where id = :i"), {"i": tension_id})
        await session.commit()
    second = await _record(
        sessionmaker, "2026-W41", new=[_gap("bridge", (ashby, beer), key=("b",))], resolved=[missing_id]
    )

    rows = await _as(sessionmaker, "anchor_lens", "select * from lens.gaps(10)")
    assert list(rows[0]._fields) == [
        "id", "week", "kind", "status", "titles", "title", "detail", "reopened", "created_at", "decided_at"
    ]
    by_id = {r.id: r for r in rows}
    assert [r.id for r in rows] == sorted(by_id, reverse=True)
    assert (by_id[second.new_ids[0]].week, by_id[second.new_ids[0]].status) == ("2026-W41", "open")
    assert by_id[link_id].status == "done"
    # Resolved and researched both read as closed: nothing says which.
    assert by_id[missing_id].status == "closed"
    assert by_id[tension_id].status == "closed"
    # A closed missing_note keeps its sources but loses its proposed
    # title and detail: "closed" with a title no lens note has would
    # still say a knowledge note (or an alias) by that name exists.
    assert (by_id[missing_id].title, by_id[missing_id].titles, by_id[missing_id].detail) == (
        None, ["Ashby"], None
    )
    assert PROPOSED not in repr(rows)
    # Any other gap, closed or not, keeps its text; titles are current.
    assert (by_id[tension_id].titles, by_id[tension_id].detail) == (["Ashby", "Beer"], DETAIL)
    assert (by_id[open_missing_id].status, by_id[open_missing_id].title) == ("open", "Открытая")
    assert "resolved" not in repr(rows) and "researched" not in repr(rows)

    assert len(await _as(sessionmaker, "anchor_lens", "select id from lens.gaps(1)")) == 1
    assert await _as(sessionmaker, "anchor_lens", "select id from lens.gaps(-5)") == []
    assert await _as(sessionmaker, "anchor_lens", "select id from lens.gaps(null)") == []
    async with sessionmaker() as session:
        reads = (await session.execute(text("select fn, rows from lens_read order by id"))).all()
    assert [tuple(r) for r in reads] == [("gaps", 5), ("gaps", 1), ("gaps", 0), ("gaps", 0)]


async def test_lens_gaps_forgets_a_note_that_left_the_lens(sessionmaker):
    """A note moved out of the lens (into a knowledge folder, say) leaves
    lens.gaps() as it leaves lens.notes() and lens.rounds(): its title is
    skipped, and the gap's own text, which may name it, is withheld."""
    notes = await _seed(sessionmaker)
    ashby, beer, wiener = notes["Ashby"].id, notes["Beer"].id, notes["Wiener"].id
    record = await _record(
        sessionmaker,
        new=[
            _gap(note_ids=(ashby, beer), titles=("Ashby", "Beer")),
            _gap("tension", (ashby, wiener), ("Ashby", "Wiener"), key=("t",), detail="Эшби и Винер."),
        ],
    )
    left, kept = record.new_ids
    async with sessionmaker() as session:
        await lens.delete_for_file(session, notes["Beer"].vault_file_id)
        await session.commit()

    rows = {r.id: r for r in await _as(sessionmaker, "anchor_lens", "select * from lens.gaps(10)")}
    assert (rows[left].status, rows[left].titles, rows[left].title, rows[left].detail) == (
        "open", ["Ashby"], None, None
    )
    assert (rows[kept].titles, rows[kept].detail) == (["Ashby", "Wiener"], "Эшби и Винер.")
    assert "Beer" not in repr(rows) and DETAIL not in repr(rows)


async def test_lens_gaps_is_capped_at_fifty_and_a_rolled_back_read_leaves_a_gap(sessionmaker):
    await _record(sessionmaker, new=[_gap(key=(str(i),)) for i in range(55)])
    assert len(await _as(sessionmaker, "anchor_lens", "select id from lens.gaps(1000)")) == 50
    async with sessionmaker() as session:
        await session.execute(text("set local role anchor_lens"))
        assert len((await session.execute(text("select id from lens.gaps(5)"))).all()) == 5
        await session.rollback()
    async with sessionmaker() as session:
        assert (await session.execute(text("select count(*) from lens_read"))).scalar_one() == 1
        assert await lens.unrecorded_reads(session) == 1


@pytest.mark.parametrize(
    "table", ["public.lens_gap", "public.lens_garden_run", "debug.lens_gap", "debug.lens_garden_run"]
)
async def test_lens_role_may_not_select_the_garden_tables(sessionmaker, table):
    await _record(sessionmaker, new=[_gap()])
    async with sessionmaker() as session:
        await session.execute(text("set local role anchor_lens"))
        with pytest.raises(ProgrammingError, match="permission denied"):
            await session.execute(text(f"select * from {table}"))


async def test_nobody_else_may_call_lens_gaps(sessionmaker):
    async with sessionmaker() as session:
        await session.execute(text("set local role anchor_debug"))
        with pytest.raises(ProgrammingError, match="permission denied"):
            await session.execute(text("select * from lens.gaps(5)"))
    async with sessionmaker() as session:
        acl = (
            await session.execute(
                text(
                    "select p.prosecdef, p.proconfig, has_function_privilege('public', p.oid, 'execute')"
                    " from pg_proc p join pg_namespace n on n.oid = p.pronamespace"
                    " where n.nspname = 'lens' and p.proname = 'gaps'"
                )
            )
        ).one()
    assert acl[0] is True
    assert acl[1] == ["search_path=pg_catalog, public"]
    assert acl[2] is False


# --- debug views ------------------------------------------------------------------


async def test_debug_views_carry_no_gap_text_signature_or_findings(sessionmaker):
    await _seed(sessionmaker)
    record = await _record(
        sessionmaker,
        new=[_gap("missing_note", (1,), ("Ashby",), title=PROPOSED, key=("m",)), _gap()],
    )
    async with sessionmaker() as session:
        await session.execute(
            text("update lens_gap set status = 'resolved', resolved_at = now() where id = :i"),
            {"i": record.new_ids[0]},
        )
        await session.execute(text("update lens_gap set tg_message_id = 7001 where id = :i"), {"i": record.new_ids[1]})
        await session.execute(text("update lens_garden_run set sent_at = now(), tg_message_id = 7001"))
        await session.commit()
        columns = {
            (t, c)
            for t, c in await session.execute(
                text(
                    "select table_name, column_name from information_schema.columns"
                    " where table_schema = 'debug'"
                    " and table_name in ('lens_gap', 'lens_garden_run', 'lens_note')"
                )
            )
        }
    assert {c for t, c in columns if t == "lens_gap"} == {
        "id", "garden_run_id", "kind", "note_ids", "status", "reopened", "created_at", "decided_at",
        "sent", "detail_len",
    }
    assert {c for t, c in columns if t == "lens_garden_run"} == {
        "id", "idle_run_id", "iso_week", "lens_version_id", "created_at", "sent_at", "tg_message_id",
        "sent_gap_ids", "sent_reopened",
    }
    assert "alias_count" in {c for t, c in columns if t == "lens_note"}

    gaps = await _as(sessionmaker, "anchor_debug", "select id, status, sent, detail_len from debug.lens_gap order by id")
    runs = await _as(sessionmaker, "anchor_debug", "select * from debug.lens_garden_run")
    notes = await _as(sessionmaker, "anchor_debug", "select alias_count from debug.lens_note order by id")
    assert [tuple(g) for g in gaps] == [
        (record.new_ids[0], "closed", False, len(DETAIL)),
        (record.new_ids[1], "open", True, len(DETAIL)),
    ]
    assert [(r.iso_week, r.tg_message_id) for r in runs] == [("2026-W40", 7001)]
    assert [n.alias_count for n in notes] == [1, 0, 0]
    dumped = repr(gaps) + repr(runs) + repr(await _as(sessionmaker, "anchor_debug", "select * from debug.lens_gap"))
    for secret in (DETAIL, PROPOSED, "Ashby", FINDINGS_TEXT, _sig("link", "Ashby", "Beer"), "resolved"):
        assert secret not in dumped, secret


# --- the migration itself ------------------------------------------------------------


def test_migration_upgrades_and_downgrades_cleanly(scratch_database):  # noqa: F811
    def tables():
        return {
            r["table_name"]
            for r in _run(
                scratch_database,
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'",
            )
        }

    def views():
        return {
            r["table_name"]
            for r in _run(
                scratch_database,
                "SELECT table_name FROM information_schema.views WHERE table_schema = 'debug'",
            )
        }

    def functions():
        return {
            r["proname"]
            for r in _run(
                scratch_database,
                "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'lens'",
            )
        }

    def columns(table):
        return {
            r["column_name"]
            for r in _run(
                scratch_database,
                "SELECT column_name FROM information_schema.columns "
                f"WHERE table_schema = '{table.split('.')[0]}' AND table_name = '{table.split('.')[1]}'",
            )
        }

    _alembic(scratch_database, "upgrade", AFTER)
    assert {"lens_garden_run", "lens_gap"} <= tables()
    assert {"lens_garden_run", "lens_gap", "lens_note"} <= views()
    assert functions() == {"notes", "graph", "rounds", "gaps"}
    assert "aliases" in columns("public.lens_note")
    assert "alias_count" in columns("debug.lens_note")
    _run(
        scratch_database,
        "INSERT INTO vault_file (path, role) VALUES ('Anchor/Reports/Lens garden 2026-W40-k3f7qa.md', 'report')",
        "INSERT INTO idle_run (kind, local_date) VALUES ('lens_garden', '2026-09-30')",
    )

    _alembic(scratch_database, "downgrade", BEFORE)
    assert not {"lens_garden_run", "lens_gap"} & tables()
    assert not {"lens_garden_run", "lens_gap"} & views()
    assert "lens_note" in views()
    assert functions() == {"notes", "graph", "rounds"}
    assert "aliases" not in columns("public.lens_note")
    assert "alias_count" not in columns("debug.lens_note")
    assert _run(scratch_database, "SELECT count(*) AS n FROM vault_file WHERE role = 'report'")[0]["n"] == 0
    assert _run(scratch_database, "SELECT count(*) AS n FROM idle_run")[0]["n"] == 0
    grants = _run(
        scratch_database,
        "SELECT has_table_privilege('anchor_debug', 'debug.lens_note', 'select') AS ok",
    )
    assert grants[0]["ok"] is True
    _alembic(scratch_database, "upgrade", "head")

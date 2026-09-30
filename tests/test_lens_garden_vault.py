"""L3's vault side: the garden's report note, aliases, and the garden dying with the lens.

The L3 spec's sections 3, 4 and 9 (vault bullets), milestone L3 of
anchor-lens-plan.md. Rendering is pure (app/vault/render.py's
`render_report`); the write path is the real sync pass against the
throwaway database and tests/vault_fake.py's in-memory vault. What is
pinned:

- render: frontmatter and callout, gaps by kind with their status, «Ещё
  открыто», «Структура»; links go to lens notes only; model text is
  escaped (no wikilink, link, tag, table cell, HTML, comment or line
  break); the 60 KiB cap drops «Структура» first;
- the write path: create-only, then compare-and-swap on the digest; a
  hand edit makes the row `diverged`; a taken name `NAME_TAKEN`; a
  deleted report is `dismissed` (sync mode) and never recreated; the
  gates leave existing files alone; an old vaultd's `REFUSED` is
  counted and never rolls the pass back; the note is never ingested;
- aliases come from the graph, masked ones dropped, kept when the graph
  is missing or truncated;
- `delete_garden` runs on every lens-off path: consent off, knowledge
  off, lens off.

Every note, title and gap here is synthetic.
"""

from __future__ import annotations

import datetime
import hashlib

import pytest
from sqlalchemy import func, select, update

from app.config import Settings
from app.core.clock import FrozenClock
from app.db.models import LensGap, LensGardenRun, LensNote, Memory, UserState, VaultFile
from app.vault import consent, errors, lens, render
from app.vault import sync as sync_module
from app.vault.client import Graph
from app.vault.errors import VaultError
from app.vault.sync import FACT_PATH_RE, run_vault_sync
from vault_fake import PRE_REPORTS_WRITABLE, FakeVault

TOKEN = "vault-token-" + "v" * 32
NOW = datetime.datetime(2026, 9, 29, 10, 0, tzinfo=datetime.timezone.utc)
EPOCH = "k3f7qa"
WEEK = "2026-W40"
REPORT = f"Anchor/Reports/Lens garden {WEEK}-{EPOCH}.md"

ASHBY = "# Ashby\n\nRequisite variety. See [[Beer]].\n"
BEER = "# Beer\n\nThe viable system model.\n"
WIENER = "# Wiener\n\nCybernetics.\n"
DETAIL = "Эшби и Винер пишут об обратной связи, но не ссылаются друг на друга. Связать?"


def _settings(*, mode: str = "mirror", knowledge: bool = True, lens_on: bool = True) -> Settings:
    return Settings(
        VAULT_MODE=mode,
        VAULT_API_TOKEN=TOKEN,
        VAULT_KNOWLEDGE_ENABLED=knowledge,
        LENS_ENABLED=lens_on,
    )


def _sig(*parts: str) -> str:
    return hashlib.sha256("|".join(("v1",) + parts).encode("utf-8")).hexdigest()


async def _seed(sessionmaker, *, notes_consent: bool = True) -> None:
    async with sessionmaker() as session:
        session.add(
            UserState(
                id=1, chat_id=555, timezone="Europe/Paris", vault_epoch=EPOCH,
                notes_consent=notes_consent,
            )
        )
        await session.commit()


def _vault() -> FakeVault:
    vault = FakeVault()
    vault.notes["Lens/Ashby.md"] = ("lens", ASHBY)
    vault.notes["Lens/Beer.md"] = ("lens", BEER)
    vault.notes["Lens/Wiener.md"] = ("lens", WIENER)
    vault.notes["Library/Secret shelf.md"] = ("knowledge", "# Shelf\n\nKnowledge only.\n")
    return vault


async def _pass(sessionmaker, vault, settings=None, clock=None):
    async with sessionmaker() as session:
        return await run_vault_sync(session, settings or _settings(), clock or FrozenClock(NOW), vault)


async def _note_ids(sessionmaker) -> dict[str, int]:
    async with sessionmaker() as session:
        return dict((await session.execute(select(LensNote.title, LensNote.id))).all())


async def _garden(sessionmaker, *, week: str = WEEK, findings: dict | None = None) -> lens.GardenRecord:
    """A run with one link gap (Ashby, Wiener) and one missing note."""
    ids = await _note_ids(sessionmaker)
    new = [
        lens.NewGap(
            kind="link",
            note_ids=(ids["Ashby"], ids["Wiener"]),
            titles=("Ashby", "Wiener"),
            title=None,
            detail=DETAIL,
            signature=_sig("link", "ashby", "wiener"),
            recheck={"titles": ["Ashby", "Wiener"]},
        ),
        lens.NewGap(
            kind="missing_note",
            note_ids=(ids["Beer"],),
            titles=("Beer",),
            title="Гомеостат",
            detail="Бир опирается на гомеостат, а заметки о нём нет.",
            signature=_sig("missing_note", "гомеостат"),
            recheck={"title": "Гомеостат"},
        ),
    ]
    async with sessionmaker() as session:
        record = await lens.record_garden(
            session,
            idle_run_id=None,
            iso_week=week,
            version_id=None,
            findings=findings if findings is not None else {"hubs": [ids["Beer"]], "wanted": ["Гомеостат"]},
            resolved_ids=(),
            reopened_ids=(),
            new=new,
            now=NOW,
        )
        await session.commit()
        return record


async def _report_row(sessionmaker) -> VaultFile | None:
    async with sessionmaker() as session:
        return (
            await session.execute(select(VaultFile).where(VaultFile.role == "report"))
        ).scalar_one_or_none()


async def _garden_counts(sessionmaker) -> tuple[int, int]:
    async with sessionmaker() as session:
        runs = (await session.execute(select(func.count()).select_from(LensGardenRun))).scalar_one()
        gaps = (await session.execute(select(func.count()).select_from(LensGap))).scalar_one()
        return runs, gaps


async def _garden_ready(sessionmaker, vault: FakeVault, **settings) -> lens.GardenRecord:
    """Seed, index the lens, record a run: the report is due next pass."""
    await _seed(sessionmaker)
    await _pass(sessionmaker, vault, _settings(**settings))
    return await _garden(sessionmaker)


# --- render (pure) ----------------------------------------------------------------


LENS_TITLES = {1: "Ashby", 2: "Beer", 3: "Wiener", 4: "Pask"}


def _rgap(id_: int, kind: str = "link", *, note_ids=(1, 3), titles=("Ashby", "Wiener"), title=None,
          detail: str = DETAIL, status: str = "open", reopened: int = 0) -> lens.ReportGap:
    return lens.ReportGap(
        id=id_, kind=kind, note_ids=tuple(note_ids), titles=tuple(titles), title=title,
        detail=detail, status=status, reopened=reopened,
    )


def _data(gaps=(), still_open=(), findings=None, titles=None) -> lens.ReportData:
    return lens.ReportData(
        run_id=1,
        iso_week=WEEK,
        created_at=NOW,
        findings=findings or {},
        gaps=tuple(gaps),
        still_open=tuple(still_open),
        lens_titles=dict(LENS_TITLES if titles is None else titles),
    )


def test_render_report_layout():
    data = _data(
        gaps=[
            _rgap(1),
            _rgap(2, "missing_note", note_ids=(2,), titles=("Beer",), title="Гомеостат",
                  detail="Заметки нет.", status="done"),
            _rgap(3, "tension", note_ids=(1, 2), titles=("Ashby", "Beer"), detail="Спорят?",
                  status="dismissed", reopened=1),
            _rgap(4, "bridge", note_ids=(2, 99), titles=("Beer", "Gone"), detail="Мост?", status="resolved"),
        ],
        still_open=[_rgap(9, note_ids=(4, 1), titles=("Pask", "Ashby"), detail="Старое.")],
        findings={
            "hubs": [{"id": 2, "score": 0.5}],
            "clusters": [{"id": 1, "name": "Кибернетика", "members": [1, 3]}, {"id": 2, "members": [4]}],
            "orphans": [4, 12345],
            "dead_ends": [3],
            "wanted": ["Гомеостат", {"text": "Автопоэзис", "sources": 2}],
        },
    )
    content = render.render_report(data, EPOCH).content
    assert content.startswith(f"---\nanchor: report\nanchor_epoch: {EPOCH}\nanchor_week: {WEEK}\n---\n")
    assert "> [!note] Echo" in content and "Правки здесь Echo не читает" in content
    for header in ("## Связи", "## Недостающие заметки", "## Напряжения", "## Мосты", "## Ещё открыто", "## Структура"):
        assert header in content
    order = [content.index(h) for h in ("## Связи", "## Ещё открыто", "## Структура")]
    assert order == sorted(order)
    assert f"- [[Ashby]] · [[Wiener]]: {render.report_escape(DETAIL)} _(открыто)_" in content
    # A proposed note is plain text: a link to it would create it.
    assert "«Гомеостат» — [[Beer]]: Заметки нет. _(закрыто, проверю в следующем саду)_" in content
    assert "_(не нужно, снова)_" in content and "_(закрыто)_" in content
    # A note that left the lens shows its stored title, unlinked.
    assert "[[Beer]] · Gone: Мост?" in content and "[[Gone]]" not in content
    assert "- [[Pask]] · [[Ashby]]: Старое. _(открыто)_" in content
    structure = content.split("## Структура", 1)[1]
    assert "- Узлы: [[Beer]]" in structure
    assert "- Без связей: [[Pask]]\n" in structure  # 12345 is not a lens note: never named
    assert "- Тупики: [[Wiener]]" in structure
    assert "  - Кибернетика: [[Ashby]], [[Wiener]]" in structure and "  - [[Pask]]" in structure
    assert "- Нужны заметки: Гомеостат, Автопоэзис" in structure
    assert "[[Гомеостат]]" not in content and "[[Автопоэзис]]" not in content


def test_render_report_with_no_gaps_and_no_structure():
    content = render.render_report(_data(), EPOCH).content
    assert "Предложений нет." in content and "## Структура" not in content


HOSTILE = (
    "[[Secret note]] and ![[Embed]] | cell #tag\n# Heading [link](https://evil.example) "
    "<a href='x'>html</a> %%hidden%% obsidian://open"
)


def test_model_text_is_escaped():
    data = _data(
        gaps=[_rgap(1, "missing_note", note_ids=(1,), titles=("Ashby",), title=HOSTILE[:80], detail=HOSTILE)],
        findings={"clusters": [{"id": 1, "name": HOSTILE, "members": [1]}], "wanted": [HOSTILE]},
    )
    content = render.render_report(data, EPOCH).content
    body = content.split("---\n", 2)[2]
    links = [chunk.split("]]", 1)[0] for chunk in body.split("[[")[1:]]
    assert set(links) == {"Ashby"}
    for bad in ("[[Secret", "![[", "| cell", "#tag", "\n# Heading", "](", "://", "<a", "%%"):
        assert bad not in body, bad
    text_lines = [line for line in body.splitlines() if not line.startswith("#")]
    assert not any("|" in line or "#" in line for line in text_lines)
    # No model text starts a line: every line is ours.
    for line in body.splitlines():
        assert not line or line.startswith(("- ", "  - ", "> ", "#")), line


def test_escape_keeps_plain_text_readable():
    assert render.report_escape("Связь  Эшби\nи Бира") == "Связь Эшби и Бира"
    assert render.report_escape("C# и [x]") == "C＃ и ［x］"


def test_a_title_obsidian_cannot_link_is_shown_as_text():
    data = _data(gaps=[_rgap(1, note_ids=(1, 3))], titles={1: "A#b", 3: "Wiener"})
    content = render.render_report(data, EPOCH).content
    assert "A＃b · [[Wiener]]" in content and "[[A#b]]" not in content


def test_the_cap_drops_structure_first_then_trims_older_gaps():
    long_detail = "Очень длинное объяснение предложения сада. " * 7
    long_detail = long_detail[:300]
    older = [_rgap(100 + i, detail=long_detail) for i in range(400)]
    own = [_rgap(i, detail=long_detail) for i in range(1, 11)]
    data = _data(gaps=own, still_open=older, findings={"hubs": [1, 2, 3], "wanted": ["Гомеостат"]})
    rendered = render.render_report(data, EPOCH)
    size = len(rendered.content.encode("utf-8"))
    assert size <= render.REPORT_MAX_BYTES
    assert "## Структура" not in rendered.content
    assert rendered.content.count(long_detail.strip()[:40]) >= 10
    assert "- …и ещё " in rendered.content.split("## Ещё открыто", 1)[1]
    # Everything fits comfortably: nothing is cut.
    small = render.render_report(_data(gaps=own, findings={"hubs": [1]}), EPOCH).content
    assert "## Структура" in small and "…и ещё" not in small


def test_the_cap_reserves_the_count_line_only_where_it_cuts():
    """Only «Структура» pushed the note over: every gap line still fits
    and nothing says «…и ещё» (the count line is reserved only where a
    section is cut). And a section with no room for a single line still
    says how many it had, whenever its header shows."""
    detail = ("Предложение сада, длинное и подробное. " * 8)[:300]
    own = [_rgap(i, detail=detail) for i in range(1, 11)]

    def older(count: int) -> list:
        return [_rgap(1000 + i, detail=detail) for i in range(count)]

    def fits(count: int) -> bool:
        content = render.render_report(_data(gaps=own, still_open=older(count)), EPOCH).content
        return "…и ещё" not in content

    low, high = 1, 400  # fits(low), not fits(high)
    assert fits(low) and not fits(high)
    while high - low > 1:
        middle = (low + high) // 2
        low, high = (middle, high) if fits(middle) else (low, middle)
    wanted = [f"Желаемая заметка {i}" for i in range(10)]
    capped = render.render_report(
        _data(gaps=own, still_open=older(low), findings={"wanted": wanted}), EPOCH
    ).content
    assert len(capped.encode("utf-8")) <= render.REPORT_MAX_BYTES
    assert "## Структура" not in capped and "…и ещё" not in capped
    assert capped.count("_(открыто)_") == len(own) + low

    # Own gaps crowding «Ещё открыто» out: whenever its header shows,
    # every older gap is either listed or counted.
    header_without_lines = False
    for count in range(150, 200):
        many = [_rgap(i, detail=detail) for i in range(1, count)]
        content = render.render_report(_data(gaps=many, still_open=older(5)), EPOCH).content
        assert len(content.encode("utf-8")) <= render.REPORT_MAX_BYTES
        if "## Ещё открыто" not in content:
            continue
        section = content.split("## Ещё открыто", 1)[1].strip().splitlines()
        shown = sum(1 for line in section if "_(открыто)_" in line)
        counted = int(section[-1].rsplit(" ", 1)[1]) if "…и ещё" in section[-1] else 0
        assert shown + counted == 5
        header_without_lines = header_without_lines or shown == 0
    assert header_without_lines


def test_the_digest_moves_with_a_status_and_nothing_else():
    one = render.render_report(_data(gaps=[_rgap(1)]), EPOCH)
    again = render.render_report(_data(gaps=[_rgap(1)]), EPOCH)
    tapped = render.render_report(_data(gaps=[_rgap(1, status="dismissed")]), EPOCH)
    assert one.digest == again.digest and one.content == again.content
    assert tapped.digest != one.digest


def test_a_report_is_never_a_fact_path():
    assert not FACT_PATH_RE.match(REPORT)
    assert FACT_PATH_RE.match(f"Anchor/Memory/0001-{EPOCH}.md")


# --- the write path ---------------------------------------------------------------


async def test_the_report_is_created_then_left_alone_while_unchanged(sessionmaker):
    vault = _vault()
    await _garden_ready(sessionmaker, vault)
    result = await _pass(sessionmaker, vault)
    assert result.created == 1 and result.reports_refused == 0
    content = vault.files[REPORT]
    assert "anchor: report" in content and f"anchor_week: {WEEK}" in content
    assert "[[Ashby]] · [[Wiener]]" in content and "«Гомеостат» — [[Beer]]" in content
    # A knowledge-only note is never named in the report.
    assert "Secret shelf" not in content
    row = await _report_row(sessionmaker)
    assert (row.path, row.state, row.memory_id, row.local_date, row.note_class) == (REPORT, "ok", None, None, None)
    assert row.disk_sha256 == hashlib.sha256(content.encode()).hexdigest()

    puts = sum(1 for call in vault.calls if call[0] == "put")
    again = await _pass(sessionmaker, vault)
    assert again.created == again.updated == 0
    assert sum(1 for call in vault.calls if call[0] == "put") == puts
    # The Telegram header may now name the note.
    async with sessionmaker() as session:
        message = await lens.unsent_run(session)
    assert message is not None and message.report_path == REPORT


async def test_a_tap_rewrites_the_report_by_compare_and_swap(sessionmaker):
    vault = _vault()
    record = await _garden_ready(sessionmaker, vault)
    await _pass(sessionmaker, vault)
    async with sessionmaker() as session:
        assert await lens.mark_run_sent(session, record.run_id, 9001, now=NOW)
        assert await lens.decide_gap(session, record.new_ids[0], EPOCH, "dismissed", NOW) == "ok"
        await session.commit()
    before = (await _report_row(sessionmaker)).disk_sha256
    result = await _pass(sessionmaker, vault)
    assert result.updated == 1
    assert "_(не нужно)_" in vault.files[REPORT]
    put = [call for call in vault.calls if call == ("put", REPORT)]
    assert len(put) == 2
    assert (await _report_row(sessionmaker)).disk_sha256 != before


async def test_a_hand_edit_makes_the_report_diverged_for_good(sessionmaker):
    vault = _vault()
    record = await _garden_ready(sessionmaker, vault)
    await _pass(sessionmaker, vault)
    vault.files[REPORT] = vault.files[REPORT] + "\nМоя пометка.\n"
    edited = vault.files[REPORT]
    async with sessionmaker() as session:
        await lens.mark_run_sent(session, record.run_id, 9001, now=NOW)
        await lens.decide_gap(session, record.new_ids[0], EPOCH, "done", NOW)
        await session.commit()
    await _pass(sessionmaker, vault)
    await _pass(sessionmaker, vault)
    assert (await _report_row(sessionmaker)).state == "diverged"
    assert vault.files[REPORT] == edited


async def test_a_taken_name_quarantines_the_row(sessionmaker):
    vault = _vault()
    await _garden_ready(sessionmaker, vault)
    vault.files[REPORT] = "Someone else's file."
    result = await _pass(sessionmaker, vault)
    row = await _report_row(sessionmaker)
    assert (row.state, row.reason) == ("quarantined", errors.NAME_TAKEN)
    assert result.quarantined == 1 and vault.files[REPORT] == "Someone else's file."


async def test_a_deleted_report_is_dismissed_and_never_recreated(sessionmaker):
    vault = _vault()
    await _garden_ready(sessionmaker, vault, mode="sync")
    clock = FrozenClock(NOW)
    await _pass(sessionmaker, vault, _settings(mode="sync"), clock)
    assert REPORT in vault.files
    del vault.files[REPORT]
    await _pass(sessionmaker, vault, _settings(mode="sync"), clock)
    clock.advance(datetime.timedelta(seconds=3600))
    await _pass(sessionmaker, vault, _settings(mode="sync"), clock)
    row = await _report_row(sessionmaker)
    assert row.state == "dismissed"
    async with sessionmaker() as session:
        assert (await session.execute(select(func.count()).select_from(Memory))).scalar_one() == 0
    await _pass(sessionmaker, vault, _settings(mode="sync"), clock)
    assert REPORT not in vault.files


@pytest.mark.parametrize(
    "gate",
    [{"knowledge": False}, {"lens_on": False}, "consent"],
    ids=["knowledge-off", "lens-off", "consent-off"],
)
async def test_the_gates_stop_writing_and_leave_the_file(sessionmaker, gate):
    vault = _vault()
    await _garden_ready(sessionmaker, vault)
    await _pass(sessionmaker, vault)
    written = vault.files[REPORT]
    if gate == "consent":
        async with sessionmaker() as session:
            await consent.set_notes_consent(session, False)
        settings = _settings()
    else:
        settings = _settings(**gate)
    calls = len(vault.calls)
    await _pass(sessionmaker, vault, settings)
    assert vault.files[REPORT] == written
    assert not [call for call in vault.calls[calls:] if call[0] in ("put", "delete")]
    # The garden itself died with the lens.
    assert await _garden_counts(sessionmaker) == (0, 0)


async def test_no_report_is_written_while_a_gate_is_off(sessionmaker):
    """The report step alone (a full pass with a gate off also deletes
    the garden, so there would be nothing to write anyway)."""
    vault = _vault()
    await _garden_ready(sessionmaker, vault)
    for settings, consented in ((_settings(knowledge=False), True), (_settings(lens_on=False), True), (_settings(), False)):
        async with sessionmaker() as session:
            await session.execute(update(UserState).values(notes_consent=consented))
            result = sync_module.PassResult()
            await sync_module._render_reports(
                session, vault, {}, settings, FrozenClock(NOW), sync_module._Budget(50), result
            )
            await session.rollback()
    assert REPORT not in vault.files and await _report_row(sessionmaker) is None


async def test_a_failed_create_never_puts_the_path_in_the_header(sessionmaker):
    """`_render_reports` commits the row before the create; vaultd going
    away mid-create leaves it behind with no file, and the one Telegram
    message must not name a note that was never written."""
    vault = _vault()
    await _garden_ready(sessionmaker, vault)

    def fail_report(path: str) -> None:
        if path == REPORT:
            vault.crash_before_put = VaultError(errors.UNAVAILABLE)

    vault.before_put = fail_report
    await _pass(sessionmaker, vault)
    assert REPORT not in vault.files
    row = await _report_row(sessionmaker)
    assert row is not None and row.disk_sha256 is None
    async with sessionmaker() as session:
        message = await lens.unsent_run(session)
    assert message is not None and message.report_path is None
    # The next pass writes it, and the header may name it.
    vault.before_put = None
    await _pass(sessionmaker, vault)
    assert REPORT in vault.files
    async with sessionmaker() as session:
        assert (await lens.unsent_run(session)).report_path == REPORT


async def test_an_old_vaultd_refusing_reports_never_rolls_the_pass_back(sessionmaker):
    vault = _vault()
    vault.writable = PRE_REPORTS_WRITABLE
    await _garden_ready(sessionmaker, vault)
    vault.notes["Lens/Pask.md"] = ("lens", "# Pask\n\nConversation theory.\n")
    result = await _pass(sessionmaker, vault)
    assert result.reports_refused == 1 and result.lens_stored == 1
    assert REPORT not in vault.files
    # The row went with the refusal: nothing reads the note as written.
    assert await _report_row(sessionmaker) is None
    assert "Pask" in await _note_ids(sessionmaker)
    async with sessionmaker() as session:
        message = await lens.unsent_run(session)
    assert message is not None and message.report_path is None
    # vaultd deployed: the next pass writes it.
    vault.writable = FakeVault().writable
    later = await _pass(sessionmaker, vault)
    assert later.created == 1 and REPORT in vault.files


async def test_an_old_vaultd_refusing_an_update_is_counted(sessionmaker):
    vault = _vault()
    record = await _garden_ready(sessionmaker, vault)
    await _pass(sessionmaker, vault)
    async with sessionmaker() as session:
        await lens.mark_run_sent(session, record.run_id, 9001, now=NOW)
        await lens.decide_gap(session, record.new_ids[0], EPOCH, "done", NOW)
        await session.commit()
    # vaultd still lists the file but refuses the write (a route that
    # refuses what it once took): counted, and the pass goes on.
    real_put = vault.put_file

    async def refusing_put(path, content, if_sha256):
        if path.startswith("Anchor/Reports/"):
            raise VaultError(errors.REFUSED)
        return await real_put(path, content, if_sha256)

    vault.put_file = refusing_put
    result = await _pass(sessionmaker, vault)
    assert result.reports_refused == 1 and result.updated == 0
    assert (await _report_row(sessionmaker)).state == "ok"


async def test_only_the_latest_run_with_gaps_is_rendered(sessionmaker):
    vault = _vault()
    await _garden_ready(sessionmaker, vault)
    await _pass(sessionmaker, vault)
    async with sessionmaker() as session:
        # Week 40's message went out (an unsent run's gaps would move on).
        await lens.mark_run_sent(session, (await lens.unsent_run(session)).run_id, 7001, now=NOW)
        await lens.record_garden(
            session, idle_run_id=None, iso_week="2026-W41", version_id=None, findings={},
            resolved_ids=(), reopened_ids=(), new=[], now=NOW,
        )
        await session.commit()
    result = await _pass(sessionmaker, vault)
    # The empty week-41 run has no note; week 40's stays the latest with gaps.
    assert result.created == 0
    assert [path for path in vault.files if path.startswith("Anchor/Reports/")] == [REPORT]


async def test_the_report_is_never_ingested(sessionmaker):
    vault = _vault()
    await _garden_ready(sessionmaker, vault, mode="sync")
    await _pass(sessionmaker, vault, _settings(mode="sync"))
    vault.files[REPORT] = "---\nanchor: fact\nfact: Я люблю кофе.\nkind: preference\n---\n"
    await _pass(sessionmaker, vault, _settings(mode="sync"))
    async with sessionmaker() as session:
        assert (await session.execute(select(func.count()).select_from(Memory))).scalar_one() == 0
    assert not [call for call in vault.calls if call == ("get", REPORT)]
    assert (await _report_row(sessionmaker)).state == "diverged"


async def test_a_pre_delete_report_is_an_epoch_orphan(sessionmaker):
    """A report from before /delete, re-uploaded by an offline device, has
    the old epoch in its name and frontmatter: deleted, never adopted."""
    vault = _vault()
    await _seed(sessionmaker)
    old = f"Anchor/Reports/Lens garden {WEEK}-zzzzzz.md"
    vault.files[old] = "---\nanchor: report\nanchor_epoch: zzzzzz\nanchor_week: 2026-W40\n---\nСтарое.\n"
    result = await _pass(sessionmaker, vault)
    assert result.orphans == 1 and old not in vault.files


# --- aliases ----------------------------------------------------------------------


async def _aliases(sessionmaker) -> dict[str, list[str]]:
    async with sessionmaker() as session:
        return {title: list(aliases) for title, aliases in await session.execute(select(LensNote.title, LensNote.aliases))}


async def test_aliases_come_from_the_graph_and_masked_ones_are_dropped(sessionmaker):
    await _seed(sessionmaker)
    vault = _vault()
    secret = "sk-or-v1-" + "a" * 48
    vault.aliases["Lens/Ashby.md"] = ("Эшби", " Эшби ", "", secret)
    await _pass(sessionmaker, vault)
    assert (await _aliases(sessionmaker))["Ashby"] == ["Эшби"]
    # The file is unchanged; an alias edited in frontmatter still lands.
    vault.aliases["Lens/Ashby.md"] = ("Эшби", "У. Росс Эшби")
    result = await _pass(sessionmaker, vault)
    assert result.lens_stored == 1
    assert (await _aliases(sessionmaker))["Ashby"] == ["Эшби", "У. Росс Эшби"]


async def test_aliases_are_kept_when_the_graph_is_missing_or_truncated(sessionmaker):
    await _seed(sessionmaker)
    vault = _vault()
    vault.aliases["Lens/Ashby.md"] = ("Эшби",)
    vault.aliases["Lens/Beer.md"] = ("Бир",)
    await _pass(sessionmaker, vault)

    vault.graph_error = VaultError(errors.NOT_FOUND)
    vault.notes["Lens/Ashby.md"] = ("lens", ASHBY + "\nMore.\n")
    await _pass(sessionmaker, vault)
    assert (await _aliases(sessionmaker))["Ashby"] == ["Эшби"]

    vault.graph_error = None
    full = await vault.knowledge_graph()
    vault.graph = Graph(
        nodes=[node for node in full.nodes if node.path != "Lens/Beer.md"],
        edges=[edge for edge in full.edges if edge.src != "Lens/Beer.md"],
        truncated=True,
    )
    vault.notes["Lens/Beer.md"] = ("lens", BEER + "\nMore.\n")
    await _pass(sessionmaker, vault)
    assert (await _aliases(sessionmaker))["Beer"] == ["Бир"]


# --- the garden dies with the lens -----------------------------------------------


async def test_consent_off_deletes_the_garden_in_its_own_transaction(sessionmaker):
    vault = _vault()
    await _garden_ready(sessionmaker, vault)
    assert await _garden_counts(sessionmaker) == (1, 2)
    async with sessionmaker() as session:
        await consent.set_notes_consent(session, False)
    assert await _garden_counts(sessionmaker) == (0, 0)


@pytest.mark.parametrize("settings", [{"knowledge": False}, {"lens_on": False}], ids=["knowledge-off", "lens-off"])
async def test_a_flag_off_deletes_the_garden_on_the_next_pass(sessionmaker, settings):
    vault = _vault()
    await _garden_ready(sessionmaker, vault)
    await _pass(sessionmaker, vault, _settings(**settings))
    assert await _garden_counts(sessionmaker) == (0, 0)


async def test_a_run_without_gaps_also_goes_with_the_lens(sessionmaker):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        await lens.record_garden(
            session, idle_run_id=None, iso_week=WEEK, version_id=None, findings={},
            resolved_ids=(), reopened_ids=(), new=[], now=NOW,
        )
        await session.commit()
    await _pass(sessionmaker, _vault(), _settings(lens_on=False))
    assert await _garden_counts(sessionmaker) == (0, 0)


async def test_consent_off_on_the_pass_is_a_floor(sessionmaker):
    """Consent already off, garden rows present anyway (say, restored by
    hand): the pass removes them too."""
    await _seed(sessionmaker, notes_consent=False)
    async with sessionmaker() as session:
        await lens.record_garden(
            session, idle_run_id=None, iso_week=WEEK, version_id=None, findings={},
            resolved_ids=(), reopened_ids=(), new=[], now=NOW,
        )
        await session.commit()
    await _pass(sessionmaker, _vault())
    assert await _garden_counts(sessionmaker) == (0, 0)

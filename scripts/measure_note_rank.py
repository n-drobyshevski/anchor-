"""Measure note retrieval separation, with a lexical gate (milestone 8d, phase 2).

Phase-8 plan section 9 says to measure `NOTES_MIN_RANK` before fixing
it, "exactly as 2b did for trigram retrieval". The 8e plan's section 9
amends this to two thresholds, `PERSONAL_MIN_RANK` and
`KNOWLEDGE_MIN_RANK`, measured separately.

**Phase 1** measured `ts_rank_cd` alone and found it did not cleanly
separate true positives from noise: a single shared content word could
outscore a genuine but short true positive. **Phase 2** adds a second
signal, `matched` -- the count of distinct query lexemes a chunk's own
`tsv` actually contains, returned by `search_ranked` alongside `rank` --
and measures whether *gating* on both (`matched >= N` and `rank >=` a
floor) separates where rank alone did not.

**What this script does, and does not, do.**

- Builds a throwaway Postgres database (never the session one, never
  production), runs `alembic upgrade head`, indexes the frozen corpus
  in `scripts/note_rank_corpus.py` (~60 notes, ~100 messages) with
  `app.vault.notes_text.prepare` through the real access modules
  (consent on), into both `note_chunk_personal` and
  `note_chunk_knowledge`.
- For every message, fetches every chunk `search_ranked` finds (no
  `LIMIT` cutoff that would hide a correct-but-lower-ranked chunk from
  the gate analysis), then evaluates three candidate gates
  (`matched >= 1`, `>= 2`, `>= 3`), each at its own best rank floor,
  and reports tp/fp/fn/tn, precision, recall (overall and per
  language), and gated top-hit accuracy for each.
- Reports which gate (if any) meets the fixed acceptance criterion
  (precision >= 0.95 on noise, recall >= 0.6 overall, no language
  below recall 0.4), and says plainly if none does.
- **Does not pick a final constant in code**, and does not touch
  `app/core/turn.py`. Reading this output and deciding is a separate,
  human step.

Run it with:

    uv run python scripts/measure_note_rank.py

It needs the same reachable Postgres cluster the test suite uses
(`ANCHOR_ADMIN_DATABASE_URL`, default `postgresql://anchor:anchor@127.0.0.1:5432/postgres`).
"""

from __future__ import annotations

import asyncio
import os
import random
import string
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg
from sqlalchemy.ext.asyncio import AsyncSession

from scripts.note_rank_corpus import MESSAGES, NOTES

REPO_ROOT = Path(__file__).resolve().parent.parent

MIN_MATCHED_CANDIDATES = (1, 2, 3)

# The acceptance criterion, fixed before any gate is scored (per the
# task): precision on noise >= 0.95 (so at most 2 of the corpus's 40
# noise messages ever surface a chunk), recall overall >= 0.6, and no
# single language's recall below 0.4.
MIN_NOISE_PRECISION = 0.95
MIN_OVERALL_RECALL = 0.6
MIN_PER_LANGUAGE_RECALL = 0.4


def _admin_dsn() -> str:
    return os.environ.get(
        "ANCHOR_ADMIN_DATABASE_URL", "postgresql://anchor:anchor@127.0.0.1:5432/postgres"
    )


def _run_alembic_upgrade(database_url: str) -> None:
    from alembic import command
    from alembic.config import Config

    prior = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        command.upgrade(cfg, "head")
    finally:
        if prior is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prior


async def _create_database() -> tuple[str, str]:
    """A throwaway `anchor_measure_<rand>` database. Returns (name, asyncpg url)."""
    db_name = "anchor_measure_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    admin_dsn = _admin_dsn()
    base = admin_dsn.rsplit("/", 1)[0]
    raw_url = f"{base}/{db_name}"
    asyncpg_url = raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(
            f'CREATE DATABASE "{db_name}" TEMPLATE template0 LOCALE \'C.UTF-8\' ENCODING \'UTF8\''
        )
    finally:
        await conn.close()
    return db_name, asyncpg_url


async def _drop_database(db_name: str) -> None:
    conn = await asyncpg.connect(_admin_dsn())
    try:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid()"
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
    finally:
        await conn.close()


# --------------------------------------------------------------------------
# Indexing.
# --------------------------------------------------------------------------


async def _index_all(
    session: AsyncSession, note_class: str, path_prefix: str, replace_chunks
) -> None:
    """Insert every corpus note as a `vault_file` row plus its chunks.

    `note_class` and `path_prefix` are passed in by the caller rather
    than derived from a model here, so this function never has to name
    a chunk table itself (tests/test_vault_notes_isolation.py scans
    scripts/ too, and only the access module each `replace_chunks`
    belongs to may name its table).
    """
    from app.db.models import VaultFile
    from app.vault.notes_text import prepare

    for note in NOTES:
        row = VaultFile(path=f"{path_prefix}/{note.title}.md", role="note", note_class=note_class)
        session.add(row)
        await session.flush()
        chunks = prepare(note.text, note.title)
        await replace_chunks(session, row.id, chunks)
    await session.commit()


def _note_for_heading(heading: str | None) -> str | None:
    """The note title a chunk's heading was built from (notes_text._heading_path)."""
    if heading is None:
        return None
    return heading.split(" › ", 1)[0]


# --------------------------------------------------------------------------
# Collection: every candidate chunk for every message, unfiltered.
# --------------------------------------------------------------------------


@dataclass
class MessageResult:
    text: str
    lang: str
    expected: str | None
    # (note title, rank, matched), best rank first (as search_ranked orders).
    candidates: list[tuple[str | None, float, int]] = field(default_factory=list)


async def _collect(session: AsyncSession, model: type, search_ranked) -> list[MessageResult]:
    results: list[MessageResult] = []
    # A limit generously above the corpus's total chunk count, so no
    # correct-but-lower-ranked chunk is cut off before the gate analysis
    # even sees it.
    limit = 2000
    for msg in MESSAGES:
        rows = await search_ranked(session, model, msg.text, limit)
        candidates = [(_note_for_heading(heading), rank, matched) for heading, _text, rank, matched in rows]
        results.append(MessageResult(msg.text, msg.lang, msg.expected, candidates))
    return results


def _print_top1_table(class_name: str, results: list[MessageResult]) -> None:
    print(f"\n=== {class_name}: top-1 candidate per message (ungated) ===")
    print(f"{'lang':4} {'expected':28} {'top_note':28} {'rank':>7} {'matched':>7}  hit")
    for r in results:
        if r.candidates:
            top_note, rank, matched = r.candidates[0]
            rank_str, matched_str = f"{rank:.4f}", str(matched)
        else:
            top_note, rank_str, matched_str = None, "  -   ", " -"
        hit = "-" if r.expected is None else ("YES" if top_note == r.expected else "no")
        print(
            f"{r.lang:4} {(r.expected or '(none)'):28} {(top_note or '(none)'):28} "
            f"{rank_str:>7} {matched_str:>7}  {hit}"
        )


# --------------------------------------------------------------------------
# Gate analysis: matched >= N and rank >= a floor.
# --------------------------------------------------------------------------


@dataclass
class GateStats:
    min_matched: int
    floor: float
    tp: int
    fp: int
    fn: int
    tn: int
    precision: float
    recall: float
    per_lang: dict[str, tuple[int, int]]  # lang -> (hits, total)
    gated_top_hit: tuple[int, int]  # (correct, total) among tp messages with >=1 gated candidate


def _meets_criterion(stats: GateStats) -> bool:
    return (
        stats.precision >= MIN_NOISE_PRECISION
        and stats.recall >= MIN_OVERALL_RECALL
        and all(
            (hits / total if total else 0.0) >= MIN_PER_LANGUAGE_RECALL
            for hits, total in stats.per_lang.values()
        )
    )


def _all_gates(results: list[MessageResult], min_matched: int) -> list[GateStats]:
    """`GateStats` for every observed rank floor at this `min_matched`, worst to best floor."""
    tp_rows = [r for r in results if r.expected is not None]
    noise_rows = [r for r in results if r.expected is None]

    def _best_rank_for_note(r: MessageResult, note: str | None) -> float | None:
        matches = [
            rank
            for candidate_note, rank, matched in r.candidates
            if matched >= min_matched and (note is None or candidate_note == note)
        ]
        return max(matches) if matches else None

    tp_best = [_best_rank_for_note(r, r.expected) for r in tp_rows]
    noise_best = [_best_rank_for_note(r, None) for r in noise_rows]

    floors = sorted({v for v in tp_best if v is not None} | {v for v in noise_best if v is not None})
    stats_by_floor: list[GateStats] = []
    for floor in floors:
        tp = sum(1 for v in tp_best if v is not None and v >= floor)
        fn = len(tp_best) - tp
        fp = sum(1 for v in noise_best if v is not None and v >= floor)
        tn = len(noise_best) - fp
        precision = tp / (tp + fp) if (tp + fp) else 1.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        per_lang: dict[str, tuple[int, int]] = {}
        for r, v in zip(tp_rows, tp_best):
            hits, total = per_lang.get(r.lang, (0, 0))
            total += 1
            if v is not None and v >= floor:
                hits += 1
            per_lang[r.lang] = (hits, total)
        gated_correct = 0
        gated_total = 0
        for r in tp_rows:
            gated_total += 1
            gated = [
                (note, rank) for note, rank, matched in r.candidates if matched >= min_matched and rank >= floor
            ]
            if gated and max(gated, key=lambda pair: pair[1])[0] == r.expected:
                gated_correct += 1
        stats_by_floor.append(
            GateStats(min_matched, floor, tp, fp, fn, tn, precision, recall, per_lang, (gated_correct, gated_total))
        )
    return stats_by_floor


def _best_by_score(all_gates: list[GateStats]) -> GateStats | None:
    """The floor maximising precision+recall -- the "best separating floor" report."""
    if not all_gates:
        return None
    return max(all_gates, key=lambda s: s.precision + s.recall)


def _best_meeting_criterion(all_gates: list[GateStats]) -> GateStats | None:
    """Among floors that meet the fixed acceptance criterion, the one with the highest recall."""
    passing = [s for s in all_gates if _meets_criterion(s)]
    if not passing:
        return None
    return max(passing, key=lambda s: s.recall)


def _print_stats_block(label: str, stats: GateStats) -> None:
    print(
        f"  {label} floor: {stats.floor:.4f}  "
        f"tp={stats.tp} fp={stats.fp} fn={stats.fn} tn={stats.tn}  "
        f"precision={stats.precision:.2f} recall={stats.recall:.2f}"
    )
    print("    per-language recall:")
    for lang in ("ru", "fr", "en"):
        hits, total = stats.per_lang.get(lang, (0, 0))
        recall = hits / total if total else 0.0
        print(f"      {lang}: {hits}/{total} = {recall:.2f}")
    correct, total = stats.gated_top_hit
    accuracy = correct / total if total else 0.0
    print(f"    gated top-hit accuracy: {correct}/{total} = {accuracy:.2f}")
    print(f"    meets the acceptance criterion (precision>=0.95, recall>=0.6, per-lang>=0.4): {_meets_criterion(stats)}")


def _print_gate(class_name: str, all_gates: list[GateStats], min_matched: int) -> bool:
    """Prints both the best-scoring floor and, if different, the best floor that
    actually meets the fixed criterion. Returns whether any floor at this
    `min_matched` meets the criterion."""
    print(f"\n--- {class_name}: gate matched >= {min_matched} ---")
    if not all_gates:
        print("  no candidate cleared this matched threshold at all -- no data to gate on.")
        return False
    best = _best_by_score(all_gates)
    _print_stats_block("best-scoring (precision+recall) rank", best)
    passing = _best_meeting_criterion(all_gates)
    if passing is not None and passing.floor != best.floor:
        print("  highest-recall floor that meets the criterion, if any:")
        _print_stats_block("criterion-meeting rank", passing)
    return passing is not None


async def _index_and_measure(asyncpg_url: str) -> None:
    from app.db.models import NoteChunkKnowledge, NoteChunkPersonal, UserState
    from app.db.session import create_engine_and_sessionmaker
    from app.vault import notes_knowledge, notes_personal
    from app.vault._chunks import search_ranked

    n_tp = sum(1 for m in MESSAGES if m.expected is not None)
    n_noise = len(MESSAGES) - n_tp
    print(f"corpus: {len(NOTES)} notes, {len(MESSAGES)} messages ({n_tp} true positives, {n_noise} noise)")

    engine, sessionmaker = create_engine_and_sessionmaker(asyncpg_url)
    try:
        async with sessionmaker() as session:
            session.add(UserState(id=1, chat_id=1, notes_consent=True))
            await session.commit()

        async with sessionmaker() as session:
            print(f"indexing {len(NOTES)} synthetic notes as personal notes ...")
            await _index_all(session, "personal", "Personal", notes_personal.replace_chunks)
        async with sessionmaker() as session:
            print(f"indexing {len(NOTES)} synthetic notes as knowledge notes ...")
            await _index_all(session, "knowledge", "Library", notes_knowledge.replace_chunks)

        any_gate_passed = False
        for model, class_name in (
            (NoteChunkPersonal, "personal (PERSONAL_MIN_RANK)"),
            (NoteChunkKnowledge, "knowledge (KNOWLEDGE_MIN_RANK)"),
        ):
            async with sessionmaker() as session:
                results = await _collect(session, model, search_ranked)
            _print_top1_table(class_name, results)
            for min_matched in MIN_MATCHED_CANDIDATES:
                all_gates = _all_gates(results, min_matched)
                if _print_gate(class_name, all_gates, min_matched):
                    any_gate_passed = True

        print(
            "\n=== overall: at least one gate meets the acceptance criterion: "
            f"{any_gate_passed} ==="
        )
    finally:
        await engine.dispose()


def main() -> None:
    db_name, asyncpg_url = asyncio.run(_create_database())
    print(f"scratch database: {db_name}")
    try:
        _run_alembic_upgrade(asyncpg_url.replace("postgresql+asyncpg://", "postgresql://", 1))
        asyncio.run(_index_and_measure(asyncpg_url))
    finally:
        asyncio.run(_drop_database(db_name))
        print(f"\ndropped scratch database {db_name}")


if __name__ == "__main__":
    main()

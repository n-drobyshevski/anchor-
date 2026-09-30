"""Loading and validating eval cases (phase-3 plan section 9).

Section 9 writes the case files as `.yaml`. They are **TOML** here, by
decision: PyYAML is not in this project's dependencies and 3e is not
worth adding one for, while `tomllib` has been in the standard library
since 3.11. The plan's intent -- hand-editable case files with
multi-line Russian prose and nested tables -- is served either way, and
TOML's triple-quoted strings handle the transcripts cleanly.

Validation is strict and eager: every case is parsed and checked before
the first API call, so a typo in case 13 fails in a second rather than
after $0.09 of model calls.
"""

from __future__ import annotations

import dataclasses
import pathlib
import tomllib

from eval.judge import RUBRIC

CASES_DIR = pathlib.Path(__file__).parent / "cases"

# What `input.kind` may be. Each maps to a different production prompt
# builder -- see eval/scenario.py.
CHAT = "chat"
CHECKIN = "checkin"
NEUTRAL = "neutral"
OUTBOUND = "outbound"
# L2 (anchor-lens-plan.md sections 7 and 13): the weekly review's lens
# round -- selector and grounding call -- over a first-pass analysis the
# case supplies. Not a persona prompt at all; see eval/scenario.py.
LENS_REVIEW = "lens_review"
# L3 (anchor-lens-plan.md section 8; the L3 spec section 9): the lens
# garden's step 1 and its one model call over synthetic notes the case
# seeds. Not a persona prompt either; see eval/scenario.py.
LENS_GARDEN = "lens_garden"
# L4 (anchor-lens-plan.md section 9; the L4 spec section 8): lens
# research's two model steps over synthetic data the case supplies --
# the query call over a gap and its notes (`lens_query`), and the
# lens-mode distill over one page and a question (`lens_distill`). Not
# persona prompts either; see eval/scenario.py.
LENS_QUERY = "lens_query"
LENS_DISTILL = "lens_distill"
INPUT_KINDS = (CHAT, CHECKIN, NEUTRAL, OUTBOUND, LENS_REVIEW, LENS_GARDEN, LENS_QUERY, LENS_DISTILL)
# The gap kinds a lens research may be asked for (app/vault/lens.py's
# RESEARCHABLE_KINDS; tests/test_eval_checks.py pins the two equal).
RESEARCH_GAP_KINDS = ("missing_note", "tension", "bridge")

# What a lens case's `setup.lens` entries may be, and what its
# `checks.lens_outcome` may name -- the same values the migration's
# check constraints allow (app/core/lens_review.py's three outcomes).
LENS_KINDS = ("person", "concept")
LENS_OUTCOMES = ("grounded", "empty", "fallback")
# The first-pass analysis keys app/core/review.py's `validate()` reads.
ANALYSIS_KEYS = ("wins", "misses", "patterns", "intentions", "proposals")

OUTBOUND_KINDS = ("morning", "evening_nag", "silence", "tick", "weekly_review")


@dataclasses.dataclass(frozen=True)
class Case:
    id: str
    title: str
    blocking: bool
    setup: dict
    input: dict
    checks: dict
    path: pathlib.Path

    @property
    def judge_items(self) -> list[str]:
        return list(self.checks.get("judge", []))


def _require(condition: bool, path: pathlib.Path, message: str) -> None:
    if not condition:
        raise ValueError(f"{path.name}: {message}")


def parse(raw: dict, path: pathlib.Path) -> Case:
    """Validate one parsed TOML document into a Case."""
    for key in ("id", "title"):
        _require(isinstance(raw.get(key), str) and raw[key], path, f"missing {key}")

    input_block = raw.get("input") or {}
    kind = input_block.get("kind")
    _require(kind in INPUT_KINDS, path, f"input.kind must be one of {INPUT_KINDS}")

    if kind == LENS_REVIEW:
        _check_lens(raw, input_block, path)
    elif kind == LENS_GARDEN:
        _check_garden(raw, path)
    elif kind == LENS_QUERY:
        _check_lens_query(raw, input_block, path)
    elif kind == LENS_DISTILL:
        _check_lens_distill(raw, input_block, path)
    elif kind == OUTBOUND:
        _require(
            input_block.get("outbound_kind") in OUTBOUND_KINDS,
            path,
            f"outbound cases need outbound_kind in {OUTBOUND_KINDS}",
        )
    else:
        _require(
            isinstance(input_block.get("text"), str) and input_block["text"].strip(),
            path,
            f"{kind} cases need a non-empty input.text",
        )

    checks = raw.get("checks") or {}
    for item in checks.get("judge", []):
        _require(item in RUBRIC, path, f"unknown rubric item {item!r}")
    if "sentences" in checks:
        bounds = checks["sentences"]
        _require(
            isinstance(bounds, list)
            and len(bounds) == 2
            and all(isinstance(n, int) for n in bounds)
            and bounds[0] <= bounds[1],
            path,
            "sentences must be [min, max]",
        )

    for line in (raw.get("setup") or {}).get("transcript", []):
        _require(
            line.get("role") in ("user", "assistant"),
            path,
            "transcript role must be user or assistant",
        )
        _require(isinstance(line.get("content"), str), path, "transcript needs content")

    return Case(
        id=raw["id"],
        title=raw["title"],
        blocking=bool(raw.get("blocking", False)),
        setup=raw.get("setup") or {},
        input=input_block,
        checks=checks,
        path=path,
    )


def _check_lens(raw: dict, input_block: dict, path: pathlib.Path) -> None:
    """A lens case's own shape (L2). Every title a case refers to --
    links, earlier rounds, the checks -- must be one of its seeded notes,
    so a renamed note cannot leave a check that passes vacuously."""
    analysis = input_block.get("analysis")
    _require(
        isinstance(analysis, dict) and set(analysis) <= set(ANALYSIS_KEYS),
        path,
        f"lens_review cases need an input.analysis table with keys from {ANALYSIS_KEYS}",
    )
    known = _check_seeded_notes(raw, path, LENS_REVIEW)
    setup = raw.get("setup") or {}
    for picked in setup.get("lens_history", []):
        _require(
            isinstance(picked, list) and set(picked) <= known,
            path,
            "setup.lens_history entries are lists of seeded titles",
        )
    checks = raw.get("checks") or {}
    for key in ("selected_include", "grounds_include"):
        _require(
            set(checks.get(key, [])) <= known,
            path,
            f"checks.{key} names a title that is not in setup.lens",
        )
    outcome = checks.get("lens_outcome")
    _require(
        outcome is None or outcome in LENS_OUTCOMES,
        path,
        f"checks.lens_outcome must be one of {LENS_OUTCOMES}",
    )


def _check_garden(raw: dict, path: pathlib.Path) -> None:
    """A garden case's own shape (L3): seeded notes as for a lens case,
    at least the three the garden's gate asks for, and a `garden_link`
    check naming two of them."""
    known = _check_seeded_notes(raw, path, LENS_GARDEN)
    _require(len(known) >= 3, path, "lens_garden cases need at least three setup.lens notes")
    checks = raw.get("checks") or {}
    pair = checks.get("garden_link")
    _require(
        pair is None
        or (isinstance(pair, list) and len(pair) == 2 and len(set(pair)) == 2 and set(pair) <= known),
        path,
        "checks.garden_link must be two distinct titles from setup.lens",
    )
    minimum = checks.get("min_gaps")
    _require(
        minimum is None or (isinstance(minimum, int) and minimum >= 0),
        path,
        "checks.min_gaps must be a non-negative integer",
    )


def _check_lens_query(raw: dict, input_block: dict, path: pathlib.Path) -> None:
    """A query case's own shape (L4): seeded notes, each with the summary
    the query call sees, and `input.gap` naming some of them -- the gap's
    kind, one sentence of detail, a proposed title for a missing note."""
    known = _check_seeded_notes(raw, path, LENS_QUERY)
    for note in raw["setup"]["lens"]:
        _require(
            isinstance(note.get("summary"), str) and note["summary"].strip(),
            path,
            "lens_query cases need a summary on every setup.lens note (what the call sees)",
        )
    gap = input_block.get("gap")
    _require(isinstance(gap, dict), path, "lens_query cases need an input.gap table")
    _require(
        gap.get("kind") in RESEARCH_GAP_KINDS,
        path,
        f"input.gap.kind must be one of {RESEARCH_GAP_KINDS}",
    )
    _require(
        isinstance(gap.get("detail"), str) and gap["detail"].strip(),
        path,
        "input.gap needs a detail",
    )
    notes = gap.get("notes")
    _require(
        isinstance(notes, list) and notes and set(notes) <= known,
        path,
        "input.gap.notes is a non-empty list of seeded titles",
    )
    title = gap.get("title")
    _require(
        (title is None) == (gap["kind"] != "missing_note")
        and (title is None or (isinstance(title, str) and title.strip())),
        path,
        "input.gap.title is set for a missing_note gap, and only for one",
    )
    checks = raw.get("checks") or {}
    _require(
        isinstance(checks.get("query_valid", True), bool),
        path,
        "checks.query_valid must be a boolean",
    )


def _check_lens_distill(raw: dict, input_block: dict, path: pathlib.Path) -> None:
    """A distill case's own shape (L4): the question (a research query,
    English, one line) and one page's text, as the fetcher would hand it
    to distill."""
    question = input_block.get("question")
    _require(
        isinstance(question, str) and question.strip() and "\n" not in question.strip(),
        path,
        "lens_distill cases need a one-line input.question",
    )
    _require(
        isinstance(input_block.get("page_text"), str) and input_block["page_text"].strip(),
        path,
        "lens_distill cases need a non-empty input.page_text",
    )
    title = input_block.get("page_title")
    _require(title is None or isinstance(title, str), path, "input.page_title must be a string")
    checks = raw.get("checks") or {}
    for key in ("min_cards", "max_cards"):
        value = checks.get(key)
        _require(
            value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0),
            path,
            f"checks.{key} must be a non-negative integer",
        )


def _check_seeded_notes(raw: dict, path: pathlib.Path, kind: str) -> set[str]:
    """The seeded notes shared by lens and garden cases; returns their titles."""
    setup = raw.get("setup") or {}
    notes = setup.get("lens")
    _require(
        isinstance(notes, list) and notes,
        path,
        f"{kind} cases need a non-empty setup.lens list",
    )
    titles: list[str] = []
    for note in notes:
        _require(
            isinstance(note, dict)
            and isinstance(note.get("title"), str)
            and note["title"].strip()
            and isinstance(note.get("body"), str)
            and note["body"].strip(),
            path,
            "every setup.lens entry needs a title and a body",
        )
        _require(
            note.get("kind", "concept") in LENS_KINDS,
            path,
            f"setup.lens kind must be one of {LENS_KINDS}",
        )
        titles.append(note["title"])
    _require(len(set(titles)) == len(titles), path, "setup.lens titles must be unique")
    known = set(titles)
    for pair in setup.get("lens_links", []):
        _require(
            isinstance(pair, list) and len(pair) == 2 and set(pair) <= known,
            path,
            "setup.lens_links entries are [title, title] pairs of seeded notes",
        )
    return known


def load_all(directory: pathlib.Path = CASES_DIR) -> list[Case]:
    """Every case, sorted by filename so the report reads in plan order."""
    cases = []
    for path in sorted(directory.glob("*.toml")):
        with path.open("rb") as handle:
            cases.append(parse(tomllib.load(handle), path))

    ids = [case.id for case in cases]
    duplicates = {name for name in ids if ids.count(name) > 1}
    if duplicates:
        raise ValueError(f"duplicate case ids: {', '.join(sorted(duplicates))}")
    return cases

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
INPUT_KINDS = (CHAT, CHECKIN, NEUTRAL, OUTBOUND)

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

    if kind == OUTBOUND:
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

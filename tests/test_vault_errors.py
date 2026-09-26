"""app/vault/errors.py's QUARANTINE_CODES: the closed set of
`vault_file.reason` codes (phase-8 plan sections 6 and 10), one source
of truth shared with app/vault/sync.py.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app.vault import errors, sync

ROOT = Path(__file__).resolve().parent.parent
REASON_CODE_RE = re.compile(r"^[a-z][a-z_]{0,39}$")


def test_every_code_matches_the_db_check_regex():
    for code in errors.QUARANTINE_CODES:
        assert REASON_CODE_RE.match(code), code


def test_it_is_a_frozenset_of_the_expected_codes():
    assert errors.QUARANTINE_CODES == frozenset(
        {
            "name_taken",
            "bad_yaml",
            "bad_type",
            "bad_kind",
            "technique",
            "too_long",
            "empty",
            "unsafe",
            "instruction",
            "pin_cap",
            "duplicate_file",
            "duplicate_fact",
            "protected",
        }
    )


def _reason_assignments(path: Path, module) -> set[str]:
    """Every code assigned to `row.reason` (or `.reason =`) in a module,
    as a stand-in for "every code this file can quarantine with".
    Static, like tests/test_vault_isolation.py's AST checks -- this
    file has no vaultd, no fixture files, nothing to run against.

    A value is either a string literal or a bare name (sync.py aliases
    errors.py's constants rather than repeating their strings, per
    test_sync_py_uses_errors_own_constants_not_a_second_copy below), so
    a `Name` node is resolved against the already-imported module's own
    globals rather than re-parsed.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = node.targets
        values = node.value.elts if isinstance(node.value, ast.Tuple) else [node.value]
        for target, value in zip(_flatten_tuple_targets(targets), values):
            if not _is_reason_target(target):
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                found.add(value.value)
            elif isinstance(value, ast.Name) and isinstance(getattr(module, value.id, None), str):
                found.add(getattr(module, value.id))
    return found


def _flatten_tuple_targets(targets: list[ast.expr]) -> list[ast.expr]:
    if len(targets) == 1 and isinstance(targets[0], ast.Tuple):
        return list(targets[0].elts)
    return targets


def _is_reason_target(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "reason"


def test_every_code_sync_py_writes_is_in_the_set():
    written = _reason_assignments(ROOT / "app" / "vault" / "sync.py", sync)
    assert written, "sync.py should quarantine with at least one code"
    assert written <= errors.QUARANTINE_CODES


def test_sync_py_uses_errors_own_constants_not_a_second_copy():
    """app/vault/sync.py must not redefine NAME_TAKEN/BAD_YAML as its
    own string literals -- only alias errors.py's."""
    tree = ast.parse((ROOT / "app" / "vault" / "sync.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in ("NAME_TAKEN", "BAD_YAML"):
                assert isinstance(node.value, ast.Attribute) and node.value.value.id == "errors", (
                    f"{target.id} must alias errors.{target.id}, not redefine it"
                )

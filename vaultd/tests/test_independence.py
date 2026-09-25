"""vaultd imports nothing from the bot (plan sections 2, 15).

The mirror of this test lives in the bot's tests/test_vault_isolation.py.
Both walk the AST rather than grepping, so a docstring that mentions
`app` does not trip them and an `importlib` string would still need an
import statement to smuggle itself in.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_TOP_LEVEL = {"app", "eval", "migrations"}


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


def test_vaultd_imports_nothing_from_the_bot() -> None:
    files = sorted((ROOT / "vaultd").glob("*.py")) + sorted((ROOT / "tests").glob("*.py"))
    assert files
    violations = [
        f"{path.relative_to(ROOT)} imports {name}"
        for path in files
        for name in _imported_modules(path)
        if name.split(".")[0] in FORBIDDEN_TOP_LEVEL
    ]
    assert violations == []


def test_the_check_catches_an_import(tmp_path: Path) -> None:
    sample = tmp_path / "bad.py"
    sample.write_text("from app.core import memory\nimport app\n")
    assert set(_imported_modules(sample)) == {"app", "app.core"}

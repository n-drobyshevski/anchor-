"""Only app/core/echo_write.py writes into the vault for Echo, and it never
writes a memory (lens L4; the L4 spec section 7).

Echo's inbox writer is the one place the bot creates a note in the
user's own vault on Echo's behalf (anchor-lens-plan.md sections 9 and
14.5), and the one place that undoes one (`/lens undo`). A second
caller -- an idle job, a web route, a tool -- would be a way for web
text to land in the vault without the user's tap. So, over the AST of
every module in app/, eval/ and scripts/:

- `put_echo_note` is named only by the client that defines it and by
  echo_write.py;
- an undo with `writer="echo"` appears only in echo_write.py;
- `echo_changeset` (the ledger) is named only by echo_write.py, besides
  the models, purge (/delete) and export (/export);
- echo_write.py imports nothing that writes a memory: a lens card is
  material to read, never something Echo knows about the user.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCANNED = ("app", "eval", "scripts")
WRITER = "app/core/echo_write.py"
CLIENT = "app/vault/client.py"
LEDGER_EXEMPT = ("app/db/models.py", "app/core/purge.py", "app/core/export.py")
# An id column (study_card's) and a log key: ids, never the ledger.
NOT_LEDGER_WORDS = ("echo_changeset_id",)

# What would let echo_write.py write a memory.
MEMORY_MODULES = ("app.core.memory", "app.core.cards", "app.core.extract", "app.vault.sync")
MEMORY_NAMES = ("Memory", "PendingMemory", "write_memory", "add_memory", "adopt")


def _sources() -> list[Path]:
    paths: list[Path] = []
    for top in SCANNED:
        paths.extend(sorted((ROOT / top).rglob("*.py")))
    assert paths
    return paths


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _docstrings(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _words(tree: ast.AST) -> set[str]:
    docstrings = _docstrings(tree)
    words: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            words.add(node.id)
        elif isinstance(node, ast.Attribute):
            words.add(node.attr)
        elif isinstance(node, ast.alias):
            words.add(node.name.rsplit(".", 1)[-1])
            if node.asname:
                words.add(node.asname)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            words.add(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            words.add(node.value)
    return words


def _echo_undo_calls(tree: ast.AST) -> int:
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "writer" and isinstance(keyword.value, ast.Constant) and keyword.value.value == "echo":
                count += 1
    return count


def _violations(path: Path, rel: str) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    words = _words(tree)
    found = []
    if "put_echo_note" in words and rel not in (WRITER, CLIENT):
        found.append(f"{rel}: names put_echo_note")
    if _echo_undo_calls(tree) and rel != WRITER:
        found.append(f"{rel}: undoes with writer='echo'")
    if rel not in (WRITER, *LEDGER_EXEMPT) and any(
        ("echo_changeset" in word.lower() or "EchoChangeset" in word) and word not in NOT_LEDGER_WORDS
        for word in words
    ):
        found.append(f"{rel}: names the echo_changeset ledger")
    return found


def test_only_echo_write_writes_or_undoes_for_echo():
    assert [v for path in _sources() for v in _violations(path, _rel(path))] == []


def test_echo_write_does_both():
    """Not vacuous: the writer really is the one that writes and undoes."""
    tree = ast.parse((ROOT / WRITER).read_text(encoding="utf-8"))
    assert "put_echo_note" in _words(tree)
    assert _echo_undo_calls(tree) == 1
    assert "EchoChangeset" in _words(tree)


def test_echo_write_never_writes_a_memory():
    tree = ast.parse((ROOT / WRITER).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    assert not [m for m in imported if m in MEMORY_MODULES or any(m.startswith(x + ".") for x in MEMORY_MODULES)]
    words = _words(tree)
    assert not [name for name in MEMORY_NAMES if name in words]


@pytest.mark.parametrize(
    ("code", "rel"),
    [
        ("async def f(c):\n    await c.put_echo_note('x.md', 'y', 'echo_1')\n", "app/core/idle/lens_research.py"),
        ("async def f(c):\n    await c.undo_changeset('echo_1', writer='echo')\n", "app/tg/lens.py"),
        ("from app.db.models import EchoChangeset\n", "app/tg/lens.py"),
        ("q = 'select vault_ref from echo_changeset'\n", "app/web/mcp_core.py"),
    ],
)
def test_the_check_catches_each_violation(tmp_path, code, rel):
    path = tmp_path / "sample.py"
    path.write_text(code)
    assert _violations(path, rel)


def test_the_check_ignores_prose_and_the_id_column(tmp_path):
    path = tmp_path / "sample.py"
    path.write_text('"""Mentions put_echo_note and echo_changeset in prose."""\n')
    assert _violations(path, "app/tg/lens.py") == []
    path.write_text("def f(card):\n    return card.echo_changeset_id, {'echo_changeset_id': 1}\n")
    assert _violations(path, "app/research/jobs.py") == []

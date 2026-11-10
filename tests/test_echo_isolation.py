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
- an undo with `writer="echo"` appears only in echo_write.py and in
  app/tg/graph.py, whose `/graph undo` names only changesets whose
  purpose is `prune` (footer link pruning, the user's own tap);
- `graph_prune_apply`, Echo's other write (footer link pruning), is
  named only by the client and app/tg/graph.py: a press or a typed
  `/graph apply <hash>`, never an idle job or a web route;
- `echo_changeset` (the ledger) is named only by echo_write.py, besides
  the models, purge (/delete) and export (/export);
- echo_write.py imports nothing that writes a memory: a lens card is
  material to read, never something Echo knows about the user.

**Board sync is the one write without a tap**, and it is named here as
such (docs/decisions.md, "Board sync"): the user chose to have their
task boards kept in step with the planner in the background. So:

- `post_board` (vaultd's board write) is named only by the client and
  echo_write.py, like `put_echo_note`;
- echo_write.py's `write_board` is named only by echo_write.py and
  app/planner/board_job.py, the `BOARD_SYNC` job -- its own job kind,
  which app/worker.py alone runs (`run_pass`), and which no idle module
  may import (tests/test_idle_isolation.py);
- `undo_boards` is named only by app/tg/boards.py (`/boards undo`);
- every other writer and undo in echo_write.py is still named only by
  the Telegram module whose tap or command runs it: the inbox writer by
  app/tg/garden.py, `/lens undo` by app/tg/lens.py, the stream notes by
  app/tg/planner.py. A new caller of any of them from a job would be a
  second background writer, and fails here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCANNED = ("app", "eval", "scripts")
WRITER = "app/core/echo_write.py"
CLIENT = "app/vault/client.py"
GRAPH = "app/tg/graph.py"
BOARD_JOB = "app/planner/board_job.py"
WORKER = "app/worker.py"
# Each of echo_write.py's entry points, and the only module besides
# echo_write.py that may name it: the tap or command that runs it, or,
# for `write_board`, the board-sync job (the documented exception).
WRITER_CALLERS = {
    "adopt_research": "app/tg/garden.py",
    "settle_open": "app/tg/garden.py",
    "orphan_rows": "app/tg/garden.py",
    "undo_last": "app/tg/lens.py",
    "write_stream_notes": "app/tg/planner.py",
    "undo_stream_notes": "app/tg/planner.py",
    "write_board": BOARD_JOB,
    "undo_boards": "app/tg/boards.py",
}
# Who may import the board-sync job at all, and who may run a pass.
BOARD_JOB_IMPORTERS = (WORKER, "app/tg/boards.py")
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


def _imports_board_job(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name == "app.planner.board_job" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "app.planner.board_job":
                return True
            if node.module == "app.planner" and any(a.name == "board_job" for a in node.names):
                return True
    return False


def _violations(path: Path, rel: str) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    words = _words(tree)
    found = []
    if "put_echo_note" in words and rel not in (WRITER, CLIENT):
        found.append(f"{rel}: names put_echo_note")
    if "graph_prune_apply" in words and rel not in (GRAPH, CLIENT):
        found.append(f"{rel}: names graph_prune_apply")
    if "post_board" in words and rel not in (WRITER, CLIENT):
        found.append(f"{rel}: names post_board")
    for name, caller in WRITER_CALLERS.items():
        if name in words and rel not in (WRITER, caller):
            found.append(f"{rel}: names {name}")
    if _imports_board_job(tree) and rel not in (*BOARD_JOB_IMPORTERS, BOARD_JOB):
        found.append(f"{rel}: imports the board-sync job")
    if _imports_board_job(tree) and "run_pass" in words and rel != WORKER:
        found.append(f"{rel}: runs a board-sync pass")
    if _echo_undo_calls(tree) and rel not in (WRITER, GRAPH):
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


def test_the_board_exception_is_real_and_named():
    """Not vacuous: the job really writes through echo_write.py, the
    worker really runs it, and echo_write.py really posts the board."""
    job = ast.parse((ROOT / BOARD_JOB).read_text(encoding="utf-8"))
    assert "write_board" in _words(job)
    worker = ast.parse((ROOT / WORKER).read_text(encoding="utf-8"))
    assert _imports_board_job(worker) and "run_pass" in _words(worker)
    writer = ast.parse((ROOT / WRITER).read_text(encoding="utf-8"))
    assert "post_board" in _words(writer)
    # Each named caller really does call its writer.
    for name, caller in WRITER_CALLERS.items():
        assert name in _words(ast.parse((ROOT / caller).read_text(encoding="utf-8"))), (name, caller)


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
        ("async def f(c):\n    await c.post_board('B.md', 'x', [], 'boards_1')\n", "app/core/idle/lens_research.py"),
        ("from app.core.echo_write import write_board\n", "app/core/idle/reflect.py"),
        ("from app.core.echo_write import write_board\n", "app/worker.py"),
        ("from app.core import echo_write\nx = echo_write.write_stream_notes\n", "app/planner/board_job.py"),
        ("from app.core import echo_write\nx = echo_write.adopt_research\n", "app/core/idle/lens_research.py"),
        ("from app.planner import board_job\n", "app/core/idle/reflect.py"),
        ("from app.planner import board_job\nboard_job.run_pass\n", "app/tg/boards.py"),
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

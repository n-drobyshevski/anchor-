"""Who may reach vault notes, pinned (8e plan sections 7-8).

Personal note text reaches exactly one consumer, the persona's chat
turn, and never a search engine, a research or idle model, a notebook,
a grant, a web panel or any query that leaves the system -- in this
phase or any later one. Knowledge note text reaches only the persona's
turn in 8e. Rather than trusting docstrings, this walks the AST of
every module in app/, eval/ and scripts/:

- **Imports.** `app.vault.notes_personal` and `app.vault.notes_knowledge`
  may be imported only by `app/core/turn.py` and the rest of
  `app/vault/`, in every spelling (`import a.b`, `from a import b`,
  `from a.b import c`). A later plan that adds a consumer adds one line
  to ALLOWED_IMPORTERS and justifies it against section 7.
- **Forbidden importers** are named again, explicitly, so the rule
  reads where the plan states it, and still holds if the allowlist ever
  grows by mistake.
- **Table names.** Only each access module may name its own chunk
  table, as an ORM model or as a string (SQL). Names, attributes and
  string literals are scanned; docstrings are not, so prose may mention
  a table. The models file, purge and export are exempt by name.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCANNED = ("app", "eval", "scripts")

NOTE_MODULES = ("app.vault.notes_personal", "app.vault.notes_knowledge")

# module -> the paths that may import it. Nothing else.
#
# `scripts/measure_note_rank.py` (milestone 8d, phase 1) is the one
# addition beyond the plan's own two: it is the measurement the
# phase-8 plan's section 9 asks for ("measure before fixing
# NOTES_MIN_RANK", per class per the 8e plan's section 9), it is a
# human-run offline tool rather than a runtime consumer, and it must
# exercise the real access modules -- including their consent check --
# to measure the same ranking `app/core/turn.py` will eventually use.
ALLOWED_IMPORTERS = {
    "app.vault.notes_personal": ("app/core/turn.py", "app/vault/", "scripts/measure_note_rank.py"),
    # app/web/mcp_core.py (connector milestone C3,
    # anchor-claude-connector-plan.md section 9): the Claude connector's
    # `search_library` tool reads knowledge chunks straight from
    # notes_knowledge.search_library -- knowledge only, never
    # notes_personal, which stays off this list.
    "app.vault.notes_knowledge": (
        "app/core/turn.py",
        "app/vault/",
        "scripts/measure_note_rank.py",
        "app/web/mcp_core.py",
    ),
}

# The one exception to FORBIDDEN_IMPORTERS' "app/web/" below, and only
# for notes_knowledge -- see the ALLOWED_IMPORTERS comment above.
FORBIDDEN_EXCEPTIONS = {"app.vault.notes_knowledge": ("app/web/mcp_core.py",)}

# 8e plan section 8, verbatim: neither module may be imported by these.
FORBIDDEN_IMPORTERS = (
    "app/core/idle/",
    "app/core/notebook.py",
    "app/core/extract.py",
    "app/core/welfare.py",
    "app/core/tick.py",
    "app/core/outbound_send.py",
    "app/core/scene.py",
    "app/research/",
    "app/core/interests.py",
    "app/core/grants.py",
    "app/web/",
    "app/planner/",
)

# table -> (the module that owns it, the names that mean it).
TABLES = {
    "note_chunk_personal": ("app/vault/notes_personal.py", ("note_chunk_personal", "NoteChunkPersonal")),
    "note_chunk_knowledge": ("app/vault/notes_knowledge.py", ("note_chunk_knowledge", "NoteChunkKnowledge")),
}
# Named by the list, each for a reason: the models define the tables,
# purge truncates them (/delete), export's comments explain why they
# are left out (/export), and the measurement script (milestone 8d,
# phase 1) is the one place outside the access modules themselves that
# legitimately spans both classes at once, by the same reasoning as
# ALLOWED_IMPORTERS above. Migrations live outside app/.
TABLE_NAME_EXEMPT = (
    "app/db/models.py",
    "app/core/purge.py",
    "app/core/export.py",
    "scripts/measure_note_rank.py",
)


def _sources() -> list[Path]:
    paths: list[Path] = []
    for top in SCANNED:
        paths.extend(sorted((ROOT / top).rglob("*.py")))
    assert paths, "nothing to scan -- this test would pass vacuously"
    return paths


def _rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _imported_modules(tree: ast.AST) -> set[str]:
    """Every module a file imports, with `from pkg import sub` expanded."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _reaches(imported: set[str], module: str) -> bool:
    return any(name == module or name.startswith(module + ".") for name in imported)


def _attribute_uses(tree: ast.AST) -> set[str]:
    """`app.vault.notes_personal` reached as an attribute, after `import app.vault`."""
    return {
        f"app.vault.{node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and f"app.vault.{node.attr}" in NOTE_MODULES
    }


def _import_violations(path: Path, rel: str) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = _imported_modules(tree) | _attribute_uses(tree)
    found = []
    for module in NOTE_MODULES:
        if not _reaches(imported, module):
            continue
        if not rel.startswith(ALLOWED_IMPORTERS[module]):
            found.append(f"{rel}: imports {module}, which only {ALLOWED_IMPORTERS[module]} may")
        if rel.startswith(FORBIDDEN_IMPORTERS) and rel not in FORBIDDEN_EXCEPTIONS.get(module, ()):
            found.append(f"{rel}: imports {module} (8e plan section 8 forbids it here)")
    return found


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _named_tables(tree: ast.AST) -> set[str]:
    """The chunk tables a file names in code: identifiers or string literals."""
    docstrings = _docstring_nodes(tree)
    named: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            words = [node.id]
        elif isinstance(node, ast.Attribute):
            words = [node.attr]
        elif isinstance(node, ast.alias):
            words = [node.name, node.asname or ""]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            words = [node.value]
        else:
            continue
        for table, (_owner, names) in TABLES.items():
            if any(name.lower() in word.lower() for word in words for name in names):
                named.add(table)
    return named


def _table_violations(path: Path, rel: str) -> list[str]:
    if rel in TABLE_NAME_EXEMPT:
        return []
    named = _named_tables(ast.parse(path.read_text(encoding="utf-8")))
    return [f"{rel}: names {table}" for table in sorted(named) if TABLES[table][0] != rel]


def test_only_turn_and_app_vault_import_the_note_modules():
    violations = [v for path in _sources() for v in _import_violations(path, _rel(path))]
    assert violations == []


def test_only_each_access_module_names_its_chunk_table():
    violations = [v for path in _sources() for v in _table_violations(path, _rel(path))]
    assert violations == []


def test_each_access_module_names_only_its_own_table():
    for table, (owner, _names) in TABLES.items():
        named = _named_tables(ast.parse((ROOT / owner).read_text(encoding="utf-8")))
        assert named == {table}, owner


def test_sync_indexes_knowledge_only_and_never_imports_notes_personal():
    """8d (this PR): only knowledge notes are indexed (docs/decisions.md,
    "index knowledge notes only"). `app/vault/sync.py` is allowed to
    import `notes_personal` (it is "the rest of app/vault/"), but this
    PR must not actually reach for it -- personal indexing is later
    work, and nothing reads a personal note until that plan lands."""
    tree = ast.parse((ROOT / "app/vault/sync.py").read_text(encoding="utf-8"))
    imported = _imported_modules(tree) | _attribute_uses(tree)
    assert not _reaches(imported, "app.vault.notes_personal")
    assert _reaches(imported, "app.vault.notes_knowledge")


def test_the_forbidden_importers_exist():
    """A renamed module must not quietly drop out of the rule."""
    for entry in FORBIDDEN_IMPORTERS:
        assert (ROOT / entry).exists(), entry


# --- guarding the guard ---

IMPORT_SAMPLES = [
    "from app.vault import notes_personal\n",
    "from app.vault import notes_knowledge as k\n",
    "import app.vault.notes_personal\n",
    "from app.vault.notes_knowledge import search\n",
    "from app.vault import consent, notes_personal\n",
    "import app.vault\napp.vault.notes_personal.search\n",
]


@pytest.mark.parametrize("code", IMPORT_SAMPLES)
@pytest.mark.parametrize(
    "rel",
    [
        "app/core/idle/research.py",
        "app/research/jobs.py",
        "app/core/notebook.py",
        "app/web/mcp.py",
        "app/core/grants.py",
        "app/tg/router.py",
        "eval/run.py",
    ],
)
def test_the_import_check_catches_every_spelling(tmp_path, code, rel):
    path = tmp_path / "sample.py"
    path.write_text(code)
    assert _import_violations(path, rel)


@pytest.mark.parametrize("rel", ["app/core/turn.py", "app/vault/sync.py"])
def test_the_allowed_importers_pass(tmp_path, rel):
    path = tmp_path / "sample.py"
    path.write_text("from app.vault import notes_personal, notes_knowledge\n")
    assert _import_violations(path, rel) == []


def test_mcp_core_may_import_notes_knowledge_only(tmp_path):
    """Connector milestone C3: app/web/mcp_core.py is the one app/web/
    module allowed to reach notes_knowledge (search_library), and it
    still may not reach notes_personal -- app/web/ stays forbidden
    there, with no exception."""
    path = tmp_path / "sample.py"
    path.write_text("from app.vault import notes_knowledge\n")
    assert _import_violations(path, "app/web/mcp_core.py") == []

    path.write_text("from app.vault import notes_personal\n")
    assert _import_violations(path, "app/web/mcp_core.py")

    # No other app/web/ module gets the exception.
    path.write_text("from app.vault import notes_knowledge\n")
    assert _import_violations(path, "app/web/mcp_claude.py")


TABLE_SAMPLES = [
    "from app.db.models import NoteChunkPersonal\n",
    "from app.db import models\nmodels.NoteChunkKnowledge\n",
    "q = 'select text from note_chunk_personal'\n",
    "q = f'SELECT * FROM NOTE_CHUNK_KNOWLEDGE where id = {1}'\n",
    "def f():\n    return 'note_chunk_personal'\n",
]


@pytest.mark.parametrize("code", TABLE_SAMPLES)
def test_the_table_check_catches_models_and_sql(tmp_path, code):
    path = tmp_path / "sample.py"
    path.write_text(code)
    assert _table_violations(path, "app/core/grants.py")


def test_the_table_check_ignores_docstrings_but_not_other_strings(tmp_path):
    path = tmp_path / "sample.py"
    path.write_text('"""Mentions note_chunk_personal in prose."""\n\ndef f():\n    """So does this: NoteChunkKnowledge."""\n')
    assert _table_violations(path, "app/core/grants.py") == []
    path.write_text('"""Prose."""\nx = "note_chunk_personal"\n')
    assert _table_violations(path, "app/core/grants.py")


def test_the_other_access_module_may_not_name_a_table(tmp_path):
    path = tmp_path / "sample.py"
    path.write_text("from app.db.models import NoteChunkKnowledge\n")
    assert _table_violations(path, "app/vault/notes_personal.py")

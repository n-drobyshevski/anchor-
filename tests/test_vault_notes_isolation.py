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
- **L1, the lens** (anchor-lens-plan.md section 5): `app.vault.lens` is
  a third access module, owning `lens_note`, `note_link`,
  `lens_version` and `lens_read`. Its importers are named one by one --
  the sync pass and /lens -- not "the rest of app/vault/". L2 adds
  `lens_round` to what it owns.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCANNED = ("app", "eval", "scripts")

NOTE_MODULES = ("app.vault.notes_personal", "app.vault.notes_knowledge", "app.vault.lens")

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
    # L1 (anchor-lens-plan.md sections 5 and 11): the sync pass writes the
    # lens tables, and /lens reads counts from them and flips Claude
    # Code's switch (the daily Claude digest asks app/tg/lens.py for its
    # line, and does not import this module itself). Nothing in Echo
    # reads a lens note's text in L1. The scheduler asks only whether the
    # door is open or was read through, so a digest is queued even with
    # both flags off. L2: the weekly review's selector and grounding call
    # (plan sections 7 and 10: the review reaches the lens, via the
    # selector); app/core/review.py itself goes through it, never here.
    # The eval harness seeds its synthetic lens notes through this
    # module's own writers and reads the round back through its catalog
    # (L2's lens cases, plan section 13) -- a throwaway database, never
    # a runtime consumer, and still no table named outside this module.
    "app.vault.lens": (
        "app/vault/sync.py",
        "app/tg/lens.py",
        "app/core/scheduler.py",
        "app/core/lens_review.py",
        "eval/scenario.py",
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
    # L1: the lens's four tables, all app/vault/lens.py's.
    "lens_note": ("app/vault/lens.py", ("lens_note", "LensNote")),
    "note_link": ("app/vault/lens.py", ("note_link", "NoteLink")),
    "lens_version": ("app/vault/lens.py", ("lens_version", "LensVersion")),
    "lens_read": ("app/vault/lens.py", ("lens_read", "LensRead")),
    # L2: the review's rounds (plan section 7), the same module's.
    "lens_round": ("app/vault/lens.py", ("lens_round", "LensRound")),
}
# L2: words that contain a lens table's name without meaning the table.
# review_proposal's two new columns hold ids, never a note's text, and
# review code and the card read and write them by name; the two
# settings bound a round (app/config.py, app/core/lens_review.py). Only
# these exact words are set aside before the substring match below; any
# other word containing a table's name still counts.
NOT_TABLE_WORDS = (
    "lens_round_id",
    "lens_note_ids",
    "lens_round_max_notes",
    "lens_round_max_chars",
)
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


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def _attribute_uses(tree: ast.AST) -> set[str]:
    """`app.vault.notes_personal` reached as an attribute, after `import app.vault`.

    `lens` is a common word, so it counts only when reached through
    something named `vault` (`app.vault.lens`, `vault.lens`); the two
    `notes_*` names are distinctive enough to count anywhere."""
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or f"app.vault.{node.attr}" not in NOTE_MODULES:
            continue
        if node.attr == "lens" and not _dotted(node.value).split(".")[-1] == "vault":
            continue
        found.add(f"app.vault.{node.attr}")
    return found


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
        words = [word for word in words if word.lower() not in NOT_TABLE_WORDS]
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
    owned: dict[str, set[str]] = {}
    for table, (owner, _names) in TABLES.items():
        owned.setdefault(owner, set()).add(table)
    for owner, tables in owned.items():
        named = _named_tables(ast.parse((ROOT / owner).read_text(encoding="utf-8")))
        assert named == tables, owner


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
    "from app.vault import lens\n",
    "from app.vault.lens import stored\n",
    "import app.vault.lens\n",
    "import app.vault\napp.vault.lens.stored\n",
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
    "from app.db.models import LensNote\n",
    "q = 'select body from lens_note'\n",
    "q = 'select unresolved_text from note_link'\n",
    "q = 'insert into lens_read (fn, rows) values (1, 2)'\n",
    "from app.db.models import LensRound\n",
    "q = 'select rationale from lens_round'\n",
    "proposal.lens_round_ids\n",
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


def test_id_columns_and_round_settings_are_not_table_names(tmp_path):
    """L2: review code stores and the card reads a proposal's round id and
    note ids by name, and the selector reads its two bounds; those words
    alone are not the lens tables."""
    path = tmp_path / "sample.py"
    path.write_text(
        "def f(proposal):\n"
        "    return ReviewProposal(lens_round_id=1, lens_note_ids=[2]), "
        "proposal.lens_round_id, proposal.lens_note_ids, settings.LENS_ROUND_MAX_NOTES\n"
    )
    assert _table_violations(path, "app/tg/review.py") == []


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


# --- L1: the lens ---


def test_only_sync_and_tg_lens_import_the_lens_module(tmp_path):
    """Not "the rest of app/vault/": the lens module's importers are
    named one by one (anchor-lens-plan.md section 5)."""
    path = tmp_path / "sample.py"
    path.write_text("from app.vault import lens\n")
    for rel in ("app/vault/sync.py", "app/tg/lens.py"):
        assert _import_violations(path, rel) == [], rel
    for rel in ("app/vault/status.py", "app/tg/claude.py", "app/core/turn.py", "app/web/mcp_core.py"):
        assert _import_violations(path, rel), rel


def test_sync_imports_the_lens_module_and_tg_lens_does_too():
    """The allowlist is not aspirational: both named importers exist and
    use it, so a rename cannot leave the rule guarding nothing."""
    for rel in ("app/vault/sync.py", "app/tg/lens.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        assert _reaches(_imported_modules(tree) | _attribute_uses(tree), "app.vault.lens"), rel


def test_the_review_reaches_the_lens_only_through_lens_review(tmp_path):
    """L2: the selector module is the review's one door to the lens, and
    it does use it; the review module itself is still refused."""
    tree = ast.parse((ROOT / "app/core/lens_review.py").read_text(encoding="utf-8"))
    assert _reaches(_imported_modules(tree) | _attribute_uses(tree), "app.vault.lens")
    path = tmp_path / "sample.py"
    path.write_text("from app.vault import lens\n")
    assert _import_violations(path, "app/core/lens_review.py") == []
    assert _import_violations(path, "app/core/review.py")


def test_an_unrelated_lens_attribute_is_not_an_import(tmp_path):
    path = tmp_path / "sample.py"
    path.write_text("def f(camera):\n    return camera.lens\n")
    assert _import_violations(path, "app/core/grants.py") == []

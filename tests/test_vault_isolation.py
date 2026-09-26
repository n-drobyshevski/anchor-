"""What app/vault/ may import, and that the bot never imports vaultd (phase-8 plan 13).

**The sync path makes no model call** and can change no `user_state`
field. Rather than trusting a docstring, this walks the AST of every
module in app/vault/ and fails on an import of anything that could: an
LLM provider, `update_state`, the outbound machinery, proposals, persona
loading, or the worker (which would drag all of those in). The same
shape as tests/test_research_isolation.py and the extractor's
`update_state` test.

**The bot and vaultd are separate projects.** vaultd is the security
boundary; if the bot imported it, "the vault refuses" would quietly
become "the bot's copy of the vault's code refuses". The mirror of this
check lives in vaultd/tests/test_independence.py.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VAULT = ROOT / "app" / "vault"

# module -> why app/vault/ must not import it.
FORBIDDEN_IMPORTS = {
    "app.llm": "the sync path makes no model call",
    "app.llm.provider": "the sync path makes no model call",
    "app.llm.openrouter": "the sync path makes no model call",
    "app.core.outbound": "the vault sends nothing proactive through the outbound path",
    "app.core.outbound_send": "the vault sends nothing proactive through the outbound path",
    "app.core.outbound_gate": "the vault sends nothing proactive through the outbound path",
    "app.core.proposal": "the vault proposes nothing",
    "app.core.prompt": "persona loading has no place in the sync path",
    "app.core.turn": "the sync path runs no persona turn",
    "app.core.tick": "the sync path runs no tick",
    "app.core.extract": "the sync path runs no extractor",
    "app.worker": "plan section 8: app/vault/ must not import app.worker",
}
# Names that must not appear in app/vault/ code, imported from anywhere.
FORBIDDEN_NAMES = {"update_state", "load_persona", "run_turn"}


def _modules() -> list[Path]:
    modules = sorted(VAULT.glob("*.py"))
    assert modules, "app/vault/ has no modules"
    return modules


def _imports(tree: ast.AST) -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((alias.name, []) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append((node.module, [alias.name for alias in node.names]))
    return out


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for module, names in _imports(tree):
        if module in FORBIDDEN_IMPORTS or module.startswith("app.llm"):
            found.append(f"{path.name}: imports {module}")
        for name in names:
            if name in FORBIDDEN_NAMES:
                found.append(f"{path.name}: imports {name}")
            if f"{module}.{name}" in FORBIDDEN_IMPORTS:
                found.append(f"{path.name}: imports {module}.{name}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append(f"{path.name}: uses {node.id}")
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            found.append(f"{path.name}: uses .{node.attr}")
    return found


def test_app_vault_imports_nothing_that_could_call_a_model_or_write_state():
    violations = [v for path in _modules() for v in _violations(path)]
    assert violations == []


def test_the_check_catches_each_kind_of_violation(tmp_path):
    """Guards the guard."""
    samples = {
        "a.py": "from app.worker import process_one_update\n",
        "b.py": "from app.core.state import update_state\n",
        "c.py": "import app.llm.openrouter\n",
        "d.py": "from app.core import state\nstate.update_state(None, 'x', 1, 'vault')\n",
        "e.py": "from app.core import proposal\n",
    }
    for name, code in samples.items():
        path = tmp_path / name
        path.write_text(code)
        assert _violations(path), name


def test_the_bot_never_imports_vaultd():
    offenders = []
    for path in sorted((ROOT / "app").rglob("*.py")) + sorted((ROOT / "tests").glob("*.py")):
        for module, _ in _imports(ast.parse(path.read_text(encoding="utf-8"))):
            if module.split(".")[0] == "vaultd":
                offenders.append(f"{path.relative_to(ROOT)} imports {module}")
    assert offenders == []


# 8c: the vault may change memory only through these three (plan
# section 13). Nothing in app/vault/ may name any other memory writer --
# hard_delete and add_pending in particular, which 8b's rule forbade
# outright. Whether a given call actually *runs* in mirror mode is a
# runtime question, not a static one: mirror's "applies nothing" is
# pinned by tests/test_vault_sync.py's own behavioural tests (an edit
# in the vault changes disk_sha256 and nothing else), which this AST
# scan cannot see.
MEMORY_WRITERS = {"write_memory", "set_pinned", "forget_lineage"}
FORBIDDEN_MEMORY_WRITERS = {"hard_delete", "add_pending"}


def test_only_the_three_memory_writers_are_ever_named():
    offenders = []
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
            if name in FORBIDDEN_MEMORY_WRITERS:
                offenders.append(f"{path.name}: {name}")
    assert offenders == []


def test_the_check_catches_a_forbidden_writer(tmp_path):
    path = tmp_path / "z.py"
    path.write_text("from app.core.memory import hard_delete\nhard_delete(1)\n")
    tree = ast.parse(path.read_text())
    found = [
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute))
    ]
    assert "hard_delete" in found


# 8e: notes consent is the one user_state column app/vault/ writes, and
# consent.py is its only writer. Any other module here that so much as
# builds an UPDATE of UserState fails.
def _user_state_writes(tree: ast.AST) -> list[str]:
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "values"):
            continue
        # Any statement built on UserState, however `update` was imported
        # or aliased: app/vault/ has no other reason to call .values on it.
        receiver = ast.unparse(node.func.value)
        if "UserState" in receiver:
            found.extend(k.arg or "**" for k in node.keywords)
    return found


def test_only_consent_writes_user_state_and_only_notes_consent():
    for path in _modules():
        writes = _user_state_writes(ast.parse(path.read_text(encoding="utf-8")))
        if path.name == "consent.py":
            assert writes == ["notes_consent"]
        else:
            assert writes == [], f"{path.name} writes user_state: {writes}"


@pytest.mark.parametrize(
    "code",
    [
        "update(UserState).where(UserState.id == 1).values(vault_epoch='x')\n",
        "from sqlalchemy import update as _u\n_u(UserState).values(vault_epoch='x')\n",
        "sa.update(models.UserState).values(vault_epoch='x')\n",
    ],
)
def test_the_user_state_check_catches_a_write(code):
    assert _user_state_writes(ast.parse(code)) == ["vault_epoch"]

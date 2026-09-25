"""What app/vault/ may import, and that the bot never imports vaultd (phase-5 plan 13).

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


# 5b: mirror records the vault's edits and applies none of them. Until
# 5c, nothing in app/vault/ may so much as name a function that changes
# memory. 5c replaces this with the narrower rule of plan section 13
# (only write_memory, set_pinned and forget_lineage).
MEMORY_WRITERS = {"write_memory", "set_pinned", "hard_delete", "forget_lineage", "add_pending"}


def test_mirror_mode_applies_nothing_to_memory():
    offenders = []
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
            if name in MEMORY_WRITERS:
                offenders.append(f"{path.name}: {name}")
    assert offenders == []

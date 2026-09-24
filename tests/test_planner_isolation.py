"""Nothing that builds the persona or its memory may import app.planner.

Same shape as tests/test_research_isolation.py: app/planner/ is Anchor's
MCP client of the planner, and the plan's Verification section asks for
an isolation test alongside the client/auth/snapshot tests. The
extractor, the scene summarizer and memory retrieval must never reach
into it directly -- they see only the plain-string lines
app/core/prompt.py's build_now_block(planner=...) already renders, the
same discipline app/core/prompt.py's own docstring states for
memory ids.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

MODULES = (
    pathlib.Path("app/core/extract.py"),
    pathlib.Path("app/core/scene.py"),
    pathlib.Path("app/core/memory.py"),
)


def _imported_names(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_module_imports_app_planner(path: pathlib.Path) -> None:
    assert path.exists(), f"{path} is missing -- this test would pass vacuously"
    names = _imported_names(path)
    violations = [name for name in names if name == "app.planner" or name.startswith("app.planner.")]
    assert not violations, f"{path}: imports {violations}"


def test_the_detector_would_actually_catch_a_violation(tmp_path: pathlib.Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("from app.planner import snapshot\n", encoding="utf-8")
    assert "app.planner" in _imported_names(sample)

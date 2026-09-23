"""Idle never sends Telegram messages and never touches live state (plan
section 8's invariants; approved plan §5's test list for milestone 6a).

Two halves, copying tests/test_autonomy_isolation.py's own structure and
self-tests (that file's module docstring is the pattern this one
follows, aimed at app/core/idle/ instead):

- An AST import-restriction scan over every module in app/core/idle/.
- A mock-bot test: every implemented idle kind, run end to end, must
  send and edit nothing.
"""

from __future__ import annotations

import ast
import datetime
import pathlib

import pytest

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.idle import BACKFILL, CONSOLIDATE, IDLE_RUN, REFLECT
from app.db.models import IdleRun, Memory, Message, Scene, UserState
from conftest import FakeLLMProvider, make_bot

MODULES = sorted(pathlib.Path("app/core/idle").glob("*.py"))

# Same reason table shape as tests/test_autonomy_isolation.py's own
# FORBIDDEN_IMPORTS -- reasons are data so a failure explains itself.
FORBIDDEN_IMPORTS = {
    "app.core.state": "writes user_state (update_state/set_counters/record_change)",
    "app.core.outbound": "the outbound gate's own state loader and counters",
    "app.core.outbound_gate": "decides whether a proactive message may be sent",
    "app.core.outbound_send": "sends proactive messages",
    "app.core.scheduler": "plans proactive sends",
    "app.core.checkin": "writes the streak and the daily check-in",
    "app.core.orders": "standing orders -- not an idle-writable table",
    "app.core.amendments": "persona amendments -- not an idle-writable table",
    "app.core.review": "the weekly review -- writes WeeklyReview/ReviewProposal",
    "app.core.cards": "research card adoption",
    "app.research.jobs": "the /study pipeline -- 6a does not run research",
}

FORBIDDEN_PREFIXES = ("app.tg",)


def _code_without_docstrings(path: pathlib.Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _imported_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_there_are_idle_modules_to_check():
    """Guards the guard: a glob matching nothing makes every assertion
    below vacuously true."""
    assert len(MODULES) >= 6
    assert any(p.name == "gate.py" for p in MODULES)
    assert any(p.name == "runner.py" for p in MODULES)


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_idle_module_imports_a_forbidden_name(path):
    stripped = _code_without_docstrings(path)
    tree = ast.parse(stripped)
    imported = _imported_names(tree)
    violations = [
        f"{path}: imports {name} ({FORBIDDEN_IMPORTS[name]})"
        for name in imported
        if name in FORBIDDEN_IMPORTS
    ]
    violations.extend(
        f"{path}: imports {name} (app/tg/ is the Telegram layer)"
        for name in imported
        if any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN_PREFIXES)
    )
    assert not violations, "\n".join(violations)


def test_the_detector_would_actually_catch_a_violation():
    """Guards the guard, exactly like test_autonomy_isolation.py's own
    version of this test."""
    sample = ast.parse(
        "from app.core.state import update_state\n"
        "from app.core.outbound_gate import gate\n"
        "import app.tg.router\n"
    )
    imported = _imported_names(sample)
    assert "app.core.state" in imported
    assert "app.core.outbound_gate" in imported
    assert any(name.startswith("app.tg") for name in imported)


def test_docstring_stripping_does_not_flag_prose_about_the_rule(tmp_path):
    """A module's own docstring may *name* app.core.state while
    explaining why it must never import it -- same self-test as
    tests/test_autonomy_isolation.py's."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        '"""This module must never import app.core.state."""\n'
        "def f():\n"
        '    """Nor app.tg."""\n'
        "    return 1\n",
        encoding="utf-8",
    )
    stripped = _code_without_docstrings(sample)
    assert "app.core.state" not in stripped
    assert "app.tg" not in stripped
    assert _imported_names(ast.parse(stripped)) == []


# --- the mock-bot test: zero sends, zero edits ------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [BACKFILL, CONSOLIDATE, REFLECT])
async def test_idle_kind_never_sends_or_edits(sessionmaker, monkeypatch, kind):
    """Every implemented idle kind, run through the worker's own dispatch
    with a real (fake-transport) bot in hand: zero Telegram calls of any
    kind. `Bot.__call__` is where every aiogram method goes, so patching
    it also catches a Bot the idle code might build for itself.
    Parametrized so 6c-6d extend it automatically -- 6b adds
    CONSOLIDATE and REFLECT."""
    from aiogram import Bot

    from app.worker import _run_job

    calls: list[str] = []
    original_call = Bot.__call__

    async def _recording_call(self, method, *args, **kwargs):
        calls.append(type(method).__name__)
        return await original_call(self, method, *args, **kwargs)

    monkeypatch.setattr(Bot, "__call__", _recording_call)

    clock = FrozenClock(datetime.datetime(2026, 9, 23, 12, 0, tzinfo=datetime.timezone.utc))
    now = clock.now_utc()
    merge_ids: tuple[int, int] | None = None
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, timezone="Europe/Paris"))
        scene = Scene(
            started_at=now - datetime.timedelta(hours=5),
            ended_at=now - datetime.timedelta(hours=4),
            # BACKFILL needs an un-summarized scene to pick up; CONSOLIDATE
            # and REFLECT need something already summarized -- CONSOLIDATE
            # doesn't read scenes at all, REFLECT's kind rule needs a
            # fresh summary to fire on.
            summary=None if kind == BACKFILL else "Коротко поговорили.",
        )
        session.add(scene)
        await session.commit()
        await session.refresh(scene)
        scene_id = scene.id
        session.add_all(
            [
                Message(role="user", content="привет", ooc=False, kind="chat", scene_id=scene_id),
                Message(role="assistant", content="привет!", ooc=False, kind="chat", scene_id=scene_id),
                Message(role="user", content="как дела", ooc=False, kind="chat", scene_id=scene_id),
            ]
        )
        if kind == CONSOLIDATE:
            # Two clusters, so the gate's `kind_rule:not_enough_clusters`
            # (>= 2 required) passes.
            m1 = Memory(kind="identity", text="живёт в Лилле", source="extractor")
            m2 = Memory(kind="identity", text="живёт в Лилле, во Франции", source="extractor")
            m3 = Memory(kind="preference", text="работает программистом", source="extractor")
            m4 = Memory(kind="preference", text="работает программистом в стартапе", source="extractor")
            session.add_all([m1, m2, m3, m4])
            await session.commit()
            await session.refresh(m1)
            await session.refresh(m2)
            merge_ids = (m1.id, m2.id)
        run = IdleRun(kind=kind, local_date=now.date(), status="queued")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    bot, fake = make_bot()
    provider = FakeLLMProvider(text="Коротко: поговорили.")
    if kind == CONSOLIDATE:
        safety_text = (
            '{"merges": [{"ids": [%d, %d], "text": "живёт в Лилле", "kind": "identity"}], '
            '"contradictions": []}' % merge_ids
        )
    else:
        safety_text = '{"add": [], "close": [], "update": []}'
    safety_provider = FakeLLMProvider(text=safety_text)

    async with sessionmaker() as session:
        await _run_job(
            session, Settings(), provider, provider, bot, clock, IDLE_RUN,
            {"run_id": run_id}, safety_provider=safety_provider, sessionmaker=sessionmaker,
        )

    # Not vacuous: the run really did its work.
    async with sessionmaker() as session:
        assert (await session.get(IdleRun, run_id)).status == "done"
        if kind == BACKFILL:
            assert (await session.get(Scene, scene_id)).summary == "Коротко: поговорили."
        elif kind == CONSOLIDATE:
            assert (await session.get(Memory, merge_ids[0])).superseded_by is not None
    assert calls == []
    assert fake.sent == []
    assert fake.edits == []
    assert fake.documents == []

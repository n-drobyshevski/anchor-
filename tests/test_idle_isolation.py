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
import decimal
import pathlib

import pytest

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.idle import (
    BACKFILL,
    CANARY,
    CONSOLIDATE,
    CRITIQUE,
    IDLE_RUN,
    PREBRIEF,
    REFLECT,
    RESEARCH,
)
from app.db.models import (
    BriefNote,
    IdleRun,
    InterestTopic,
    Memory,
    Message,
    PersonaAmendment,
    Scene,
    UserState,
)
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
    # 8e (8e plan sections 7-8): vault notes reach only the persona's
    # turn. Personal note text never reaches an idle model, in any phase;
    # knowledge note text does not in 8e either.
    "app.vault.notes_personal": "personal vault notes reach only the persona's turn",
    "app.vault.notes_knowledge": "knowledge vault notes reach only the persona's turn in 8e",
    # 6d: `app.research.jobs` is deliberately no longer banned.
    # app/core/idle/research.py (and, for its own gate fact,
    # app/core/idle/facts.py) calls `run_research_job`/`study_quota_used`
    # directly -- see that module's own docstring on why the *pipeline*
    # runs unchanged while the *queueing* is idle's own. Every other
    # entry in this table still stands: research never adopts a card
    # (`app.core.cards` stays banned) and never writes anything this
    # table's other rows forbid.
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
    """Every module path an import can reach, in both spellings.

    `from app.core.state import x` records `app.core.state`; `from
    app.core import state` records `app.core` *and* `app.core.state`
    (the name may be a submodule). Before phase-6 package D only the
    first spelling was recorded, so `from app.core import state` or
    `from app import tg` slipped straight past the ban -- and the idle
    modules already use that spelling for their allowed imports.
    """
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return names


# Function names that must not be imported into idle code from anywhere,
# whatever module they come from: the live-control writer and card
# adoption. (Outbound sending is covered by the module bans above plus
# the app.tg prefix.)
FORBIDDEN_SYMBOLS = {
    "update_state": "writes a live control field of user_state",
    "set_counters": "writes the outbound counters",
    "adopt": "adopts a research card (or an amendment)",
}


def _imported_symbols(tree: ast.AST) -> list[str]:
    return [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ]


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
    violations.extend(
        f"{path}: imports {name} ({FORBIDDEN_SYMBOLS[name]})"
        for name in _imported_symbols(tree)
        if name in FORBIDDEN_SYMBOLS
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


@pytest.mark.parametrize(
    ("source", "caught"),
    [
        ("from app.core import state\n", "app.core.state"),
        ("from app.core import outbound_send\n", "app.core.outbound_send"),
        ("from app.core import cards\n", "app.core.cards"),
        ("from app import tg\n", "app.tg"),
    ],
)
def test_the_detector_catches_the_from_package_import_spelling(source, caught):
    assert caught in _imported_names(ast.parse(source))


def test_the_detector_catches_a_forbidden_symbol_from_anywhere():
    tree = ast.parse(
        "from app.some.reexport import update_state\n"
        "from app.core.somewhere import adopt as take\n"
    )
    assert {"update_state", "adopt"} <= set(_imported_symbols(tree))


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
@pytest.mark.parametrize(
    "kind", [BACKFILL, CONSOLIDATE, REFLECT, PREBRIEF, CRITIQUE, CANARY, RESEARCH]
)
async def test_idle_kind_never_sends_or_edits(sessionmaker, monkeypatch, kind):
    """Every implemented idle kind, run through the worker's own dispatch
    with a real (fake-transport) bot in hand: zero Telegram calls of any
    kind. `Bot.__call__` is where every aiogram method goes, so patching
    it also catches a Bot the idle code might build for itself.
    Parametrized so 6c-6d extend it automatically -- 6b added
    CONSOLIDATE and REFLECT, 6c adds PREBRIEF, CRITIQUE and CANARY, 6d
    adds RESEARCH. RESEARCH is the one kind this asserts *no completion
    message* for on top of the shared "no Telegram calls at all" --
    plan section 6.5's own "no completion message is sent" is exactly
    the same property this test already checks for every other kind, so
    it needs no extra assertion, only the setup to make the run do real
    (mocked-network) work."""
    from aiogram import Bot

    from app.worker import _run_job

    calls: list[str] = []
    original_call = Bot.__call__

    async def _recording_call(self, method, *args, **kwargs):
        calls.append(type(method).__name__)
        return await original_call(self, method, *args, **kwargs)

    monkeypatch.setattr(Bot, "__call__", _recording_call)

    # PREBRIEF's kind rule only allows after 19:00 local (Europe/Paris);
    # every other kind here is fine at noon.
    base_hour = 18 if kind == PREBRIEF else 12
    clock = FrozenClock(datetime.datetime(2026, 9, 23, base_hour, 0, tzinfo=datetime.timezone.utc))
    now = clock.now_utc()
    merge_ids: tuple[int, int] | None = None
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, timezone="Europe/Paris", due_action="позвонить"))
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
        if kind == CANARY:
            session.add(PersonaAmendment(text="меньше вопросов", status="active", persona_sha="x"))
        if kind == RESEARCH:
            session.add(InterestTopic(text="бессонница", packet="forums", active=True))
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
    elif kind == PREBRIEF:
        safety_text = '{"notes": ["Коротко: сегодня был спокойный день."]}'
    elif kind == RESEARCH:
        # research's "safety_provider" is the distill model
        # (app/core/idle/research.py's own docstring: the same one
        # app/worker.py's RESEARCH branch gives run_research_job). The
        # quote must be a verbatim substring of the fetched clip text
        # below (distill.validate's own anchor check).
        safety_text = (
            '{"cards": [{"kind": "technique", "text": "Совет со страницы.", '
            '"quote": "Спать лучше в прохладной комнате.", "risk": "low"}]}'
        )
    else:
        safety_text = '{"add": [], "close": [], "update": []}'
    safety_provider = FakeLLMProvider(text=safety_text)

    if kind == CRITIQUE:
        # run_critique builds its own judge provider (LLM_MODEL_JUDGE)
        # lazily -- see app/core/idle/critique.py's own docstring on why
        # it is not threaded through app/worker.py. Patched here so this
        # test never reaches the network.
        judge_fake = FakeLLMProvider(
            text='{"voice": 5, "one_action": 5, "boundaries": 5, "no_pressure": 5, "third_parties": 5}'
        )

        class _DummyClient:
            async def close(self):
                pass

        monkeypatch.setattr("app.llm.openrouter.build_client", lambda api_key: _DummyClient())
        monkeypatch.setattr(
            "app.core.idle.critique._build_judge_provider", lambda settings, client: judge_fake
        )
    if kind == CANARY:
        # run_canary reuses eval.trial.run_blocking_subset, which opens
        # its own throwaway database -- monkeypatched here so this test
        # (and its mock bot) stays fast and never touches the network.
        async def _fake_run_blocking_subset(settings, *, clock, amendments, on_case_done=None, **kw):
            if on_case_done is not None:
                await on_case_done()
            from eval.trial import TrialResult

            return TrialResult(cases={"01": True}, passed=True, usd_cost=0.0)

        monkeypatch.setattr("eval.trial.run_blocking_subset", _fake_run_blocking_subset)

    if kind == RESEARCH:
        # run_research_job's own network seams -- see app/core/idle/
        # research.py's own docstring: the pipeline runs unchanged, so
        # this test patches the same module-level names
        # tests/test_research_jobs.py's fixtures stand in for, never
        # app/core/idle/research.py itself.
        from app.llm.provider import LLMResponse, LLMUsage
        from app.research import search
        from app.research.fetch import Clip

        clip = Clip(
            url="https://reddit.com/r/sleep/comment",
            domain="reddit.com",
            title="Совет",
            text="Спать лучше в прохладной комнате.",
            text_sha256="deadbeef",
            http_status=200,
        )

        async def _fake_fetch(url, **kwargs):
            return clip

        async def _fake_find_urls(provider, **kwargs):
            usage = LLMUsage(
                input_tokens=100, cached_tokens=0, output_tokens=3,
                cost_usd=decimal.Decimal("0.002"),
            )
            return search.SearchOutcome(
                urls=(clip.url,),
                responses=(LLMResponse(text="", usage=usage, model="fake-safety"),),
            )

        monkeypatch.setattr("app.research.jobs.default_fetch", _fake_fetch)
        monkeypatch.setattr("app.research.search.find_urls", _fake_find_urls)

    settings = Settings(RESEARCH_ENABLED=True) if kind == RESEARCH else Settings()

    async with sessionmaker() as session:
        await _run_job(
            session, settings, provider, provider, bot, clock, IDLE_RUN,
            {"run_id": run_id}, safety_provider=safety_provider, sessionmaker=sessionmaker,
        )

    # Not vacuous: the run really did its work.
    async with sessionmaker() as session:
        assert (await session.get(IdleRun, run_id)).status == "done"
        if kind == BACKFILL:
            assert (await session.get(Scene, scene_id)).summary == "Коротко: поговорили."
        elif kind == CONSOLIDATE:
            assert (await session.get(Memory, merge_ids[0])).superseded_by is not None
        elif kind == PREBRIEF:
            tomorrow = now.date() + datetime.timedelta(days=1)
            assert (await session.get(BriefNote, tomorrow)) is not None
        elif kind == CRITIQUE:
            run = await session.get(IdleRun, run_id)
            assert run.summary.get("count") == 1
        elif kind == CANARY:
            run = await session.get(IdleRun, run_id)
            assert run.summary.get("cases") == {"01": True}
        elif kind == RESEARCH:
            run = await session.get(IdleRun, run_id)
            assert run.summary.get("cards") == 1
    assert calls == []
    assert fake.sent == []
    assert fake.edits == []
    assert fake.documents == []


# --- 6c: welfare/OOC/canned exclusion, and "summary is never text" ------


@pytest.mark.asyncio
async def test_critique_input_excludes_welfare_ooc_and_canned(sessionmaker):
    """A qualifying persona reply exists alongside welfare/canned/OOC
    rows; the sample must never pick up the excluded ones (plan section
    8: "Welfare, OOC and canned rows never enter idle inputs")."""
    from app.core.idle.critique import _sample

    async with sessionmaker() as session:
        scene = Scene(
            started_at=datetime.datetime(2026, 9, 23, tzinfo=datetime.timezone.utc), ended_at=None
        )
        session.add(scene)
        await session.commit()
        await session.refresh(scene)
        session.add_all(
            [
                Message(role="assistant", content="берегись", ooc=False, kind="welfare", scene_id=scene.id),
                Message(role="assistant", content="шаблон", ooc=False, kind="canned", scene_id=scene.id),
                Message(role="assistant", content="о своём", ooc=True, kind="chat", scene_id=scene.id),
                Message(role="assistant", content="настоящий ответ", ooc=False, kind="chat", scene_id=scene.id),
            ]
        )
        await session.commit()

    async with sessionmaker() as session:
        sample = await _sample(session, 10)
    assert [m.content for m in sample] == ["настоящий ответ"]


def test_prebrief_input_never_reads_messages_at_all():
    """prebrief's own input is Checkin/StandingOrder/notebook/due_action
    only (module docstring) -- it never imports app.db.models.Message,
    so welfare/OOC/canned rows structurally cannot reach it."""
    import app.core.idle.prebrief as prebrief_module

    tree = ast.parse(pathlib.Path(prebrief_module.__file__).read_text(encoding="utf-8"))
    imported_names = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "Message" not in imported_names


@pytest.mark.asyncio
async def test_critique_and_canary_summary_is_numbers_ids_and_codes_only(sessionmaker, monkeypatch):
    """plan section 8: "Logs and idle_run.summary contain no text" --
    every value in a critique/canary idle_run.summary must be an int,
    float, bool, or a dict/list composed only of those plus short id-
    or code-shaped strings (never a free-text sentence)."""

    def _is_safe(value) -> bool:
        if isinstance(value, bool):
            return True
        if isinstance(value, (int, float)):
            return True
        if isinstance(value, str):
            # ids/codes only -- short, no spaces (a sentence has spaces).
            return " " not in value and len(value) <= 32
        if isinstance(value, list):
            return all(_is_safe(v) for v in value)
        if isinstance(value, dict):
            return all(_is_safe(k) for k in value) and all(_is_safe(v) for v in value.values())
        return False

    clock = FrozenClock(datetime.datetime(2026, 9, 23, 12, 0, tzinfo=datetime.timezone.utc))
    now = clock.now_utc()
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, timezone="Europe/Paris"))
        scene = Scene(started_at=now, ended_at=None)
        session.add(scene)
        await session.commit()
        await session.refresh(scene)
        session.add_all(
            [
                Message(role="user", content="привет", ooc=False, kind="chat", scene_id=scene.id),
                Message(role="assistant", content="привет!", ooc=False, kind="chat", scene_id=scene.id),
            ]
        )
        run = IdleRun(kind=CRITIQUE, local_date=now.date(), status="queued")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    judge_fake = FakeLLMProvider(
        text='{"voice": 2, "one_action": 5, "boundaries": 2, "no_pressure": 5, "third_parties": 5}'
    )
    monkeypatch.setattr(
        "app.core.idle.critique._build_judge_provider", lambda settings, client: judge_fake
    )

    bot, fake = make_bot()
    from app.worker import _run_job

    async with sessionmaker() as session:
        await _run_job(
            session, Settings(), FakeLLMProvider(), FakeLLMProvider(), bot, clock, IDLE_RUN,
            {"run_id": run_id}, safety_provider=FakeLLMProvider(), sessionmaker=sessionmaker,
        )

    async with sessionmaker() as session:
        run = await session.get(IdleRun, run_id)
        assert _is_safe(run.summary), run.summary

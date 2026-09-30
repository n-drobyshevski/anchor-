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
    LENS_GARDEN,
    LENS_RESEARCH,
    PREBRIEF,
    REFLECT,
    RESEARCH,
)
from app.db.models import (
    BriefNote,
    IdleRun,
    InterestTopic,
    LensNote,
    Memory,
    Message,
    NoteLink,
    PersonaAmendment,
    Scene,
    UserState,
    VaultFile,
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
    # L4 (the L4 spec sections 5 and 7): Echo writes into the vault only
    # on the user's «в Inbox» tap, never from idle.
    "app.core.echo_write": "Echo's inbox writes happen only on the user's tap",
    # 8e (8e plan sections 7-8): vault notes reach only the persona's
    # turn. Personal note text never reaches an idle model, in any phase;
    # knowledge note text does not in 8e either.
    "app.vault.notes_personal": "personal vault notes reach only the persona's turn",
    "app.vault.notes_knowledge": "knowledge vault notes reach only the persona's turn in 8e",
    # L1 (anchor-lens-plan.md section 5): nothing in Echo reads the lens
    # yet. L3-L5 allow it in the two new idle kinds and in reflect, each
    # by name, when they land -- see ALLOWED_PER_FILE below.
    "app.vault.lens": "the lens reaches only the idle kinds ALLOWED_PER_FILE names",
    # L5 (the L5 spec sections 1 and 5): the shared selector core --
    # catalog rendering, the lens block, the selection checks. Idle
    # reaches it only where it may reach the lens itself.
    "app.core.lens_select": "the lens selector core reaches only reflect's lens round",
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

# file name -> forbidden imports that one idle module may make anyway,
# each justified by its plan. Everything else in FORBIDDEN_IMPORTS, and
# `app.tg`, stays banned for it too.
ALLOWED_PER_FILE = {
    # L3 (anchor-lens-plan.md sections 5, 8 and 10; the L3 spec section
    # 8): the weekly garden reads the lens graph and summaries and
    # records its run and gaps, through app/vault/lens.py only. It sends
    # nothing: app/tg/garden.py delivers after the vault pass.
    "lens_garden.py": {"app.vault.lens"},
    # L4 (plan sections 9 and 10; the L4 spec section 7): the research a
    # garden tap asked for reads the gap's seed (its detail, its lens
    # notes' titles and summaries) through app/vault/lens.py only. It
    # sends nothing either: the result message is the garden hook's.
    "lens_research.py": {"app.vault.lens"},
    # L5 (plan sections 7 and 10; the L5 spec sections 3 and 5): the
    # idle reflect's lens round -- the selector and the grounding of the
    # draft's open threads -- reads the catalog and the selected bodies
    # and records its `reflect` round, through app/vault/lens.py and the
    # shared core in app/core/lens_select.py. It is reflect's one door
    # to the lens: reflect.py and critique.py get no entry here, and the
    # round sends nothing.
    "reflect_lens.py": {"app.vault.lens", "app.core.lens_select"},
}


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
    violations = _violations(path)
    assert not violations, "\n".join(violations)


def _violations(path: pathlib.Path) -> list[str]:
    stripped = _code_without_docstrings(path)
    tree = ast.parse(stripped)
    imported = _imported_names(tree)
    allowed = ALLOWED_PER_FILE.get(path.name, set())
    violations = [
        f"{path}: imports {name} ({FORBIDDEN_IMPORTS[name]})"
        for name in imported
        if name in FORBIDDEN_IMPORTS and name not in allowed
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
    return violations


def test_the_lens_allowance_is_the_garden_s_lens_research_s_and_reflect_lens_s_alone(tmp_path):
    """L3: `app.vault.lens` is allowed in lens_garden.py and, from L4, in
    lens_research.py; L5 adds reflect_lens.py, which alone may also
    import `app.core.lens_select` (the L5 spec section 5). Nowhere else
    in app/core/idle/ -- reflect.py and critique.py included -- may
    reach either. The allowance lifts nothing else: none of the three
    may import app.tg, a chunk module, card adoption, a state writer or
    the review."""
    idle = tmp_path / "idle"
    idle.mkdir()
    grants = {
        "lens_garden.py": ("from app.vault import lens\n",),
        "lens_research.py": ("from app.vault import lens\n",),
        "reflect_lens.py": (
            "from app.vault import lens\n",
            "from app.core.lens_select import SELECTOR_SCHEMA, select_messages\n",
            "from app.core import lens_select\n",
        ),
    }
    for name, sources in grants.items():
        allowed = idle / name
        for source in sources:
            allowed.write_text(source, encoding="utf-8")
            assert _violations(allowed) == [], (name, source)
        for source in (
            "from app.tg import garden\n",
            "from app.vault import notes_knowledge\n",
            "from app.core import state\n",
            "from app.core import cards\n",
            "from app.core import review\n",
            "from app.core.echo_write import adopt_research\n",
        ):
            allowed.write_text(source, encoding="utf-8")
            assert _violations(allowed), (name, source)
    # The selector core is reflect_lens.py's alone, not the garden's.
    for name in ("lens_garden.py", "lens_research.py"):
        other = idle / name
        other.write_text("from app.core import lens_select\n", encoding="utf-8")
        assert _violations(other), name
    for name in ("research.py", "reflect.py", "critique.py", "runner.py"):
        other = idle / name
        for source in (
            "from app.vault import lens\n",
            "from app.core import lens_select\n",
            "from app.core.lens_select import render_lens_block\n",
        ):
            other.write_text(source, encoding="utf-8")
            assert _violations(other), (name, source)


def test_reflect_reaches_the_lens_only_through_reflect_lens():
    """L5: the allowance is not aspirational -- reflect_lens.py does
    import both modules -- and reflect.py and critique.py, as written,
    import neither (critique attributes ids from app.db.models alone,
    the L5 spec section 4)."""
    idle = pathlib.Path("app/core/idle")
    imported = set(_imported_names(ast.parse(_code_without_docstrings(idle / "reflect_lens.py"))))
    assert {"app.vault.lens", "app.core.lens_select"} <= imported
    for name in ("reflect.py", "critique.py"):
        names = _imported_names(ast.parse(_code_without_docstrings(idle / name)))
        assert not any(
            n in ("app.vault.lens", "app.core.lens_select", "app.core.lens_review") for n in names
        ), name


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
    "kind",
    [BACKFILL, CONSOLIDATE, REFLECT, PREBRIEF, CRITIQUE, CANARY, RESEARCH, LENS_GARDEN, LENS_RESEARCH],
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
    (mocked-network) work. L3 adds LENS_GARDEN (the L3 spec section 8):
    it must end `done` with a gap recorded and still no Telegram call --
    its one message is app/tg/garden.py's, after the vault pass. L4 adds
    LENS_RESEARCH (the L4 spec section 2): it builds the query, searches,
    distills and ends `done` with a lens card, and still sends nothing --
    its result message is the garden hook's (app/worker.py), never
    idle's. L5 runs REFLECT with the lens active (the L5 spec section
    5): pass 1, the selector and the grounding call, a `reflect` round
    recorded and an open thread grounded -- and still nothing sent: a
    grounded entry reaches chat only through the persona prompt, on the
    user's own next turn."""
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
        garden_ids: list[int] = []
        if kind in (LENS_GARDEN, REFLECT):
            # Three synthetic lens notes; the second mentions the first
            # without a link, which is the gap the canned reply names.
            # L5: REFLECT runs with the lens active over the same three.
            files = []
            for index, (title, body) in enumerate(
                (("Кибернетика", "Наука."), ("Винер", "Основал кибернетику."), ("Эшби", "Закон."))
            ):
                file = VaultFile(path=f"Lens/n{index}.md", role="note", note_class="knowledge")
                session.add(file)
                await session.flush()
                files.append(file.id)
                row = LensNote(
                    vault_file_id=file.id, kind="concept", title=title, body=body,
                    body_hash=f"{index:064x}", chars=len(body),
                )
                session.add(row)
                await session.flush()
                garden_ids.append(row.id)
            if kind == LENS_GARDEN:
                session.add(NoteLink(src_file_id=files[2], dst_file_id=files[0]))
        run = IdleRun(kind=kind, local_date=now.date(), status="queued")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    lens_job_id = None
    if kind == LENS_RESEARCH:
        # A researched gap and its queued job, as a tap leaves them
        # (tests/test_lens_research_idle.py's helpers, synthetic notes).
        from app.research import jobs as research_jobs
        from app.vault import lens as lens_module
        from tests.test_lens_research_idle import EPOCH, GARDEN_MESSAGE, _lens_note, _sig

        async with sessionmaker() as session:
            state = await session.get(UserState, 1)
            state.vault_epoch = EPOCH
            state.notes_consent = True
            ashby = await _lens_note(session, "Ashby", "Кибернетик.")
            beer = await _lens_note(session, "Beer", "Модель жизнеспособной системы.")
            await session.commit()
            record = await lens_module.record_garden(
                session, idle_run_id=None, iso_week="2026-W39", version_id=None, findings={},
                resolved_ids=(), reopened_ids=(),
                new=[
                    lens_module.NewGap(
                        "tension", (ashby.id, beer.id), ("Ashby", "Beer"), None,
                        "Эшби и Бир о разнообразии.", _sig("t"), {},
                    )
                ],
                now=now,
            )
            await lens_module.mark_run_sent(session, record.run_id, GARDEN_MESSAGE, now=now)
            [gap_id] = record.new_ids
            assert await lens_module.request_research(
                session, gap_id, EPOCH, now, message_id=GARDEN_MESSAGE
            ) == "ok"
            lens_job_id, code = await research_jobs.enqueue_lens_study(
                session, _lens_settings(), clock, gap_id=gap_id, timezone="Europe/Paris"
            )
            assert code is None
            await session.commit()

    bot, fake = make_bot()
    provider = FakeLLMProvider(text="Коротко: поговорили.")
    if kind == CONSOLIDATE:
        safety_text = (
            '{"merges": [{"ids": [%d, %d], "text": "живёт в Лилле", "kind": "identity"}], '
            '"contradictions": []}' % merge_ids
        )
    elif kind == PREBRIEF:
        safety_text = '{"notes": ["Коротко: сегодня был спокойный день."]}'
    elif kind == LENS_RESEARCH:
        # One reply both calls can read: the query call takes `query`,
        # the lens-mode distill takes `cards` (each parser ignores the
        # other's key). The quote is a verbatim substring of the clip.
        safety_text = (
            '{"query": "Ashby requisite variety", "cards": [{"answers": true, '
            '"text": "Совет со страницы.", "quote": "Спать лучше в прохладной комнате.", '
            '"risk": "low"}]}'
        )
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
    if kind == REFLECT:
        # L5: pass 1's draft (one open thread), the selector's pick and
        # the grounding call's rewrite, one reply per call, in order.
        safety_provider = _ScriptedSafety(
            '{"add": [{"kind": "open_thread", "text": "%s"}], "close": [], "update": []}'
            % REFLECT_THREAD,
            '{"selected": [%d], "why": "Эшби о разнообразии."}' % garden_ids[2],
            '{"add": [{"ref": "a1", "text": "%s", "grounds": ["Эшби"]}], "update": []}'
            % REFLECT_GROUNDED,
        )

    if kind == LENS_GARDEN:
        # The garden builds its own provider (app/core/idle/lens_garden.py's
        # `build_garden_provider`, like critique's judge); patched so this
        # test never reaches the network.
        garden_fake = FakeLLMProvider(
            text=(
                '{"clusters": [], "gaps": [{"kind": "link", "note_ids": [%d, %d], '
                '"cluster_ids": [], "title": null, "detail": "Винер пишет о кибернетике."}]}'
                % (garden_ids[1], garden_ids[0])
            )
        )

        class _GardenClient:
            async def close(self):
                pass

        monkeypatch.setattr("app.llm.openrouter.build_client", lambda api_key: _GardenClient())
        monkeypatch.setattr(
            "app.core.idle.lens_garden.build_garden_provider", lambda settings, client: garden_fake
        )

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

    if kind in (RESEARCH, LENS_RESEARCH):
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
    if kind == REFLECT:
        settings = Settings(LENS_ENABLED=True, LENS_REFLECT_ENABLED=True)
    if kind == LENS_RESEARCH:
        settings = _lens_settings()
    if kind == LENS_GARDEN:
        settings = Settings(
            LENS_GARDEN_ENABLED=True, LENS_ENABLED=True, VAULT_KNOWLEDGE_ENABLED=True,
            VAULT_MODE="mirror",
        )

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
        elif kind == REFLECT:
            from sqlalchemy import select

            from app.db.models import LensRound, NotebookEntry

            run = await session.get(IdleRun, run_id)
            [round_] = (await session.execute(select(LensRound))).scalars().all()
            assert (round_.consumer, round_.outcome) == ("reflect", "grounded")
            assert run.summary.get("lens_round_id") == round_.id
            [entry] = (await session.execute(select(NotebookEntry))).scalars().all()
            assert entry.text == REFLECT_GROUNDED
            assert (entry.lens_round_id, entry.lens_note_ids) == (round_.id, [garden_ids[2]])
            assert safety_provider.calls == 3
        elif kind == CRITIQUE:
            run = await session.get(IdleRun, run_id)
            assert run.summary.get("count") == 1
        elif kind == CANARY:
            run = await session.get(IdleRun, run_id)
            assert run.summary.get("cases") == {"01": True}
        elif kind == RESEARCH:
            run = await session.get(IdleRun, run_id)
            assert run.summary.get("cards") == 1
        elif kind == LENS_GARDEN:
            from app.vault import lens

            run = await session.get(IdleRun, run_id)
            assert run.summary.get("new") == 1
            (gap,) = await lens.known_gaps(session)
            assert gap.kind == "link"
        elif kind == LENS_RESEARCH:
            from sqlalchemy import select

            from app.db.models import StudyCard, StudyJob

            run = await session.get(IdleRun, run_id)
            assert run.summary.get("cards") == 1
            assert (await session.get(StudyJob, lens_job_id)).status == "done"
            [card] = (await session.execute(select(StudyCard))).scalars().all()
            assert card.kind == "lens"
    assert calls == []
    assert fake.sent == []
    assert fake.edits == []
    assert fake.documents == []


REFLECT_THREAD = "Вернуться к вечерним чек-инам."
REFLECT_GROUNDED = "Вернуться к вечерним чек-инам: хватает ли в них разных вопросов."


class _ScriptedSafety(FakeLLMProvider):
    """FakeLLMProvider with one canned reply per call, in order (L5's
    REFLECT run makes three calls that each want a different shape)."""

    def __init__(self, *texts: str) -> None:
        super().__init__(text=texts[0], model="fake-safety")
        self._texts = list(texts)

    async def complete(self, messages, *, conversation_id, json_schema=None):
        self.text = self._texts[min(self.calls, len(self._texts) - 1)]
        return await super().complete(
            messages, conversation_id=conversation_id, json_schema=json_schema
        )


def _lens_settings() -> Settings:
    return Settings(
        RESEARCH_ENABLED=True, LENS_ENABLED=True, LENS_GARDEN_ENABLED=True, IDLE_ENABLED=True,
        RESEARCH_JOBS_PER_DAY=5, DAILY_USD_CAP=10.0,
    )


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

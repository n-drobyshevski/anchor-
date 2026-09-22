"""Only `/search` may reach for the web, and only when it is enabled (H3).

`/search` was never in a plan. It arrived with milestone 1f, it defaulted
*on*, and it is the one path in this bot that sends the user's words to a
third party (Exa, via OpenRouter's `web` plugin). Phase 4 may turn it into
a gated research loop; until then it is off, and the property that matters
is not "the current five call sites pass False" -- it is that **no future
call site can pass True by accident**.

So this file guards the property from two directions:

- **Structurally** (`_web_search_true_sites`): an AST walk over all of
  `app/` finds every call that passes a truthy `web_search=`, and asserts
  the only one is the `/search` handler. A new background job that copies
  an existing call site cannot introduce a second one without failing
  here, whether or not anyone thought to write a test for that job.
- **Behaviourally**: an ordinary chat turn, an outbound message and each
  background job are driven through a recording provider under **both**
  values of `LLM_WEB_SEARCH`, and every captured call must have
  `web_search=False`. The setting is deliberately parametrized: it gates
  only whether `/search` is *allowed*, and must never be mistaken for
  something that gates the rest of the bot.

The wire itself -- that `web_search=False` sends no `plugins`, no `tools`,
no `tool_choice` and no `functions` -- is already pinned in
tests/test_openrouter.py. This file covers everything above that seam.

Modelled on tests/test_core_clock_discipline.py, the repo's convention for
a rule that must outlive the people who read the plan.
"""

from __future__ import annotations

import ast
import datetime
import pathlib

import pytest
from aiogram import Bot

from app.config import Settings
from app.core import turn
from app.core.clock import FrozenClock, combine_local
from app.core.extract import run_extract
from app.core.outbound_gate import MORNING
from app.core.outbound_send import run_send_outbound
from app.core.scene import run_summarize_scene
from app.core.tick import run_tick_decide
from app.db.models import Message, Outbound, Scene, TelegramUpdate, UserState
from conftest import FakeLLMProvider, FakeSession

APP = pathlib.Path("app")

# The one sanctioned call site: app/tg/router.py's /search handler.
ALLOWED_SITE = ("app/tg/router.py", "search")

# Request-shaping keys that would hand the model a capability. They may be
# built in exactly one module -- the provider that owns the wire format.
TOOL_KEYS = ("plugins", "tools", "tool_choice", "functions")
TOOL_KEY_OWNER = pathlib.Path("app/llm/openrouter.py")


# --- structural: who passes web_search=True -----------------------------


def _enclosing_function(tree: ast.AST, node: ast.AST) -> str:
    """Name of the innermost def containing `node`, or "<module>"."""
    best = "<module>"
    best_start = -1
    for candidate in ast.walk(tree):
        if not isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(candidate, "end_lineno", candidate.lineno)
        if candidate.lineno <= node.lineno <= end and candidate.lineno > best_start:
            best, best_start = candidate.name, candidate.lineno
    return best


def _web_search_true_sites(path: pathlib.Path) -> list[tuple[str, str, int]]:
    """Every call in `path` that could originate a `web_search=True`.

    Returns (file, enclosing function, line). Two shapes are not origins
    and are skipped:

    - an explicit `web_search=False`, which is the thing we want;
    - `web_search=web_search`, a pass-through that forwards a parameter of
      the same name. It introduces no new source of truth -- the value
      still comes from an outer call site, which this same walk checks --
      and `turn.py` has three of them threading the /search flag down to
      the provider. `test_every_web_search_parameter_defaults_to_false`
      covers the other half of that: a pass-through is only as safe as the
      default it forwards.

    Anything else -- a different variable, an expression, a truthy literal
    -- is reported, because it cannot be proved False from here.
    """
    tree = ast.parse(path.read_text())
    found: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "web_search":
                continue
            value = keyword.value
            if isinstance(value, ast.Constant) and not value.value:
                continue
            if isinstance(value, ast.Name) and value.id == "web_search":
                continue
            found.append((str(path), _enclosing_function(tree, node), node.lineno))
    return found


def _web_search_defaults(path: pathlib.Path) -> list[tuple[str, str, int]]:
    """Every function in `path` whose `web_search` parameter is not False.

    A missing default counts as a violation too: a required parameter
    cannot be forgotten at a call site, but it also cannot be audited by
    the walk above, so the rule is simply "declare it False".
    """
    tree = ast.parse(path.read_text())
    found: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        pairs = list(zip(args.args[len(args.args) - len(args.defaults):], args.defaults))
        pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None]
        declared = {a.arg for a in args.args + args.kwonlyargs}
        defaulted = {a.arg for a, _ in pairs}
        if "web_search" in declared and "web_search" not in defaulted:
            found.append((str(path), node.name, node.lineno))
            continue
        for arg, default in pairs:
            if arg.arg != "web_search":
                continue
            if not (isinstance(default, ast.Constant) and default.value is False):
                found.append((str(path), node.name, node.lineno))
    return found


def _app_modules() -> list[pathlib.Path]:
    return sorted(APP.rglob("*.py"))


def test_there_are_app_modules_to_check():
    """Guards the guard: a glob matching nothing makes the rest vacuous."""
    modules = _app_modules()
    assert len(modules) > 15
    names = {str(p) for p in modules}
    assert "app/tg/router.py" in names
    assert "app/core/turn.py" in names
    assert "app/core/outbound_send.py" in names
    assert "app/worker.py" in names


def test_only_the_search_handler_ever_asks_for_web_search():
    sites = [site for path in _app_modules() for site in _web_search_true_sites(path)]
    assert [(f, fn) for f, fn, _ in sites] == [ALLOWED_SITE], (
        "web_search=True belongs to the /search handler and nowhere else "
        "(H3). Found:\n  " + "\n  ".join(f"{f}:{line} in {fn}()" for f, fn, line in sites)
    )


def test_every_web_search_parameter_defaults_to_false():
    """The other half of the pass-through rule. `web_search=web_search`
    is safe only because every function that declares the parameter
    defaults it to False -- flip one default and the whole bot searches
    without a single call site changing."""
    offenders = [site for path in _app_modules() for site in _web_search_defaults(path)]
    assert offenders == [], (
        "a web_search parameter must default to False (H3):\n  "
        + "\n  ".join(f"{f}:{line} in {fn}()" for f, fn, line in offenders)
    )


def test_the_default_detector_actually_detects(tmp_path):
    sample = tmp_path / "defaults.py"
    sample.write_text(
        "def good(a, *, web_search: bool = False): ...\n"
        "def bad_true(a, *, web_search: bool = True): ...\n"
        "def bad_required(a, *, web_search: bool): ...\n"
        "def unrelated(a, *, other=True): ...\n"
    )
    assert [fn for _, fn, _ in _web_search_defaults(sample)] == ["bad_true", "bad_required"]


def test_the_detector_actually_detects(tmp_path):
    """A guard that cannot fail is not a guard."""
    sample = tmp_path / "offender.py"
    sample.write_text(
        "async def innocent():\n"
        "    await p.complete(m, conversation_id='a')\n"
        "async def explicit_false():\n"
        "    await p.complete(m, conversation_id='a', web_search=False)\n"
        "async def offender():\n"
        "    await p.complete(m, conversation_id='a', web_search=True)\n"
        "async def sneaky(flag):\n"
        "    await p.complete(m, conversation_id='a', web_search=flag)\n"
        "async def passthrough(web_search=False):\n"
        "    await p.complete(m, conversation_id='a', web_search=web_search)\n"
    )
    found = _web_search_true_sites(sample)
    functions = [fn for _, fn, _ in found]
    assert functions == ["offender", "sneaky"], found


def test_no_module_outside_the_provider_builds_a_tool_request():
    """`plugins`/`tools` are wire-format concerns. If they appear in
    app/core/ or app/tg/, some caller is shaping a request behind the
    provider's back."""
    offenders: list[str] = []
    for path in _app_modules():
        if path == TOOL_KEY_OWNER:
            continue
        source = path.read_text()
        for key in TOOL_KEYS:
            if f'"{key}"' in source or f"'{key}'" in source:
                offenders.append(f"{path}: {key}")
    assert offenders == [], (
        f"only {TOOL_KEY_OWNER} may build a tool/plugin request (H3):\n  "
        + "\n  ".join(offenders)
    )


# --- behavioural: the real entry points, under both settings ------------

TIMEZONE = "Europe/Paris"
CHAT_ID = 4242
DAY = datetime.date(2026, 9, 23)

BOTH_VALUES = pytest.mark.parametrize("web_search_enabled", [True, False])


def _settings(**overrides) -> Settings:
    base = dict(TZ_DEFAULT=TIMEZONE, JITTER_MAX_MIN=0, DAILY_USD_CAP=1.00)
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _at(hour: int, minute: int = 0) -> FrozenClock:
    return FrozenClock(combine_local(DAY, datetime.time(hour, minute), TIMEZONE))


def _bot() -> tuple[Bot, FakeSession]:
    fake = FakeSession()
    return Bot(token="123456:TESTTOKEN", session=fake), fake


def _assert_never_searched(*providers: FakeLLMProvider) -> None:
    """Every model call any of these providers saw ran without the web."""
    captured = [flag for p in providers for flag in p.received_web_search]
    assert captured, "no model call was made -- the test proved nothing"
    assert captured == [False] * len(captured), captured


async def _seed_state(sessionmaker, *, update_id: int | None = None, **fields) -> None:
    """The rows every entry point assumes exist: the single user's state,
    and -- for a chat turn -- the inbound update it is answering."""
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE, **fields))
        await session.commit()
        if update_id is not None:
            session.add(TelegramUpdate(update_id=update_id, payload={}))
            await session.commit()


@BOTH_VALUES
async def test_an_ordinary_chat_turn_never_searches(sessionmaker, clock, web_search_enabled):
    await _seed_state(sessionmaker, update_id=9001, intensity=3)
    main = FakeLLMProvider(text="Принято. Дальше.")
    cheap = FakeLLMProvider(text='{"level": "none", "confidence": 0.1}')
    bot, _ = _bot()

    await turn.run(
        sessionmaker,
        bot,
        _settings(LLM_WEB_SEARCH=web_search_enabled),
        main,
        clock=clock,
        chat_id=CHAT_ID,
        update_id=9001,
        user_text="привет",
        cheap_provider=cheap,
    )

    _assert_never_searched(main, cheap)


@BOTH_VALUES
async def test_an_outbound_message_never_searches(sessionmaker, web_search_enabled):
    await _seed_state(sessionmaker, intensity=3)
    async with sessionmaker() as session:
        row = Outbound(
            kind=MORNING,
            local_date=DAY,
            bucket=0,
            planned_for=combine_local(DAY, datetime.time(9, 0), TIMEZONE),
            status="planned",
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        outbound_id = row.id

    provider = FakeLLMProvider(text="Доброе утро. Что сегодня главное?")
    bot, _ = _bot()
    async with sessionmaker() as session:
        await run_send_outbound(
            session,
            _settings(LLM_WEB_SEARCH=web_search_enabled),
            provider,
            bot,
            clock=_at(9, 0),
            outbound_id=outbound_id,
        )

    _assert_never_searched(provider)


@BOTH_VALUES
async def test_the_extract_job_never_searches(sessionmaker, clock, web_search_enabled):
    await _seed_state(sessionmaker, update_id=9002, intensity=3)
    bot, _ = _bot()
    await turn.run(
        sessionmaker,
        bot,
        _settings(LLM_WEB_SEARCH=web_search_enabled),
        FakeLLMProvider(text="Принято."),
        clock=clock,
        chat_id=CHAT_ID,
        update_id=9002,
        user_text="я сегодня думал про Лилль",
    )

    provider = FakeLLMProvider(text='{"memories": [], "state": {}}')
    async with sessionmaker() as session:
        await run_extract(
            session,
            _settings(LLM_WEB_SEARCH=web_search_enabled),
            provider,
            update_id=9002,
            memory_ids=[],
            clock=clock,
            timezone=TIMEZONE,
            intensity=3,
            focus_on=True,
            due_action=None,
        )

    _assert_never_searched(provider)


@BOTH_VALUES
async def test_the_scene_summary_job_never_searches(sessionmaker, clock, web_search_enabled):
    async with sessionmaker() as session:
        scene = Scene(started_at=clock.now_utc())
        session.add(scene)
        await session.flush()
        for i in range(6):
            session.add(
                Message(
                    scene_id=scene.id,
                    role="user" if i % 2 == 0 else "assistant",
                    content=f"строка {i}",
                    kind="chat",
                    created_at=clock.now_utc(),
                )
            )
        await session.commit()
        scene_id = scene.id

    provider = FakeLLMProvider(text="Говорили об отчёте.")
    async with sessionmaker() as session:
        await run_summarize_scene(
            session,
            _settings(LLM_WEB_SEARCH=web_search_enabled),
            provider,
            scene_id=scene_id,
            clock=clock,
            timezone=TIMEZONE,
        )

    _assert_never_searched(provider)


@BOTH_VALUES
async def test_the_tick_decision_job_never_searches(sessionmaker, web_search_enabled):
    await _seed_state(
        sessionmaker,
        intensity=3,
        focus_on=True,
        last_user_msg_at=combine_local(DAY, datetime.time(6, 0), TIMEZONE),
    )

    provider = FakeLLMProvider(text='{"send": false, "note": ""}')
    async with sessionmaker() as session:
        await run_tick_decide(
            session,
            _settings(LLM_WEB_SEARCH=web_search_enabled),
            provider,
            clock=_at(10, 0),
            local_date=DAY,
            hour=10,
        )

    _assert_never_searched(provider)


# --- /search itself ------------------------------------------------------


async def test_search_reaches_the_web_only_when_enabled(sessionmaker, clock):
    """The positive half: the flag does arrive at the provider. Without
    this, every assertion above could pass on a bot whose search is
    simply broken."""
    await _seed_state(sessionmaker, update_id=9003, intensity=3)
    provider = FakeLLMProvider(text="Вот что нашлось.")
    bot, _ = _bot()

    await turn.run(
        sessionmaker,
        bot,
        _settings(LLM_WEB_SEARCH=True),
        provider,
        clock=clock,
        chat_id=CHAT_ID,
        update_id=9003,
        user_text="когда следующий поезд",
        web_search=True,
    )

    assert provider.received_web_search == [True]


def test_the_disabled_reply_is_the_wording_the_prompt_specifies():
    """A user-facing string, pinned so a reword is a deliberate act."""
    assert turn.SEARCH_DISABLED_REPLY_TEXT == "Поиск пока выключен."


def test_web_search_is_off_by_default():
    """The whole point of H3. `/search` stays in the tree for phase 4,
    but a fresh deployment must not have it live."""
    assert Settings(_env_file=None).LLM_WEB_SEARCH is False

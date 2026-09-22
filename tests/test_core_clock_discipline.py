"""Nothing in app/core/ reads the wall clock for itself (phase-3 plan section 3).

The plan's rule is absolute: "Don't call `datetime.now()` directly
anywhere in `core/`." A rule that lives only in a docstring decays --
the next person to need a timestamp writes the obvious thing, it works,
the tests pass, and six months later a scheduler test that should be
deterministic is quietly reading the real clock and failing once a year
on the last Sunday in October.

So the rule is a test. It walks the AST of every module in app/core/
and fails on any call to datetime.now, datetime.utcnow, date.today,
time.time, or SQL's func.now() -- naming the file and line, so the fix
is obvious.

**The two deliberate exceptions are outside app/core/ and stay there:**

- app/db/queue.py compares `run_after <= func.now()` in SQL. Job
  due-ness must use the database's clock, or a frozen test clock could
  hide a real scheduling bug.
- `server_default=func.now()` on created_at columns in app/db/models.py.
  Audit stamps, never read by logic.

Modelled on the existing invariant test that pins "the extractor cannot
write sensitive state" -- the repo's convention for rules that must
survive the people who did not read the plan.
"""

from __future__ import annotations

import ast
import pathlib

CORE = pathlib.Path("app/core")

# clock.py is the one module allowed to read the clock: it *is* the
# abstraction. Everything else asks it.
EXEMPT = {"clock.py"}

# A call whose final attribute is one of these is a wall-clock read,
# however it was imported: datetime.datetime.now(), datetime.now(),
# dt.now(), and SQL's func.now() all end the same way.
FORBIDDEN_ATTRS = {"now", "utcnow", "today"}
# Dotted names that are forbidden outright.
#
# `time.monotonic()` is deliberately NOT here. It cannot tell you what
# day it is -- it only measures elapsed time, which is what
# app/core/turn.py uses it for (the latency_ms in its log line). Routing
# that through the Clock would make a FrozenClock report every call as
# taking 0ms, which is worse than useless: it would hide a latency
# regression behind a test that passes.
FORBIDDEN_DOTTED = {"time.time"}


def _dotted(node: ast.AST) -> str:
    """Render a Name/Attribute chain as `a.b.c`, or "" if it is neither."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    else:
        return ""
    return ".".join(reversed(parts))


def _violations(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text())
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted(node.func)
        if not dotted:
            continue
        last = dotted.rsplit(".", 1)[-1]
        if last in FORBIDDEN_ATTRS or dotted in FORBIDDEN_DOTTED:
            found.append(f"{path}:{node.lineno}: {dotted}()")
    return found


def _core_modules() -> list[pathlib.Path]:
    return sorted(p for p in CORE.glob("*.py") if p.name not in EXEMPT)


def test_there_are_core_modules_to_check():
    """Guards the guard: a glob that silently matched nothing would make
    every assertion below vacuously true."""
    modules = _core_modules()
    assert len(modules) > 5
    assert any(p.name == "turn.py" for p in modules)
    assert any(p.name == "outbound_gate.py" for p in modules)


def test_no_module_in_core_reads_the_wall_clock():
    offenders: list[str] = []
    for path in _core_modules():
        offenders.extend(_violations(path))
    assert offenders == [], (
        "app/core/ must take the time from the injected Clock "
        "(phase-3 plan section 3), not read it directly:\n  "
        + "\n  ".join(offenders)
    )


def test_the_detector_actually_detects(tmp_path):
    """A guard that cannot fail is not a guard."""
    sample = tmp_path / "offender.py"
    sample.write_text(
        "import datetime\n"
        "import time\n"
        "from sqlalchemy import func\n"
        "def f():\n"
        "    a = datetime.datetime.now(datetime.timezone.utc)\n"
        "    b = datetime.date.today()\n"
        "    c = func.now()\n"
        "    d = time.time()\n"
        "    e = time.monotonic()  # allowed: measures elapsed, not wall\n"
        "    return a, b, c, d, e\n"
    )
    found = _violations(sample)
    assert len(found) == 4
    assert not any("monotonic" in line for line in found)
    assert any("datetime.datetime.now" in line for line in found)
    assert any("datetime.date.today" in line for line in found)
    assert any("func.now" in line for line in found)


def test_the_detector_does_not_flag_the_clock_protocol():
    """`clock.now_utc()` is the sanctioned call and must not trip it."""
    sample_src = "def f(clock):\n    return clock.now_utc()\n"
    tree = ast.parse(sample_src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert calls and _dotted(calls[0].func).rsplit(".", 1)[-1] == "now_utc"
    assert "now_utc" not in FORBIDDEN_ATTRS


def test_the_documented_exceptions_still_live_outside_core():
    """If someone moves the queue's SQL now() into core/, this test is
    the one that should start failing -- not a DST bug in production."""
    queue_src = pathlib.Path("app/db/queue.py").read_text()
    assert "func.now()" in queue_src
    assert not (CORE / "queue.py").exists()

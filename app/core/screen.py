"""One shared screen for anything about to be stored as Anchor's own words
(implementation plan's "Design decisions": "One shared screen").

`app/research/distill.py` runs injection, redaction and risk checks by
hand, in sequence, for a card pulled off a fetched page. Milestone 5b's
notebook needs exactly the same sequence for a different kind of text --
the safety model's own reflection, and the user's own `/mind add` --
and 5c/5d's standing orders and weekly review reuse it again. Rather
than each caller re-deriving distill.py's order (and one of them
eventually getting it wrong), `screen()` is the order, once, callable
from anywhere in `app/core`.

**Import direction.** `app/core/scheduler.py` already imports from
`app.research` (`RESEARCH_SWEEP`), so a second `app.core` module
importing `app.research.{injection,risk}` adds no new direction, only a
second use of one that already exists. `app/research/distill.py` itself
is not imported from here or by it: the two modules solve the same
problem for different inputs and neither depends on the other.

**Order, and why it is this order** (mirrors distill.py's own):

1. `injection.hits` -- a stored note that carries an instruction is a
   prompt injection with a delay fuse the moment it reaches a persona
   prompt, so this runs before anything else gets a chance to say the
   text is otherwise fine.
2. `redact.is_safe_to_store` -- the same door every other write path
   in this codebase uses for secrets (card numbers, IBANs, emails).
3. `risk.assess` -- rules only ever raise. `high` refuses outright;
   the `intensity` rule id is reported on its own, distinct from a
   general `medium`, because 5b's `/mind add` treats it differently
   from every other risk hit (the implementation plan's design
   decision: a user's own "быть строже к себе" is their call, not
   Anchor's to refuse, so only the id -- not the whole medium level --
   is surfaced for a caller to choose what to do with it).

A caller that wants "refuse anything but a clean id-agnostic pass"
checks `result.ok`. A caller that wants 5b's carve-out for the user's
own intentions checks `result.reason` for exactly `"risk_intensity"`.
"""

from __future__ import annotations

import dataclasses

from app.core import redact
from app.research import injection, risk

INJECTION = "injection"
UNSAFE_TO_STORE = "unsafe_to_store"
RISK_HIGH = "risk_high"
RISK_INTENSITY = "risk_intensity"

REASONS = (INJECTION, UNSAFE_TO_STORE, RISK_HIGH, RISK_INTENSITY)


@dataclasses.dataclass(frozen=True)
class ScreenResult:
    """`ok` is True iff `text` cleared every check. `reason` is one of
    REASONS when it did not, and None when it did."""

    ok: bool
    reason: str | None = None


def screen(text: str) -> ScreenResult:
    """Run `text` through injection, redaction and the risk rules, in order.

    Returns the *first* reason `text` fails on -- callers that need to
    know about a later check too (there are none yet) would call the
    underlying modules directly, but nothing in 5b-5d needs more than
    "did it pass, and if not, why".
    """
    if injection.hits(text):
        return ScreenResult(ok=False, reason=INJECTION)
    if not redact.is_safe_to_store(text):
        return ScreenResult(ok=False, reason=UNSAFE_TO_STORE)
    level, rule_ids = risk.assess(text)
    if level == risk.HIGH:
        return ScreenResult(ok=False, reason=RISK_HIGH)
    if "intensity" in rule_ids:
        return ScreenResult(ok=False, reason=RISK_INTENSITY)
    return ScreenResult(ok=True)

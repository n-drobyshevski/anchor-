"""May a report go out right now? (phase-4 plan section 9, phase-5 plan section 8).

A report is a short system line the user is owed because of something
they started: a /read job finishing, and from phase 5 on a vault
notice or a vault hold asking for a decision. It is not an unsolicited
message, so it is not the outbound gate's business -- but it still
respects the three states that mean "not now" in the user's own voice.

This lived in app/worker.py as `_may_report_now` until 5a. The vault
needs it too, and app/vault/ must not import app.worker (phase-5 plan
section 13), so it moved here, unchanged.
"""

from __future__ import annotations

from app.config import Settings
from app.core.clock import Clock, to_local, within_window


def may_report_now(settings: Settings, clock: Clock, user_state) -> bool:
    """May a report line be sent right now (phase-4 plan section 9)?

    Three checks, and deliberately not the outbound gate: a research
    job's "done" line is a reply to a command the user typed, not an
    unsolicited message. It does not touch the outbound counters, is
    not counted by the gate, and is not subject to OUTBOUND_ENABLED or
    the daily cap -- plan section 9 says so in as many words.

    What it does respect is the three states that mean "not now" in the
    user's own voice: a pause (/out, a pause word, a welfare trigger),
    an explicit /quiet, and quiet hours. Same three the gate checks
    second, third and fourth, for the same reasons, read the same way.

    When the answer is no, nothing is sent and nothing is queued for
    later: the cards are already in /notes, which is where the line
    would have pointed.
    """
    if not user_state.persona_active:
        return False
    now = clock.now_utc()
    if user_state.quiet_until is not None and user_state.quiet_until > now:
        return False
    local_now = to_local(now, user_state.timezone)
    return not within_window(local_now.time(), settings.QUIET_START, settings.QUIET_END)

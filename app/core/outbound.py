"""The §7 cancel-outbound hook.

There is no outbound queue in Phase 1 -- the bot only ever replies to
a message the user just sent, nothing is scheduled or proactive yet.
This is therefore a deliberate no-op, given its own file so Phase 3's
proactive-tick/outbound-message machinery (plan section 1, out of
scope here) has an obvious, already-wired place to land its real
cancellation logic.
"""

from __future__ import annotations


async def cancel_outbound() -> None:
    """Cancel any in-flight or scheduled outbound message.

    # TODO(phase-3): wire this to the outbound queue once one exists.
    """
    return None

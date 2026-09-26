"""/revoke for both outside assistants (connector plan section 6.1).

One command closes everything readable from outside: Grok's grants and
Claude's windows. It works with either feature switched off, so turning
a flag off never strands a live grant. It does not end the Claude
*connection* (that is `/claude disconnect`): with no window, a
connected Claude reads nothing anyway.

When only Grok grants were open, the reply is Grok's own text, which
also tells the user the capability link is dead; otherwise it names
each client's count.
"""

from __future__ import annotations

import logging

from app.core import grants
from app.core.clock import Clock
from app.web import oauth_store

logger = logging.getLogger(__name__)

GROK_REVOKED_TEXT = (
    "Доступ для Grok закрыт ({count}). Коннектор в grok.com можно удалить: "
    "ссылка больше не работает."
)
BOTH_REVOKED_TEXT = "Доступ закрыт: {parts}."
NOTHING_TO_REVOKE = "Открытых доступов нет."
CLIENT_NAMES = ((grants.GROK, "Grok"), (grants.CLAUDE, "Claude"))


def revoked_text(counts: dict[str, int]) -> str:
    if not any(counts.values()):
        return NOTHING_TO_REVOKE
    if not counts.get(grants.CLAUDE):
        return GROK_REVOKED_TEXT.format(count=counts[grants.GROK])
    parts = ", ".join(f"{name} ({counts.get(client, 0)})" for client, name in CLIENT_NAMES)
    return BOTH_REVOKED_TEXT.format(parts=parts)


async def revoke(sessionmaker, clock: Clock) -> str:
    async with sessionmaker() as session:
        counts = await grants.revoke_all_by_client(session, clock)
        # C3: /revoke also closes the library's standing switch -- the
        # connection itself survives (that is /claude disconnect's job),
        # but "closed" must mean closed, not "closed except the one
        # door that was never a window in the first place".
        await oauth_store.disable_library_if_connected(session, clock)
    logger.info("grants revoked", extra={"event": "revoke", "count": sum(counts.values())})
    return revoked_text(counts)

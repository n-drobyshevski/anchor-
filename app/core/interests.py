"""Interest topics for idle research (Phase 6 plan sections 3, 6.5 and 7;
milestone 6d).

Mirrors `app/core/orders.py`'s shape for `/order`/`/orders`: `/interests
add` writes a row directly, no proposal card (the user names the topic
outright, exactly like a standing order), screened with `app.core.
screen` the way `/mind add` and `/order` both are, and `/interests`'s
own [✖] retires one -- `active=false`, mirroring `/orders`' [Снять] --
rather than deleting it, so `app/core/idle/research.py`'s `last_run_at`
history and any cards already produced from it stay intact.

**Every `screen()` failure refuses, including `risk_intensity`.**
`/order` and `/mind add` both carve `risk_intensity` out (the implementation
plan's decision: a user's own "быть строже к себе" is their call). A
research topic is not that kind of self-directed intention -- it drives
an unattended web-search pipeline over pages nobody has read yet, on the
user's behalf but without them in the loop, so plan section 7's "a
`high` hit gets «Такое не ищу.»" is read here as covering the whole
`screen()` verdict, not `risk_high` alone: an injection hit or unsafe-
to-store text is exactly as wrong a thing to hand the research pipeline
as a high-risk one would be, and get the identical reply.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.screen import screen
from app.db.models import InterestTopic
from app.research.jobs import PACKETS

logger = logging.getLogger(__name__)

# ck_interest_topic_text_length, mirrored here so a caller can refuse
# before ever reaching the database (same posture as
# app/research/jobs.QUERY_MAX for /study's own topic).
TEXT_MAX = 100

# Plan section 7: "caps active topics at 10" -- a fixed number, not a
# Settings knob (unlike ORDERS_MAX_ACTIVE, which the plan's config
# section does list). See the milestone report for why.
MAX_ACTIVE_TOPICS = 10

REFUSAL_TEXT = "Такое не ищу."
CAP_TEXT = "Сначала убери одну из тем."

CreateResult = str  # "ok" | "refused" | "unknown_packet" | "too_long" | "empty" | "cap"


def _clean_text(raw: str) -> str | None:
    cleaned = raw.strip()
    return cleaned or None


async def _count_active(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count()).select_from(InterestTopic).where(InterestTopic.active.is_(True))
    )
    return result.scalar_one()


async def active_topics(session: AsyncSession) -> list[InterestTopic]:
    """Active topics, oldest first (insertion order) -- `/interests`'
    own listing order; id order matches the order they were added in,
    same convention `app/core/orders.py`'s `active_orders` uses."""
    result = await session.execute(
        select(InterestTopic).where(InterestTopic.active.is_(True)).order_by(InterestTopic.id)
    )
    return list(result.scalars().all())


async def add_topic(
    session: AsyncSession, settings: Settings, *, packet: str, text: str
) -> CreateResult:
    """`/interests add <forums|guides|ref> <тема>`. Checks run in this
    order: packet name, text presence, text length, the risk screen,
    then the cap -- packet and text shape first (nothing to write yet),
    the cap last (the same "refuse the expensive check last" ordering
    `app/core/orders.create_active` already uses for its own cap)."""
    packet = packet.strip().lower()
    if packet not in PACKETS:
        return "unknown_packet"

    cleaned = _clean_text(text)
    if cleaned is None:
        return "empty"
    if len(cleaned) > TEXT_MAX:
        return "too_long"

    if not screen(cleaned).ok:
        return "refused"

    if await _count_active(session) >= MAX_ACTIVE_TOPICS:
        return "cap"

    session.add(InterestTopic(text=cleaned, packet=packet, active=True))
    await session.commit()
    logger.info("interest topic added", extra={"event": "interest_add", "packet": packet})
    return "ok"


DecisionResult = str  # "ok" | "stale"


async def retire(session: AsyncSession, topic_id: int) -> DecisionResult:
    """`/interests`'s own [✖]: retire an active topic. Idempotent-safe
    from the caller's point of view -- a topic already retired, or an id
    that never existed, both come back `"stale"`, the identical reply
    `app/core/orders.retire` gives for the same two cases."""
    topic = await session.get(InterestTopic, topic_id)
    if topic is None or not topic.active:
        return "stale"

    topic.active = False
    await session.commit()
    logger.info("interest topic retired", extra={"topic_id": topic_id})
    return "ok"


__all__ = [
    "CAP_TEXT",
    "MAX_ACTIVE_TOPICS",
    "REFUSAL_TEXT",
    "TEXT_MAX",
    "active_topics",
    "add_topic",
    "retire",
]

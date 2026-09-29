"""Deciding a weekly-review proposal: the steps Telegram's buttons and
the web's Дневник page both take, in one place so the two cannot drift.

- `decide_order(accept=...)`: a standing order the review (or the
  extractor) proposed is accepted or declined through app/core/orders.py,
  and -- when the review proposed it -- its `review_proposal` row is
  marked adopted or rejected. Only on an actual decision: never on
  `cap` (the order keeps its status, still answerable) and never for a
  user-authored or counter row (`review_proposal_id` is None there).
- `adopt_amendment`: a `persona_note` proposal is adopted as a trial
  amendment through app/core/amendments.py, and the `amendment_trial`
  job that decides whether it becomes active is queued.

This module sits outside the autonomy modules on purpose
(tests/test_autonomy_isolation.py): it combines their writers, and
queues a job, which none of them may do on their own. Moved out of
app/tg/orders.py's and app/tg/review.py's callbacks unchanged.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import amendments as amendments_module
from app.core import orders as orders_module
from app.core import review as review_module
from app.core.clock import Clock
from app.db.jobs import enqueue_job
from app.db.models import StandingOrder


async def decide_order(
    session: AsyncSession, settings: Settings, order_id: int, *, accept: bool, clock: Clock
) -> tuple[str, StandingOrder | None]:
    """Accept or decline a proposed (or countered) order. Returns
    `orders.accept`/`decline`'s result ("ok", "cap" or "stale") and the
    order row as it stands afterwards (None if it does not exist)."""
    if accept:
        result = await orders_module.accept(session, settings, order_id, clock=clock)
    else:
        result = await orders_module.decline(session, order_id, clock=clock)
    order = await session.get(StandingOrder, order_id)
    if result == "ok" and order is not None and order.review_proposal_id is not None:
        await review_module.mark_proposal(
            session,
            order.review_proposal_id,
            review_module.ADOPTED if accept else review_module.REJECTED,
            clock=clock,
        )
    return result, order


async def adopt_amendment(
    session: AsyncSession, settings: Settings, review_proposal_id: int, *, clock: Clock
) -> amendments_module.AdoptResult:
    """Adopt a `persona_note` proposal as a trial amendment and queue its
    trial. `status` is "ok", "cap" or "stale", as `amendments.adopt`."""
    result = await amendments_module.adopt(session, settings, review_proposal_id, clock=clock)
    if result.status == "ok" and result.amendment is not None:
        await enqueue_job(
            session,
            amendments_module.AMENDMENT_TRIAL,
            {"amendment_id": result.amendment.id},
            dedup_key=f"am:{result.amendment.id}",
        )
    return result

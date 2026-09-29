"""The weekly review and persona amendments on the web's Дневник page.

- GET /api/review: the latest weekly review (the analysis the persona
  message was written from: wins, misses, patterns, intentions) and its
  proposals with their status.
- POST /api/review/run: an on-demand review, like Telegram's `/review`.
  It enqueues a synthetic `/review` web update through the ingress --
  the same path POST /api/state/pause takes for /out -- so the whole
  run (the spend cap, the analysis, the persona message, the proposal
  cards) stays the one implementation app/tg/review.py already owns,
  with no second LLM path here. The message arrives in the web chat.
- POST /api/review/proposals/{id}/accept|reject: decide a pending
  proposal through app/core/review_actions.py, the same helpers
  Telegram's `so:*`/`am:*` buttons use: a standing order is accepted or
  declined (and the proposal marked with it); a persona note is adopted
  as a trial amendment (queuing its trial) or rejected. A Telegram card
  for the same proposal keeps its buttons; a later press there finds
  the proposal decided and answers «Устарело».
- GET /api/amendments and POST /api/amendments/{id}/revoke: the active
  persona amendments, flagged when persona.md changed since, as
  `/amendments` lists them.

Every write: session, the shared panel-write bucket,
`checkin_core.clear_awaiting`, the core call, `invalidate("review")`
(plus "orders" and "checkin" when an order was accepted: the Договорённости
tab and the check-in form list active orders). Silent in Telegram, with
one exception the Telegram path shares: an adopted amendment's trial
job reports its verdict when it finishes. Proposal and amendment text
never reaches a log line.
"""

from __future__ import annotations

import logging
import uuid

from aiohttp import web
from sqlalchemy import select

from app.core import amendments as amendments_core
from app.core import checkin as checkin_core
from app.core import orders as orders_core
from app.core import review as review_core
from app.core import review_actions
from app.core.prompt import persona_path_for
from app.db.models import ReviewProposal, StandingOrder, WeeklyReview
from app.web import ingress
from app.web.hub import WebHub
from app.web.http import _cookie_settings, _json, _rate_limited, _session_token_valid
from app.web.panels._common import path_id
from app.web.ratelimit import MAX_PENDING_WEB_ROWS, WebRateLimiter, pending_web_count

logger = logging.getLogger(__name__)


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


async def _proposal_dto(session, row: ReviewProposal) -> dict:
    order = None
    if row.kind == review_core.STANDING_ORDER:
        result = await session.execute(
            select(StandingOrder).where(StandingOrder.review_proposal_id == row.id)
        )
        linked = result.scalars().first()
        if linked is not None:
            order = {
                "id": linked.id,
                "status": linked.status,
                "cadence_label": orders_core.cadence_label(linked.cadence, linked.weekday),
            }
    return {
        "id": row.id,
        "kind": row.kind,
        "text": row.text,
        "reason": row.reason,
        "status": row.status,
        "order": order,
    }


async def get_review(request: web.Request) -> web.Response:
    """The latest review, or `{"review": null}` before the first one."""
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    _settings, sessionmaker, _clock, _bot = _cookie_settings(request)
    async with sessionmaker() as session:
        result = await session.execute(
            select(WeeklyReview).order_by(WeeklyReview.week_start.desc(), WeeklyReview.id.desc()).limit(1)
        )
        row = result.scalars().first()
        if row is None:
            return _json(200, {"review": None})
        proposals = (
            await session.execute(
                select(ReviewProposal).where(ReviewProposal.review_id == row.id).order_by(ReviewProposal.id)
            )
        ).scalars().all()
        analysis = row.analysis or {}
        dto = {
            "id": row.id,
            "week_start": row.week_start.isoformat(),
            "created_at": _iso(row.created_at),
            "wins": list(analysis.get("wins") or []),
            "misses": list(analysis.get("misses") or []),
            "patterns": list(analysis.get("patterns") or []),
            "intentions": list(analysis.get("intentions") or []),
            "proposals": [await _proposal_dto(session, p) for p in proposals],
        }
    return _json(200, {"review": dto})


async def post_run(request: web.Request) -> web.Response:
    """202 once `/review` is queued; the reply lands in the web chat."""
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, sessionmaker, _clock, _bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]

    retry = limiter.check_panel_write()
    if retry is not None:
        return _rate_limited(retry)
    retry = limiter.check_send()
    if retry is not None:
        return _rate_limited(retry)

    async with sessionmaker() as session:
        if await pending_web_count(session) >= MAX_PENDING_WEB_ROWS:
            return _rate_limited(5.0)
        await ingress.send_text(session, settings=settings, text="/review", client_key=str(uuid.uuid4()))
    logger.info("review queued from the web", extra={"event": "web_review_run"})
    return _json(202, {})


async def _decide(request: web.Request, accept: bool) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, sessionmaker, clock, _bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]
    hub: WebHub = request.app["web_hub"]

    proposal_id = path_id(request)
    if proposal_id is None:
        return _json(404, {"error": "not_found"})

    retry = limiter.check_panel_write()
    if retry is not None:
        return _rate_limited(retry)

    order_accepted = False
    async with sessionmaker() as session:
        proposal = await session.get(ReviewProposal, proposal_id)
        if proposal is None:
            return _json(404, {"error": "not_found"})
        if proposal.status != review_core.PENDING:
            return _json(409, {"error": "not_pending", "status": proposal.status})
        await checkin_core.clear_awaiting(session)

        if proposal.kind == review_core.PERSONA_NOTE:
            if accept:
                outcome = await review_actions.adopt_amendment(session, settings, proposal_id, clock=clock)
                status = outcome.status
                cap_message = amendments_core.CAP_TEXT
            else:
                status = "ok" if await amendments_core.reject(session, proposal_id, clock=clock) else "stale"
        else:
            result = await session.execute(
                select(StandingOrder.id).where(StandingOrder.review_proposal_id == proposal_id)
            )
            order_id = result.scalars().first()
            if order_id is None:
                return _json(404, {"error": "not_found"})
            status, _order = await review_actions.decide_order(
                session, settings, order_id, accept=accept, clock=clock
            )
            cap_message = orders_core.CAP_TEXT
            order_accepted = accept and status == "ok"

        await session.refresh(proposal)
        final_status = proposal.status

    if status == "cap":
        return _json(409, {"error": "cap", "message": cap_message})
    if status != "ok":
        return _json(409, {"error": "not_pending", "status": final_status})
    logger.info(
        "review proposal decided from the web",
        extra={"event": "web_review_accept" if accept else "web_review_reject"},
    )
    hub.publish_invalidate("review")
    if order_accepted:
        hub.publish_invalidate("orders")
        hub.publish_invalidate("checkin")
    return _json(200, {"proposal": {"id": proposal_id, "status": final_status}})


async def post_accept(request: web.Request) -> web.Response:
    return await _decide(request, accept=True)


async def post_reject(request: web.Request) -> web.Response:
    return await _decide(request, accept=False)


async def get_amendments(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, sessionmaker, _clock, _bot = _cookie_settings(request)
    async with sessionmaker() as session:
        rows = await amendments_core.list_for_display(session, persona_path_for(settings))
    return _json(
        200,
        {
            "items": [
                {
                    "id": row.amendment.id,
                    "text": row.amendment.text,
                    "activated_at": _iso(row.amendment.activated_at),
                    "stale": row.stale,
                }
                for row in rows
            ]
        },
    )


async def post_revoke(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    _settings, sessionmaker, clock, _bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]
    hub: WebHub = request.app["web_hub"]

    amendment_id = path_id(request)
    if amendment_id is None:
        return _json(404, {"error": "not_found"})

    retry = limiter.check_panel_write()
    if retry is not None:
        return _rate_limited(retry)

    async with sessionmaker() as session:
        await checkin_core.clear_awaiting(session)
        revoked = await amendments_core.revoke(session, amendment_id, clock=clock)
    if not revoked:
        return _json(404, {"error": "not_found"})
    logger.info("amendment revoked from the web", extra={"event": "web_amendment_revoke"})
    hub.publish_invalidate("review")
    return _json(200, {})


def register(app: web.Application) -> None:
    app.router.add_get("/api/review", get_review)
    app.router.add_post("/api/review/run", post_run)
    app.router.add_post("/api/review/proposals/{id}/accept", post_accept)
    app.router.add_post("/api/review/proposals/{id}/reject", post_reject)
    app.router.add_get("/api/amendments", get_amendments)
    app.router.add_post("/api/amendments/{id}/revoke", post_revoke)

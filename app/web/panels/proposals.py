"""GET /api/proposals and POST /api/proposals/{id}/accept|reject (W2
roadmap section 4).

This is the current gap the roadmap names directly: accept/reject have
only ever been Telegram inline buttons. The web panel now drives the
exact same `app/core/proposal.py` functions Telegram's callback handler
does (`accept`/`reject`, still writing `source="button"` -- that is
deliberate, not an oversight: it is the field this codebase already
uses to mean "a decision, not a raw suggestion", and a proposal decided
from the web is still exactly that kind of thing, whichever transport
the tap came from), then:

- retires the proposal's Telegram buttons through the real bot
  (`app["bot"]`), showing the same "✅ Принято"/"✖️ Отклонено" outcome
  a Telegram tap would have shown (`app/tg/proposals.py`'s
  `show_decision_outcome`, factored out of `handle_decision_callback`
  for exactly this reuse) -- never a chat message, per the "silent in
  Telegram" decision (roadmap section 7); best-effort and after the DB
  session has closed, same as `app/web/panels/state.py`'s equivalent
  call, since accept()/reject() has already committed by then and a
  Telegram-side failure (a deleted message, a network error) must not
  turn an already-successful decision into a 500;
- publishes `invalidate("proposals")`, and additionally
  `invalidate("state")` when the accepted field was `due_action` or
  `focus_on` (the HTTP contract's own note: "proposal accept of
  due/focus also invalidates state" -- a rejection, or an accepted
  `rule`, changes no `user_state` field, so neither publishes it).

404 vs 409 needs the row's current status *before* calling accept/
reject, since both of those already collapse "never existed" and "not
pending any more" into the same `None` return (that collapse is what
makes a replayed Telegram callback safe -- see their own docstrings);
the two questions this endpoint must answer separately are told apart
here by a plain `session.get` first.
"""

from __future__ import annotations

import logging

from aiohttp import web
from sqlalchemy import select

from app.core import proposal as proposal_core
from app.web.hub import WebHub
from app.web.http import _cookie_settings, _json, _rate_limited, _read_body, _session_token_valid
from app.web.ratelimit import WebRateLimiter
from app.tg import proposals as proposals_ui

logger = logging.getLogger(__name__)

RECENT_LIMIT = 20


def _proposal_dto(row: proposal_core.Proposal) -> dict:
    return {
        "id": row.id,
        "field": row.field,
        "field_label": proposals_ui.FIELD_LABELS.get(row.field, row.field),
        "value": row.value,
        "reason": row.reason,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
        "decided_at": row.decided_at.isoformat() if row.decided_at is not None else None,
    }


async def _recent_decided(session) -> list[proposal_core.Proposal]:
    result = await session.execute(
        select(proposal_core.Proposal)
        .where(proposal_core.Proposal.status != proposal_core.PENDING)
        .order_by(proposal_core.Proposal.decided_at.desc(), proposal_core.Proposal.id.desc())
        .limit(RECENT_LIMIT)
    )
    return list(result.scalars())


# --- GET /api/proposals ---------------------------------------------------


async def get_proposals(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    _settings, sessionmaker, _clock, _bot = _cookie_settings(request)

    async with sessionmaker() as session:
        pending = await proposal_core.get_pending(session)
        recent = await _recent_decided(session)

    return _json(
        200,
        {
            "pending": _proposal_dto(pending) if pending is not None else None,
            "recent": [_proposal_dto(row) for row in recent],
        },
    )


# --- POST /api/proposals/{id}/accept|reject --------------------------------


def _parse_proposal_id(request: web.Request) -> int | None:
    try:
        return int(request.match_info["id"])
    except ValueError:
        return None


async def _decide(request: web.Request, *, accept: bool) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, sessionmaker, clock, bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]
    hub: WebHub = request.app["web_hub"]

    proposal_id = _parse_proposal_id(request)
    if proposal_id is None:
        return _json(404, {"error": "not_found"})

    body, error = await _read_body(request)
    if error is not None:
        return error

    retry = limiter.check_panel_write()
    if retry is not None:
        return _rate_limited(retry)

    async with sessionmaker() as session:
        row = await session.get(proposal_core.Proposal, proposal_id)
        if row is None:
            return _json(404, {"error": "not_found"})
        if row.status != proposal_core.PENDING:
            return _json(409, {"error": "not_pending", "proposal": _proposal_dto(row)})

        if accept:
            decided = await proposal_core.accept(session, clock, proposal_id)
        else:
            decided = await proposal_core.reject(session, clock, proposal_id)
        # decided is None only on the same not-pending race _decide's own
        # pre-check above already guards against in the common case; a
        # concurrent decision landing between that check and this call is
        # the one window left, and is answered the same way: 409, current
        # state included.
        if decided is None:
            current = await session.get(proposal_core.Proposal, proposal_id)
            return _json(409, {"error": "not_pending", "proposal": _proposal_dto(current)})

    # Outside the session block -- decided.status/user_state are already
    # committed at this point -- so the Telegram HTTP round-trip below
    # neither holds the DB connection open nor can turn an already-
    # successful decision into a 500. Best-effort, the same guard
    # app/tg/proposals.py's own `send_proposal` puts around
    # `retire_buttons`: `edit_keyboard` re-raises every Telegram error
    # but "message is not modified" (a deleted message, a network
    # error, ...), and by now accept()/reject() has already applied and
    # committed its side effects, so a Telegram-side failure here must
    # not be reported as this request having failed, and must not skip
    # the invalidates below.
    if decided.tg_message_id is not None:
        try:
            await proposals_ui.show_decision_outcome(
                bot,
                chat_id=settings.ALLOWED_CHAT_ID,
                message_id=decided.tg_message_id,
                decided=decided,
                accepted=accept,
            )
        except Exception as exc:  # noqa: BLE001 - best-effort, matching send_proposal's own guard
            logger.warning(
                "showing proposal decision outcome failed",
                extra={"proposal_id": decided.id, "event": type(exc).__name__},
            )

    hub.publish_invalidate("proposals")
    if accept and decided.field in (proposal_core.DUE_ACTION, proposal_core.FOCUS_ON):
        hub.publish_invalidate("state")

    return _json(200, {"proposal": _proposal_dto(decided)})


async def post_accept(request: web.Request) -> web.Response:
    return await _decide(request, accept=True)


async def post_reject(request: web.Request) -> web.Response:
    return await _decide(request, accept=False)


# --- registration --------------------------------------------------------


def register(app: web.Application) -> None:
    app.router.add_get("/api/proposals", get_proposals)
    app.router.add_post("/api/proposals/{id}/accept", post_accept)
    app.router.add_post("/api/proposals/{id}/reject", post_reject)

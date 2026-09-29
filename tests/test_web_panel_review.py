"""app/web/panels/review.py end to end: the weekly review and persona
amendments on Дневник.

Covers: 401/403/429; GET /api/review before any review and with one
(the analysis lists, proposals with their status and linked order);
POST /api/review/run queues a synthetic /review web update and sends
nothing itself; accept/reject of a standing-order proposal (the order
activates or is declined and the proposal is marked with it -- the
same app/core/review_actions.py path Telegram's so:* buttons take) and
of a persona note (a trial amendment plus its queued trial job, or a
rejection); 404/409 for unknown or already-decided proposals and the
amendment cap; GET /api/amendments and revoke; invalidate("review");
silence in Telegram; no proposal text in logs.
"""

from __future__ import annotations

import json
import logging
import re

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from app.config import Settings
from app.core import amendments, orders, review
from app.core.clock import FrozenClock
from app.db.models import Job, PersonaAmendment, ReviewProposal, StandingOrder, TelegramUpdate, UserState, WeeklyReview
from app.web.hub import WebHub
from app.web.ratelimit import WebRateLimiter
from app.web.routes import setup_web
from app.web.sink import make_web_bot
from conftest import make_bot
from scripts.web_passphrase import make_hash

import datetime

pytestmark = pytest.mark.asyncio

CHAT_ID = 4242
PASSPHRASE = "correct horse battery staple"
HASH = make_hash(PASSPHRASE)
ORIGIN = "https://anchor.example.test"
TIMEZONE = "Europe/Paris"
START = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)

API_HEADERS = {"Sec-Fetch-Site": "same-origin", "Origin": ORIGIN}
JSON_HEADERS = {**API_HEADERS, "Content-Type": "application/json"}


def _settings(**overrides) -> Settings:
    base = dict(
        ALLOWED_CHAT_ID=CHAT_ID,
        PUBLIC_URL=ORIGIN,
        WEB_PASSPHRASE_HASH=HASH,
        WEB_SESSION_IDLE_HOURS=72,
        WEB_SESSION_MAX_DAYS=14,
        WEB_LOGIN_CODE_TTL_S=300,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(settings: Settings, sessionmaker, clock, bot, hub=None):
    app = web.Application()
    app["settings"] = settings
    app["sessionmaker"] = sessionmaker
    app["clock"] = clock
    app["bot"] = bot
    hub = hub or WebHub()
    web_bot = make_web_bot("123456:TEST", hub)
    setup_web(app, hub=hub, web_bot=web_bot)
    return app, hub, web_bot


def _cookie(resp, name: str) -> str:
    return resp.cookies[name].value


async def _post(client, path, body, cookies: dict | None = None, headers=None):
    hdrs = dict(headers or JSON_HEADERS)
    if cookies:
        hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return await client.post(path, headers=hdrs, data=json.dumps(body))


async def _get(client, path, cookies: dict | None = None, headers=None):
    hdrs = dict(headers or API_HEADERS)
    if cookies:
        hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return await client.get(path, headers=hdrs)


async def _log_in(client, fake_session) -> dict:
    resp = await _post(client, "/api/auth/passphrase", {"passphrase": PASSPHRASE})
    assert resp.status == 200
    pre_token = _cookie(resp, "__Host-anchor_pre")
    code = re.search(r"[0-9A-Z]{4}-[0-9A-Z]{4}", fake_session.sent[-1].text).group(0)
    resp = await _post(
        client, "/api/auth/code", {"code": code}, cookies={"__Host-anchor_pre": pre_token}
    )
    assert resp.status == 200
    return {"__Host-anchor_s": _cookie(resp, "__Host-anchor_s")}


async def _seed_state(sessionmaker) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE))
        await session.commit()



def _topics(hub: WebHub) -> list[str]:
    return [record.data["topic"] for record in hub._buffer if record.event == "invalidate"]


async def _app(sessionmaker):
    bot, fake = make_bot()
    app, hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    return app, hub, fake


ANALYSIS = {
    "wins": ["гулял каждый день"],
    "misses": ["поздно ложился"],
    "patterns": ["устаёт к пятнице"],
    "intentions": ["ложиться до полуночи"],
    "proposals": [],
}


async def _review_with(sessionmaker, *, order: bool = False, note: bool = False) -> dict:
    """A stored review with an optional standing-order proposal (plus
    its proposed order) and an optional persona-note proposal."""
    ids: dict = {}
    async with sessionmaker() as session:
        row = WeeklyReview(week_start=START.date(), analysis=ANALYSIS)
        session.add(row)
        await session.commit()
        ids["review"] = row.id
        if order:
            proposal = ReviewProposal(review_id=row.id, kind="standing_order", text="Прогулка", reason="помогает")
            session.add(proposal)
            await session.commit()
            order_row = await orders.propose(session, "Прогулка", "daily", None, source="review")
            await orders.link_review_proposal(session, order_row.id, proposal.id)
            ids["order_proposal"], ids["order"] = proposal.id, order_row.id
        if note:
            proposal = ReviewProposal(review_id=row.id, kind="persona_note", text="меньше вопросов по утрам")
            session.add(proposal)
            await session.commit()
            ids["note_proposal"] = proposal.id
    return ids


async def test_review_endpoints_require_a_session(sessionmaker):
    app, _hub, _fake = await _app(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        assert (await _get(client, "/api/review")).status == 401
        assert (await _post(client, "/api/review/run", {})).status == 401
        assert (await _post(client, "/api/review/proposals/1/accept", {})).status == 401
        assert (await _get(client, "/api/amendments")).status == 401
        assert (await _post(client, "/api/amendments/1/revoke", {})).status == 401


async def test_review_rejects_a_foreign_origin(sessionmaker):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(
            client, "/api/review", cookies=cookies,
            headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
        )
        assert resp.status == 403


async def test_get_review_before_any(sessionmaker):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        assert (await (await _get(client, "/api/review", cookies=cookies)).json()) == {"review": None}


async def test_get_review_shows_analysis_and_proposals(sessionmaker):
    await _seed_state(sessionmaker)
    ids = await _review_with(sessionmaker, order=True, note=True)
    app, _hub, fake = await _app(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        body = (await (await _get(client, "/api/review", cookies=cookies)).json())["review"]

    assert body["id"] == ids["review"]
    assert body["wins"] == ["гулял каждый день"]
    assert body["intentions"] == ["ложиться до полуночи"]
    order_p, note_p = body["proposals"]
    assert order_p["kind"] == "standing_order" and order_p["status"] == "pending"
    assert order_p["order"] == {"id": ids["order"], "status": "proposed", "cadence_label": "ежедневно"}
    assert note_p["kind"] == "persona_note" and note_p["order"] is None


async def test_run_queues_a_synthetic_review_and_sends_nothing(sessionmaker):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        sent_before = len(fake.sent)
        resp = await _post(client, "/api/review/run", {}, cookies=cookies)
        assert resp.status == 202
        assert len(fake.sent) == sent_before

    async with sessionmaker() as session:
        rows = list(
            (await session.execute(select(TelegramUpdate).where(TelegramUpdate.update_id < 0))).scalars()
        )
    assert len(rows) == 1
    assert rows[0].payload["message"]["text"] == "/review"


async def test_accept_order_proposal_activates_the_order(sessionmaker):
    await _seed_state(sessionmaker)
    ids = await _review_with(sessionmaker, order=True)
    app, hub, fake = await _app(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        sent_before = len(fake.sent)
        resp = await _post(client, f"/api/review/proposals/{ids['order_proposal']}/accept", {}, cookies=cookies)
        assert resp.status == 200
        assert (await resp.json())["proposal"] == {"id": ids["order_proposal"], "status": "adopted"}
        again = await _post(client, f"/api/review/proposals/{ids['order_proposal']}/reject", {}, cookies=cookies)
        assert again.status == 409
        assert len(fake.sent) == sent_before

    async with sessionmaker() as session:
        order = await session.get(StandingOrder, ids["order"])
    assert order.status == "active"
    assert {"review", "orders", "checkin"} <= set(_topics(hub))


async def test_reject_order_proposal_declines_the_order(sessionmaker):
    await _seed_state(sessionmaker)
    ids = await _review_with(sessionmaker, order=True)
    app, hub, fake = await _app(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, f"/api/review/proposals/{ids['order_proposal']}/reject", {}, cookies=cookies)
        assert resp.status == 200

    async with sessionmaker() as session:
        order = await session.get(StandingOrder, ids["order"])
        proposal = await session.get(ReviewProposal, ids["order_proposal"])
    assert order.status == "declined"
    assert proposal.status == "rejected"
    assert "orders" not in _topics(hub)


async def test_accept_order_proposal_at_the_cap_keeps_it_pending(sessionmaker):
    await _seed_state(sessionmaker)
    ids = await _review_with(sessionmaker, order=True)
    async with sessionmaker() as session:
        for i in range(5):
            session.add(StandingOrder(text=f"Дело {i}", cadence="daily", status="active", source="user"))
        await session.commit()
    app, _hub, fake = await _app(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, f"/api/review/proposals/{ids['order_proposal']}/accept", {}, cookies=cookies)
        assert resp.status == 409
        assert (await resp.json())["message"] == orders.CAP_TEXT

    async with sessionmaker() as session:
        assert (await session.get(ReviewProposal, ids["order_proposal"])).status == "pending"
        assert (await session.get(StandingOrder, ids["order"])).status == "proposed"


async def test_adopt_persona_note_starts_a_trial_and_queues_it(sessionmaker):
    await _seed_state(sessionmaker)
    ids = await _review_with(sessionmaker, note=True)
    app, hub, fake = await _app(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, f"/api/review/proposals/{ids['note_proposal']}/accept", {}, cookies=cookies)
        assert resp.status == 200

    async with sessionmaker() as session:
        rows = list((await session.execute(select(PersonaAmendment))).scalars())
        jobs = list((await session.execute(select(Job))).scalars())
    assert [(r.text, r.status, r.proposal_id) for r in rows] == [
        ("меньше вопросов по утрам", amendments.TRIAL, ids["note_proposal"])
    ]
    assert [(j.kind, j.payload) for j in jobs] == [(amendments.AMENDMENT_TRIAL, {"amendment_id": rows[0].id})]
    assert "review" in _topics(hub)


async def test_reject_persona_note(sessionmaker):
    await _seed_state(sessionmaker)
    ids = await _review_with(sessionmaker, note=True)
    app, _hub, fake = await _app(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, f"/api/review/proposals/{ids['note_proposal']}/reject", {}, cookies=cookies)
        assert resp.status == 200
    async with sessionmaker() as session:
        assert (await session.get(ReviewProposal, ids["note_proposal"])).status == review.REJECTED
        assert list((await session.execute(select(PersonaAmendment))).scalars()) == []


@pytest.mark.parametrize("raw_id", ["999999", "abc", "0"])
async def test_decide_unknown_proposal_is_404(sessionmaker, raw_id):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        assert (await _post(client, f"/api/review/proposals/{raw_id}/accept", {}, cookies=cookies)).status == 404


async def test_amendments_list_and_revoke(sessionmaker):
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        row = PersonaAmendment(text="короче по утрам", status=amendments.ACTIVE, persona_sha="old-sha")
        session.add(row)
        await session.commit()
        amendment_id = row.id
    app, hub, fake = await _app(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        body = await (await _get(client, "/api/amendments", cookies=cookies)).json()
        assert [(i["id"], i["text"], i["stale"]) for i in body["items"]] == [(amendment_id, "короче по утрам", True)]
        assert (await _post(client, f"/api/amendments/{amendment_id}/revoke", {}, cookies=cookies)).status == 200
        assert (await _post(client, f"/api/amendments/{amendment_id}/revoke", {}, cookies=cookies)).status == 404
        assert (await (await _get(client, "/api/amendments", cookies=cookies)).json())["items"] == []
    async with sessionmaker() as session:
        assert (await session.get(PersonaAmendment, amendment_id)).status == amendments.REVOKED
    assert "review" in _topics(hub)


async def test_review_never_logs_proposal_text(sessionmaker, caplog):
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        row = WeeklyReview(week_start=START.date(), analysis=ANALYSIS)
        session.add(row)
        await session.commit()
        proposal = ReviewProposal(review_id=row.id, kind="persona_note", text="SECRETNOTE короче")
        session.add(proposal)
        await session.commit()
        pid = proposal.id
    app, _hub, fake = await _app(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        with caplog.at_level(logging.DEBUG):
            await _get(client, "/api/review", cookies=cookies)
            await _post(client, f"/api/review/proposals/{pid}/reject", {}, cookies=cookies)
    blob = "\n".join(record.getMessage() + str(record.__dict__) for record in caplog.records)
    assert "SECRETNOTE" not in blob


async def test_decide_rate_limited(sessionmaker):
    await _seed_state(sessionmaker)
    ids = await _review_with(sessionmaker, note=True)
    app, _hub, fake = await _app(sessionmaker)
    limiter: WebRateLimiter = app["web_rate_limiter"]
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        for _ in range(60):
            assert limiter.check_panel_write() is None
        resp = await _post(client, f"/api/review/proposals/{ids['note_proposal']}/accept", {}, cookies=cookies)
        assert resp.status == 429

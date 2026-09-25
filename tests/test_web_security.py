"""app/web/security.py (web-chat plan track 2, design section 5).

- every security header lands on /, /api/* and /static/*
- /healthz (and friends outside the web paths) are left untouched
- a missing Sec-Fetch-Site on /api/* fails closed (403)
- cross-site/same-site Sec-Fetch-Site -> 403
- a wrong Origin -> 403; a missing Origin on a mutating request -> 403
- non-JSON Content-Type on a mutating /api/* request -> 415
- an oversized body -> 413; no Content-Length -> 411
- no Access-Control-* header is ever sent
- Cache-Control: no-store on / and /api/*
- HSTS only over https
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.config import Settings
from app.web import security

ORIGIN = "https://anchor.example.com"


def _settings(public_url: str = ORIGIN) -> Settings:
    return Settings(PUBLIC_URL=public_url)


async def _plain_ok(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def _echo_body(request: web.Request) -> web.Response:
    body, error = await _read(request)
    if error is not None:
        return error
    return web.json_response(body)


async def _read(request):
    try:
        body = await security.read_json_bounded(request)
    except security.LengthRequired:
        return None, web.json_response({"error": "length_required"}, status=411)
    except security.PayloadTooLarge:
        return None, web.json_response({"error": "payload_too_large"}, status=413)
    except security.BadJson:
        return None, web.json_response({"error": "bad_request"}, status=400)
    return body, None


def _build_app(settings: Settings) -> web.Application:
    app = web.Application(middlewares=[security.build_middleware(settings)])
    app.router.add_get("/", _plain_ok)
    app.router.add_get("/static/app.js", _plain_ok)
    app.router.add_get("/api/me", _plain_ok)
    app.router.add_post("/api/send", _echo_body)
    app.router.add_get("/healthz", _plain_ok)
    return app


SAME_ORIGIN_HEADERS = {"Sec-Fetch-Site": "same-origin", "Origin": ORIGIN}


async def test_headers_present_on_index():
    app = _build_app(_settings())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/")
        for name in (
            "Content-Security-Policy",
            "X-Content-Type-Options",
            "X-Frame-Options",
            "Referrer-Policy",
            "Permissions-Policy",
            "Cross-Origin-Opener-Policy",
            "Cross-Origin-Resource-Policy",
            "X-Robots-Tag",
            "Cache-Control",
        ):
            assert name in resp.headers, f"missing {name}"
        assert resp.headers["Cache-Control"] == "no-store"
        assert "trusted-types 'none'" in resp.headers["Content-Security-Policy"]
        assert "font-src 'self'" in resp.headers["Content-Security-Policy"]


async def test_headers_present_on_api():
    app = _build_app(_settings())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/me", headers=SAME_ORIGIN_HEADERS)
        assert resp.status == 200
        assert resp.headers["Cache-Control"] == "no-store"
        assert "Content-Security-Policy" in resp.headers


async def test_headers_present_on_static():
    app = _build_app(_settings())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/static/app.js")
        assert resp.status == 200
        assert "Content-Security-Policy" in resp.headers


async def test_healthz_is_left_untouched():
    app = _build_app(_settings())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        assert resp.status == 200
        assert "Content-Security-Policy" not in resp.headers
        assert "Cache-Control" not in resp.headers


async def test_hsts_only_over_https():
    async with TestClient(TestServer(_build_app(_settings("https://x.example")))) as client:
        resp = await client.get("/")
        assert "Strict-Transport-Security" in resp.headers

    async with TestClient(TestServer(_build_app(_settings("http://localhost")))) as client:
        resp = await client.get("/")
        assert "Strict-Transport-Security" not in resp.headers


async def test_no_cors_headers_ever():
    app = _build_app(_settings())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/me", headers=SAME_ORIGIN_HEADERS)
        for name in resp.headers:
            assert not name.lower().startswith("access-control-")


# --- CSRF: fetch metadata + Origin ---


async def test_missing_sec_fetch_site_fails_closed():
    app = _build_app(_settings())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/me")  # no Sec-Fetch-Site header at all
        assert resp.status == 403
        assert (await resp.json())["error"] == "forbidden"


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
async def test_cross_site_and_same_site_are_rejected(site):
    app = _build_app(_settings())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/me", headers={"Sec-Fetch-Site": site})
        assert resp.status == 403


async def test_none_is_accepted_for_a_get_but_not_a_post():
    app = _build_app(_settings())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/me", headers={"Sec-Fetch-Site": "none"})
        assert resp.status == 200

        resp = await client.post(
            "/api/send",
            headers={"Sec-Fetch-Site": "none", "Content-Type": "application/json"},
            data=json.dumps({}),
        )
        assert resp.status == 403


async def test_wrong_origin_is_rejected():
    app = _build_app(_settings())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/api/me", headers={"Sec-Fetch-Site": "same-origin", "Origin": "https://evil.example"}
        )
        assert resp.status == 403


async def test_mutating_request_with_no_origin_is_rejected():
    app = _build_app(_settings())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/send",
            headers={"Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"},
            data=json.dumps({}),
        )
        assert resp.status == 403


async def test_correct_origin_and_same_origin_site_is_accepted():
    app = _build_app(_settings())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/send",
            headers={**SAME_ORIGIN_HEADERS, "Content-Type": "application/json"},
            data=json.dumps({"text": "hi"}),
        )
        assert resp.status == 200


async def test_non_json_content_type_on_mutating_request_415():
    app = _build_app(_settings())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/send",
            headers={**SAME_ORIGIN_HEADERS, "Content-Type": "text/plain"},
            data="hi",
        )
        assert resp.status == 415


async def test_get_does_not_require_json_content_type():
    app = _build_app(_settings())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/me", headers=SAME_ORIGIN_HEADERS)
        assert resp.status == 200


# --- bounded JSON reading ---


async def test_oversized_body_returns_413():
    app = _build_app(_settings())
    async with TestClient(TestServer(app)) as client:
        big = json.dumps({"text": "x" * (security.MAX_BODY_BYTES + 100)})
        resp = await client.post(
            "/api/send",
            headers={**SAME_ORIGIN_HEADERS, "Content-Type": "application/json"},
            data=big,
        )
        assert resp.status == 413


async def test_missing_content_length_returns_411():
    app = _build_app(_settings())

    async def _no_length_body():
        yield b'{"text": "hi"}'

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/send",
            headers={**SAME_ORIGIN_HEADERS, "Content-Type": "application/json"},
            data=_no_length_body(),  # chunked transfer -> no Content-Length
        )
        assert resp.status == 411


async def test_bad_json_returns_400():
    app = _build_app(_settings())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/send",
            headers={**SAME_ORIGIN_HEADERS, "Content-Type": "application/json"},
            data="not json",
        )
        assert resp.status == 400

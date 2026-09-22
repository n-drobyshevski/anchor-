"""The fetcher, against a stub resolver and a stub transport (plan 5, 14).

No test here touches the network except `test_the_pin_reaches_the_socket
_while_the_name_reaches_the_host_header`, which talks to an aiohttp
server this process started on 127.0.0.1. That one is deliberate: the
whole SSRF design rests on aiohttp connecting to the resolver's address
while taking the Host header and the TLS server name from the URL, and
an assumption that load-bearing should be checked against the library
rather than against its documentation.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

import aiohttp
import pytest
from aiohttp import web

from app.research import errors, fetch as fetch_mod
from app.research.fetch import (
    Clip,
    FetchFailure,
    PinnedResolver,
    fetch,
    make_robots_cache,
)
from app.research.robots import RobotsCache

USER_AGENT = "AnchorBot/1.0 (personal, single-user; contact via repo owner)"
HTML = (
    "<html><head><title>Как высыпаться</title></head><body><nav>меню</nav>"
    "<article><p>Ложитесь спать в одно и то же время каждый день. Это самый "
    "простой приём, который работает почти для всех без исключений.</p>"
    "<p>Уберите телефон из спальни на всю ночь.</p></article>"
    "<footer>© 2026</footer></body></html>"
).encode("utf-8")


# --------------------------------------------------------------------- stubs


@dataclass
class Reply:
    """What the stub transport hands back for one URL."""

    status: int = 200
    body: bytes = b""
    content_type: str | None = "text/html; charset=utf-8"
    location: str | None = None
    set_cookie: str | None = None
    extra_headers: dict = field(default_factory=dict)

    def headers(self) -> dict:
        headers = dict(self.extra_headers)
        if self.content_type is not None:
            headers["Content-Type"] = self.content_type
        if self.location is not None:
            headers["Location"] = self.location
        if self.set_cookie is not None:
            headers["Set-Cookie"] = self.set_cookie
        return headers


class _Content:
    def __init__(self, body: bytes, counter: list[int]) -> None:
        self._body = body
        self._counter = counter

    async def _chunks(self, size: int):
        for start in range(0, len(self._body), size):
            self._counter[0] += 1
            yield self._body[start : start + size]

    def iter_chunked(self, size: int):
        return self._chunks(size)


class _Response:
    def __init__(self, reply: Reply, counter: list[int]) -> None:
        self.status = reply.status
        self.headers = reply.headers()
        self.content = _Content(reply.body, counter)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, transport: "Transport") -> None:
        self._transport = transport

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url: str, **kwargs):
        self._transport.requests.append((url, kwargs))
        reply = self._transport.replies.get(url)
        if reply is None:
            raise AssertionError(f"stub transport has no reply for {url!r}")
        return _Response(reply, self._transport.chunks)


class Transport:
    """A scripted stand-in for aiohttp, recording what the fetcher did."""

    def __init__(self, replies: dict[str, Reply]) -> None:
        self.replies = replies
        self.requests: list[tuple[str, dict]] = []
        self.pins: list[dict] = []
        self.chunks = [0]

    def opener(self, resolver: PinnedResolver):
        self.pins.append(dict(resolver._pins))
        return _Session(self)


def resolver_for(mapping: dict[str, list[str]]):
    """A stub DNS resolver. An unknown name raises, like a real one."""

    async def resolve(host: str, port: int):
        if host not in mapping:
            raise OSError("NXDOMAIN")
        return [ipaddress.ip_address(a) for a in mapping[host]]

    return resolve


PUBLIC = {"example.com": ["93.184.216.34"], "evil.io": ["93.184.216.34"]}


async def run_fetch(transport: Transport, url: str, *, dns=None, **kwargs):
    return await fetch(
        url,
        timeout_s=10,
        max_bytes=kwargs.pop("max_bytes", 2_000_000),
        max_redirects=kwargs.pop("max_redirects", 3),
        max_chars=kwargs.pop("max_chars", 15_000),
        user_agent=USER_AGENT,
        resolve=resolver_for(dns if dns is not None else PUBLIC),
        open_session=transport.opener,
        **kwargs,
    )


def allow_all_robots() -> RobotsCache:
    async def fetch_robots(url: str):
        return 404, ""

    return RobotsCache(fetch=fetch_robots, user_agent=USER_AGENT)


# ----------------------------------------------------------------- the happy path


async def test_an_ordinary_page_becomes_a_clip():
    transport = Transport({"https://example.com/sleep": Reply(body=HTML)})
    result = await run_fetch(transport, "https://example.com/sleep", robots=allow_all_robots())

    assert isinstance(result, Clip)
    assert result.domain == "example.com"
    assert result.title == "Как высыпаться"
    assert "Ложитесь спать в одно и то же время" in result.text
    # Page chrome is gone; only the main text survives.
    assert "меню" not in result.text
    assert "© 2026" not in result.text
    assert result.http_status == 200


async def test_extracted_text_is_whitespace_normalised_and_hashed():
    """Plan section 7.1 makes a quote prove itself as a verbatim
    substring of this text, so line wrapping must not decide whether a
    true quote is accepted."""
    import hashlib

    body = b"<html><body><article><p>one\n\n   two\tthree</p></article></body></html>"
    transport = Transport({"https://example.com/x": Reply(body=body)})
    result = await run_fetch(transport, "https://example.com/x", robots=allow_all_robots())

    assert "  " not in result.text
    assert "\n" not in result.text
    assert result.text_sha256 == hashlib.sha256(result.text.encode("utf-8")).hexdigest()


async def test_text_is_truncated_to_the_character_budget():
    long_body = ("<html><body><article><p>" + "слово " * 5000 + "</p></article></body></html>").encode()
    transport = Transport({"https://example.com/x": Reply(body=long_body)})
    result = await run_fetch(
        transport, "https://example.com/x", max_chars=500, robots=allow_all_robots()
    )
    assert len(result.text) == 500


async def test_plain_text_skips_html_extraction():
    transport = Transport(
        {"https://example.com/x": Reply(body="просто   текст\nстраницы".encode(), content_type="text/plain")}
    )
    result = await run_fetch(transport, "https://example.com/x", robots=allow_all_robots())
    assert result.text == "просто текст страницы"
    assert result.title is None


async def test_a_page_with_no_main_text_is_an_empty_extraction():
    transport = Transport(
        {"https://example.com/x": Reply(body=b"<html><body><div id=app></div></body></html>")}
    )
    result = await run_fetch(transport, "https://example.com/x", robots=allow_all_robots())
    assert result == FetchFailure(
        error=errors.EMPTY_EXTRACTION, domain="example.com", http_status=200
    )


# ------------------------------------------------------------------------ SSRF


@pytest.mark.parametrize(
    "address,label",
    [
        ("127.0.0.1", "loopback"),
        ("10.1.2.3", "rfc1918"),
        ("169.254.169.254", "cloud metadata"),
        ("100.64.0.1", "cgnat"),
        ("fd00::1", "ipv6 ula"),
        ("::ffff:127.0.0.1", "ipv4-mapped loopback"),
    ],
)
async def test_a_name_resolving_to_a_non_public_address_is_refused(address, label):
    transport = Transport({})
    result = await run_fetch(
        transport,
        "https://example.com/x",
        dns={"example.com": [address]},
        robots=allow_all_robots(),
    )
    assert result == FetchFailure(error=errors.BLOCKED_PRIVATE_IP, domain="example.com")
    assert transport.requests == [], f"{label}: no request may leave the machine"


async def test_a_name_resolving_to_public_and_private_is_refused_entirely():
    transport = Transport({})
    result = await run_fetch(
        transport,
        "https://example.com/x",
        dns={"example.com": ["93.184.216.34", "127.0.0.1"]},
        robots=allow_all_robots(),
    )
    assert result.error == errors.BLOCKED_PRIVATE_IP
    assert transport.requests == []


async def test_a_name_that_does_not_resolve_is_a_dns_error():
    transport = Transport({})
    result = await run_fetch(transport, "https://nowhere.example/x", dns={}, robots=allow_all_robots())
    assert result.error == errors.DNS_ERROR


async def test_only_the_vetted_addresses_are_pinned():
    transport = Transport({"https://example.com/x": Reply(body=HTML)})
    await run_fetch(transport, "https://example.com/x", robots=allow_all_robots())
    assert transport.pins == [{"example.com": [ipaddress.ip_address("93.184.216.34")]}]


async def test_the_pinned_resolver_refuses_a_host_it_was_not_given():
    """A redirect that slipped past the hop loop cannot reach the network."""
    resolver = PinnedResolver({"example.com": [ipaddress.ip_address("93.184.216.34")]})
    with pytest.raises(OSError):
        await resolver.resolve("evil.io", 443)


async def test_the_pinned_resolver_reports_the_name_not_the_address():
    """aiohttp takes TLS's server_hostname from the request URL, and the
    `hostname` field here is what it echoes back in tracing. Both must
    stay the name; only `host` becomes the vetted address."""
    resolver = PinnedResolver({"example.com": [ipaddress.ip_address("93.184.216.34")]})
    [result] = await resolver.resolve("example.com", 443)
    assert result["hostname"] == "example.com"
    assert result["host"] == "93.184.216.34"


# ------------------------------------------------------------------- redirects


async def test_a_redirect_to_a_private_address_is_refused_at_the_new_hop():
    transport = Transport(
        {"https://example.com/x": Reply(status=302, location="http://169.254.169.254/latest/")}
    )
    result = await run_fetch(transport, "https://example.com/x", robots=allow_all_robots())
    assert result.error == errors.BLOCKED_PRIVATE_IP
    # Hop one went out; hop two did not.
    assert len(transport.requests) == 1


async def test_a_redirect_to_a_name_resolving_privately_is_refused():
    transport = Transport({"https://example.com/x": Reply(status=302, location="https://evil.io/y")})
    result = await run_fetch(
        transport,
        "https://example.com/x",
        dns={"example.com": ["93.184.216.34"], "evil.io": ["127.0.0.1"]},
        robots=allow_all_robots(),
    )
    assert result == FetchFailure(error=errors.BLOCKED_PRIVATE_IP, domain="evil.io")


async def test_redirects_are_followed_up_to_the_cap():
    transport = Transport(
        {
            "https://example.com/1": Reply(status=301, location="/2"),
            "https://example.com/2": Reply(status=302, location="/3"),
            "https://example.com/3": Reply(status=307, location="/4"),
            "https://example.com/4": Reply(body=HTML),
        }
    )
    result = await run_fetch(
        transport, "https://example.com/1", max_redirects=3, robots=allow_all_robots()
    )
    assert isinstance(result, Clip)
    assert result.url == "https://example.com/4"


async def test_one_redirect_past_the_cap_stops():
    transport = Transport(
        {
            "https://example.com/1": Reply(status=301, location="/2"),
            "https://example.com/2": Reply(status=301, location="/3"),
            "https://example.com/3": Reply(status=301, location="/4"),
            "https://example.com/4": Reply(status=301, location="/5"),
            "https://example.com/5": Reply(body=HTML),
        }
    )
    result = await run_fetch(
        transport, "https://example.com/1", max_redirects=3, robots=allow_all_robots()
    )
    assert result.error == errors.TOO_MANY_REDIRECTS
    assert len(transport.requests) == 4


async def test_a_redirect_without_a_location_is_an_http_error():
    transport = Transport({"https://example.com/x": Reply(status=302)})
    result = await run_fetch(transport, "https://example.com/x", robots=allow_all_robots())
    assert result.error == errors.HTTP_ERROR
    assert result.http_status == 302


# --------------------------------------------------------- the packet allowlist


async def test_a_study_fetch_outside_the_packet_is_refused():
    transport = Transport({})
    result = await run_fetch(
        transport, "https://evil.io/x", allowed_domains=["reddit.com"], robots=allow_all_robots()
    )
    assert result == FetchFailure(error=errors.BLOCKED_DOMAIN, domain="evil.io")
    assert transport.requests == []


async def test_a_redirect_cannot_walk_a_study_fetch_off_its_packet():
    """The domain rule is re-applied at every hop, or the allowlist is
    decorative: any allowlisted page could 302 us anywhere."""
    transport = Transport(
        {"https://example.com/x": Reply(status=302, location="https://evil.io/y")}
    )
    result = await run_fetch(
        transport,
        "https://example.com/x",
        allowed_domains=["example.com"],
        robots=allow_all_robots(),
    )
    assert result == FetchFailure(error=errors.BLOCKED_DOMAIN, domain="evil.io")
    assert len(transport.requests) == 1


async def test_a_read_fetch_has_no_packet_restriction():
    transport = Transport({"https://evil.io/x": Reply(body=HTML)})
    result = await run_fetch(
        transport, "https://evil.io/x", allowed_domains=None, robots=allow_all_robots()
    )
    assert isinstance(result, Clip)


# ----------------------------------------------------------------- our limits


async def test_an_oversize_body_is_aborted_mid_stream():
    big = b"<html><body><p>" + b"x" * 1_000_000 + b"</p></body></html>"
    transport = Transport({"https://example.com/x": Reply(body=big)})
    result = await run_fetch(
        transport, "https://example.com/x", max_bytes=50_000, robots=allow_all_robots()
    )
    assert result.error == errors.TOO_LARGE
    # 50_000 / 16_384 -> we stop on the fourth chunk, not after all 62.
    assert transport.chunks[0] == 4


async def test_a_declared_oversize_length_is_refused_before_reading():
    transport = Transport(
        {
            "https://example.com/x": Reply(
                body=HTML, extra_headers={"Content-Length": "9000000"}
            )
        }
    )
    result = await run_fetch(
        transport, "https://example.com/x", max_bytes=2_000_000, robots=allow_all_robots()
    )
    assert result.error == errors.TOO_LARGE
    assert transport.chunks[0] == 0


@pytest.mark.parametrize(
    "content_type",
    ["application/pdf", "image/png", "application/json", "text/xml", None],
)
async def test_a_non_text_content_type_is_refused(content_type):
    transport = Transport({"https://example.com/x": Reply(body=HTML, content_type=content_type)})
    result = await run_fetch(transport, "https://example.com/x", robots=allow_all_robots())
    assert result.error == errors.BAD_CONTENT_TYPE


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
async def test_a_refusal_is_recorded_not_worked_around(status):
    """Plan section 5.9: when a site says no, that is a finding. There is
    no retry with a different User-Agent anywhere in this module."""
    transport = Transport({"https://example.com/x": Reply(status=status)})
    result = await run_fetch(transport, "https://example.com/x", robots=allow_all_robots())
    assert result == FetchFailure(
        error=errors.HTTP_ERROR, domain="example.com", http_status=status
    )
    assert len(transport.requests) == 1


async def test_a_userinfo_url_never_reaches_the_resolver():
    transport = Transport({})
    result = await run_fetch(transport, "https://user:pass@example.com/x", robots=allow_all_robots())
    assert result.error == errors.BLOCKED_USERINFO
    assert transport.requests == []


# ------------------------------------------------------------------- no cookies


async def test_the_real_session_cannot_hold_a_cookie():
    session = fetch_mod.open_real_session(
        PinnedResolver({}), timeout_s=5, user_agent=USER_AGENT
    )
    try:
        assert isinstance(session.cookie_jar, aiohttp.DummyCookieJar)
        session.cookie_jar.update_cookies({"session": "secret"})
        assert len(session.cookie_jar) == 0
    finally:
        await session.close()


async def test_a_cookie_set_on_one_hop_is_not_sent_on_the_next():
    transport = Transport(
        {
            "https://example.com/1": Reply(
                status=302, location="/2", set_cookie="sid=abc; Path=/"
            ),
            "https://example.com/2": Reply(body=HTML),
        }
    )
    result = await run_fetch(transport, "https://example.com/1", robots=allow_all_robots())
    assert isinstance(result, Clip)
    for _, kwargs in transport.requests:
        assert "Cookie" not in kwargs.get("headers", {})


async def test_no_request_carries_credentials_or_a_referer():
    transport = Transport({"https://example.com/x": Reply(body=HTML)})
    await run_fetch(transport, "https://example.com/x", robots=allow_all_robots())
    session = fetch_mod.open_real_session(PinnedResolver({}), timeout_s=5, user_agent=USER_AGENT)
    try:
        assert session.headers["User-Agent"] == USER_AGENT
        assert "Authorization" not in session.headers
        assert "Referer" not in session.headers
        assert "Cookie" not in session.headers
    finally:
        await session.close()


# --------------------------------------------------------------------- robots


async def test_a_disallowed_path_is_not_fetched():
    robots = RobotsCache(
        fetch=_static_robots(200, "User-agent: *\nDisallow: /private"),
        user_agent=USER_AGENT,
    )
    transport = Transport({"https://example.com/private/x": Reply(body=HTML)})
    result = await run_fetch(transport, "https://example.com/private/x", robots=robots)
    assert result == FetchFailure(error=errors.ROBOTS_DISALLOW, domain="example.com")
    assert transport.requests == []


async def test_a_rule_naming_us_specifically_is_obeyed():
    robots = RobotsCache(
        fetch=_static_robots(200, "User-agent: AnchorBot\nDisallow: /\n\nUser-agent: *\nAllow: /"),
        user_agent=USER_AGENT,
    )
    transport = Transport({"https://example.com/x": Reply(body=HTML)})
    result = await run_fetch(transport, "https://example.com/x", robots=robots)
    assert result.error == errors.ROBOTS_DISALLOW


async def test_an_allowed_path_on_a_site_with_rules_is_fetched():
    robots = RobotsCache(
        fetch=_static_robots(200, "User-agent: *\nDisallow: /private"),
        user_agent=USER_AGENT,
    )
    transport = Transport({"https://example.com/public/x": Reply(body=HTML)})
    result = await run_fetch(transport, "https://example.com/public/x", robots=robots)
    assert isinstance(result, Clip)


async def test_a_missing_robots_file_allows_everything():
    robots = RobotsCache(fetch=_static_robots(404, ""), user_agent=USER_AGENT)
    transport = Transport({"https://example.com/x": Reply(body=HTML)})
    assert isinstance(await run_fetch(transport, "https://example.com/x", robots=robots), Clip)


@pytest.mark.parametrize("status", [401, 403, 500, 503])
async def test_robots_we_cannot_read_means_we_do_not_fetch(status):
    """RFC 9309 calls a 5xx "may treat as disallow"; we do. A site that
    cannot tell us its rules does not get fetched by us today."""
    robots = RobotsCache(fetch=_static_robots(status, ""), user_agent=USER_AGENT)
    transport = Transport({"https://example.com/x": Reply(body=HTML)})
    result = await run_fetch(transport, "https://example.com/x", robots=robots)
    assert result.error == errors.ROBOTS_DISALLOW


async def test_robots_is_fetched_once_per_host_per_job():
    calls = []

    async def counting(url):
        calls.append(url)
        return 200, "User-agent: *\nAllow: /"

    robots = RobotsCache(fetch=counting, user_agent=USER_AGENT)
    transport = Transport(
        {
            "https://example.com/a": Reply(body=HTML),
            "https://example.com/b": Reply(body=HTML),
        }
    )
    await run_fetch(transport, "https://example.com/a", robots=robots)
    await run_fetch(transport, "https://example.com/b", robots=robots)
    assert calls == ["https://example.com/robots.txt"]


async def test_the_robots_fetch_itself_is_address_vetted():
    """robots.txt is a URL like any other: a host that resolves privately
    is refused there too, and with its own code rather than a disallow."""
    transport = Transport({})
    robots = make_robots_cache(
        timeout_s=5,
        max_redirects=3,
        user_agent=USER_AGENT,
        resolve=resolver_for({"example.com": ["127.0.0.1"]}),
        open_session=transport.opener,
    )
    result = await robots.allows("https://example.com", "https://example.com/x")
    assert result == errors.BLOCKED_PRIVATE_IP
    assert transport.requests == []


def _static_robots(status: int, body: str):
    async def fetch_robots(url: str):
        return status, body

    return fetch_robots


# -------------------------------------------------- the pin, against real aiohttp


async def test_the_pin_reaches_the_socket_while_the_name_reaches_the_host_header():
    """The one assumption the whole design rests on, checked for real.

    We pin `example.test` -- a name that resolves nowhere -- to 127.0.0.1
    and serve on loopback. If aiohttp used the resolver's answer for the
    Host header, or re-resolved the name, this could not pass.
    """
    seen = {}

    async def handler(request: web.Request) -> web.Response:
        seen["host"] = request.headers.get("Host")
        seen["cookie"] = request.headers.get("Cookie")
        seen["ua"] = request.headers.get("User-Agent")
        seen["peer"] = request.transport.get_extra_info("sockname")[0]
        return web.Response(text="ok", content_type="text/plain")

    app = web.Application()
    app.router.add_get("/probe", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]

    resolver = PinnedResolver({"example.test": [ipaddress.ip_address("127.0.0.1")]})
    session = fetch_mod.open_real_session(resolver, timeout_s=5, user_agent=USER_AGENT)
    try:
        async with session.get(f"http://example.test:{port}/probe") as response:
            assert response.status == 200
    finally:
        await session.close()
        await runner.cleanup()

    assert seen["host"] == f"example.test:{port}"
    assert seen["peer"] == "127.0.0.1"
    assert seen["cookie"] is None
    assert seen["ua"] == USER_AGENT

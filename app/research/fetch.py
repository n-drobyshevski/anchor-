"""The SSRF-safe fetcher (phase-4 plan section 5).

Every byte this project reads from the web comes through `fetch()` --
pages found by /study, pages the user hands to /read, and robots.txt
itself. There is no second path, and there must never be one.

**Why the resolver, and not a URL rewrite.** Plan section 5.3 wants us
to connect to an address we have vetted and not to whatever DNS says a
moment later, while still sending the real hostname as SNI so the
certificate is validated against the site and not against an IP. aiohttp
gives that directly: `TCPConnector(resolver=...)` supplies the addresses
the socket connects to, while `connector.py` takes the TLS
`server_hostname` and the Host header from `req.url` -- the name, not
the resolver's answer (verified against aiohttp 3.14.3,
`_create_direct_connection`). So `PinnedResolver` hands back addresses
this module has already vetted, and DNS rebinding has nothing left to
rebind: the second lookup never happens.

**Why a session per hop.** Each redirect hop is a fresh host that must
be re-vetted from scratch (plan section 5.4), and a connector whose pin
map grows across hops is a cache -- exactly the thing we removed. Three
hops at most, so the cost is three TCP connections in the worst case.

**What never goes out.** No cookies (`DummyCookieJar`, so a `Set-Cookie`
on hop 1 cannot come back on hop 2), no Authorization, no Referer, no
credentials from the URL, no JavaScript. The User-Agent is ours and
honest, and when a site refuses us we record the code and stop: plan
section 5.9 forbids spoofing it, proxying around it, or finding a mirror.

**What may be logged.** The domain, the HTTP status, an error code from
`errors.py`, and durations. Never the path, the query, the title or the
text -- plan section 12.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Awaitable, Callable, Sequence
from urllib.parse import urljoin

import aiohttp
import trafilatura
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import ResolveResult

from app.research import errors
from app.research.addresses import (
    IPAddress,
    Target,
    domain_matches,
    parse_target,
    vet_addresses,
)
from app.research.robots import ROBOTS_MAX_BYTES, RobotsCache

TEXT_TYPES = ("text/html", "text/plain")
READ_CHUNK = 16 * 1024
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Clip:
    """A page we fetched and extracted. Shaped like a `study_clip` row."""

    url: str
    domain: str
    title: str | None
    text: str
    text_sha256: str
    http_status: int


@dataclass(frozen=True)
class FetchFailure:
    """Why a fetch stopped, in terms safe to store and to log."""

    error: str
    domain: str | None = None
    http_status: int | None = None


FetchResult = Clip | FetchFailure

# The two seams the tests drive (plan section 14: "a local stub resolver
# and transport; no real network").
Resolver = Callable[[str, int], Awaitable["list[IPAddress]"]]
SessionOpener = Callable[[AbstractResolver], aiohttp.ClientSession]


class PinnedResolver(AbstractResolver):
    """Answers only for hosts this module has already vetted.

    An unpinned host raises rather than resolving, so a redirect that
    slipped past the hop loop cannot reach the network by accident.
    """

    def __init__(self, pins: dict[str, list[IPAddress]]) -> None:
        self._pins = pins

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        addresses = self._pins.get(host.rstrip(".").lower())
        if not addresses:
            raise OSError(f"host not pinned: {host!r}")
        return [
            ResolveResult(
                hostname=host,
                host=str(ip),
                port=port,
                family=socket.AF_INET6 if ip.version == 6 else socket.AF_INET,
                proto=socket.IPPROTO_TCP,
                flags=socket.AI_NUMERICHOST,
            )
            for ip in addresses
        ]

    async def close(self) -> None:
        return None


async def system_resolve(host: str, port: int) -> list[IPAddress]:
    """The default resolver seam: one getaddrinfo, both families."""
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    )
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def open_real_session(
    resolver: AbstractResolver, *, timeout_s: float, user_agent: str
) -> aiohttp.ClientSession:
    """A session that cannot carry state between requests.

    `use_dns_cache=False` matters: with it on, aiohttp would answer a
    later request from its own cache instead of from the pin, which is
    the same mistake as re-resolving.
    """
    connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False, limit=2)
    return aiohttp.ClientSession(
        connector=connector,
        cookie_jar=aiohttp.DummyCookieJar(),
        timeout=aiohttp.ClientTimeout(total=timeout_s),
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html, text/plain;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        },
        auto_decompress=True,
        trust_env=False,
    )


def _content_type(headers) -> tuple[str, str | None]:
    """(lowercased mime type, charset) from a Content-Type header."""
    raw = headers.get("Content-Type") or ""
    parts = [p.strip() for p in raw.split(";")]
    mime = parts[0].lower()
    charset = None
    for part in parts[1:]:
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip().strip('"').lower() or None
    return mime, charset


def _decode(body: bytes, charset: str | None) -> str:
    for candidate in (charset, "utf-8"):
        if not candidate:
            continue
        try:
            return body.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


def normalize_text(text: str) -> str:
    """Collapse every run of whitespace to one space and trim.

    Not cosmetic: plan section 7.1 makes a card's quote prove itself as a
    verbatim substring of this text, so the page's line wrapping must not
    decide whether a true quote is accepted.
    """
    return _WHITESPACE.sub(" ", text).strip()


def extract_main_text(body: bytes, mime: str, charset: str | None) -> tuple[str, str | None]:
    """(main text, title) from a fetched body. Never touches the network.

    trafilatura is used purely as a string transform. Its own
    `fetch_url`/`download` helpers are never imported here, and
    tests/test_research_isolation.py pins that.
    """
    document = _decode(body, charset)
    if mime == "text/plain":
        return normalize_text(document), None
    parsed = trafilatura.bare_extraction(
        document,
        fast=True,
        with_metadata=True,
        include_comments=False,
        include_tables=False,
        include_images=False,
        include_links=False,
    )
    if parsed is None or not parsed.text:
        return "", None
    title = normalize_text(parsed.title) if parsed.title else None
    return normalize_text(parsed.text), title


async def _read_capped(response, max_bytes: int) -> bytes | str:
    """The body, or `too_large` -- aborted mid-stream, not after."""
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        return errors.TOO_LARGE
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(READ_CHUNK):
        total += len(chunk)
        if total > max_bytes:
            return errors.TOO_LARGE
        chunks.append(chunk)
    return b"".join(chunks)


async def _vet_host(target: Target, resolve: Resolver) -> list[IPAddress] | str:
    if target.literal_ip is not None:
        # parse_target already refused a non-public literal.
        return [target.literal_ip]
    try:
        addresses = await resolve(target.host, target.port)
    except (OSError, asyncio.TimeoutError):
        return errors.DNS_ERROR
    return vet_addresses(addresses)


@dataclass(frozen=True)
class RawResponse:
    """A body we were allowed to read, before anything interpreted it."""

    target: Target
    status: int
    mime: str
    charset: str | None
    body: bytes


# Called once per hop, after the address is vetted and before the GET.
# `fetch()` uses it for the robots check; the robots fetch itself passes
# None, which is what stops the two from calling each other forever.
HopGuard = Callable[[Target], Awaitable["str | None"]]


async def _get_guarded(
    raw_url: str,
    *,
    max_bytes: int,
    max_redirects: int,
    allowed_domains: Sequence[str] | None,
    accept_types: Sequence[str] | None,
    resolve: Resolver,
    open_session: SessionOpener,
    hop_guard: HopGuard | None,
) -> RawResponse | FetchFailure:
    """One guarded GET, following redirects by hand.

    Every hop restarts the whole check -- parse, allowlist, resolve, vet,
    pin, guard -- because a redirect is a URL chosen by someone else and
    deserves exactly as much trust as the one the user typed.
    """
    current = raw_url
    last_domain: str | None = None

    for _ in range(max_redirects + 1):
        target = parse_target(current)
        if isinstance(target, str):
            return FetchFailure(error=target, domain=last_domain)
        last_domain = target.host

        if allowed_domains is not None and not domain_matches(target.host, allowed_domains):
            return FetchFailure(error=errors.BLOCKED_DOMAIN, domain=target.host)

        vetted = await _vet_host(target, resolve)
        if isinstance(vetted, str):
            return FetchFailure(error=vetted, domain=target.host)

        if hop_guard is not None:
            refusal = await hop_guard(target)
            if refusal is not None:
                return FetchFailure(error=refusal, domain=target.host)

        try:
            async with open_session(PinnedResolver({target.host: vetted})) as session:
                async with session.get(target.url, allow_redirects=False) as response:
                    status = response.status
                    if status in REDIRECT_STATUSES:
                        location = response.headers.get("Location")
                        if not location:
                            return FetchFailure(
                                error=errors.HTTP_ERROR,
                                domain=target.host,
                                http_status=status,
                            )
                        current = urljoin(target.url, location)
                        continue
                    if not 200 <= status < 300:
                        return FetchFailure(
                            error=errors.HTTP_ERROR, domain=target.host, http_status=status
                        )

                    mime, charset = _content_type(response.headers)
                    if accept_types is not None and mime not in accept_types:
                        return FetchFailure(
                            error=errors.BAD_CONTENT_TYPE,
                            domain=target.host,
                            http_status=status,
                        )
                    body = await _read_capped(response, max_bytes)
                    if isinstance(body, str):
                        return FetchFailure(
                            error=body, domain=target.host, http_status=status
                        )
        except asyncio.TimeoutError:
            return FetchFailure(error=errors.TIMEOUT, domain=target.host)
        except (aiohttp.ClientError, OSError):
            return FetchFailure(error=errors.NETWORK_ERROR, domain=target.host)

        return RawResponse(
            target=target, status=status, mime=mime, charset=charset, body=body
        )

    return FetchFailure(error=errors.TOO_MANY_REDIRECTS, domain=last_domain)


def make_robots_cache(
    *,
    timeout_s: float,
    max_redirects: int,
    user_agent: str,
    resolve: Resolver = system_resolve,
    open_session: SessionOpener | None = None,
) -> RobotsCache:
    """A per-job robots cache whose fetches go through the same guards.

    robots.txt is fetched with `accept_types=None` -- the file is what
    it is, and a site serving it as application/octet-stream is not
    telling us we may crawl it. Redirects are followed (RFC 9309 asks
    for it, and apex-to-www is ordinary), each hop re-vetted like any
    other.
    """
    opener = open_session or _default_opener(timeout_s=timeout_s, user_agent=user_agent)

    async def fetch_robots(url: str) -> tuple[int, str] | str:
        result = await _get_guarded(
            url,
            max_bytes=ROBOTS_MAX_BYTES,
            max_redirects=max_redirects,
            allowed_domains=None,
            accept_types=None,
            resolve=resolve,
            open_session=opener,
            hop_guard=None,
        )
        if isinstance(result, FetchFailure):
            return result.error
        return result.status, _decode(result.body, result.charset)

    return RobotsCache(fetch=fetch_robots, user_agent=user_agent)


def _default_opener(*, timeout_s: float, user_agent: str) -> SessionOpener:
    def opener(resolver: AbstractResolver) -> aiohttp.ClientSession:
        return open_real_session(resolver, timeout_s=timeout_s, user_agent=user_agent)

    return opener


async def fetch(
    raw_url: str,
    *,
    timeout_s: float,
    max_bytes: int,
    max_redirects: int,
    max_chars: int,
    user_agent: str,
    allowed_domains: Sequence[str] | None = None,
    robots: RobotsCache | None = None,
    resolve: Resolver = system_resolve,
    open_session: SessionOpener | None = None,
) -> FetchResult:
    """Fetch one page, or say in one code why we would not.

    `allowed_domains` is the /study packet allowlist, re-checked at every
    hop so a redirect cannot walk us off it (plan section 5, last
    paragraph). /read passes None: any public domain the user names is
    fine, and the address and robots rules still apply to it.

    `robots` is per job, so a job that reads three pages on one host asks
    that host for its rules once. Passing None skips the check and exists
    only for tests that are exercising something else.
    """
    opener = open_session or _default_opener(timeout_s=timeout_s, user_agent=user_agent)

    hop_guard: HopGuard | None = None
    if robots is not None:

        async def hop_guard(target: Target) -> str | None:  # noqa: F811
            return await robots.allows(target.origin, target.url)

    result = await _get_guarded(
        raw_url,
        max_bytes=max_bytes,
        max_redirects=max_redirects,
        allowed_domains=allowed_domains,
        accept_types=TEXT_TYPES,
        resolve=resolve,
        open_session=opener,
        hop_guard=hop_guard,
    )
    if isinstance(result, FetchFailure):
        return result

    text, title = extract_main_text(result.body, result.mime, result.charset)
    if not text:
        return FetchFailure(
            error=errors.EMPTY_EXTRACTION,
            domain=result.target.host,
            http_status=result.status,
        )
    text = text[:max_chars]
    return Clip(
        url=result.target.url,
        domain=result.target.host,
        title=title[:300] if title else None,
        text=text,
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        http_status=result.status,
    )

"""robots.txt, fetched once per host per job (phase-4 plan section 5.5).

Split out from fetch.py because it is the one part of the fetcher that
makes a *second* request in order to decide whether the first is allowed,
and that recursion is easier to read -- and much easier to test -- when
it lives behind an injected callable rather than calling back into the
module that calls it.

Unreachable is not the same as absent. RFC 9309 section 2.3.1 says a 4xx
means "allow all" (the file simply is not there) while a 5xx "may" be
treated as a full disallow. We take the strict reading of both: a site
that cannot tell us its rules does not get crawled by us today. That is
the direction plan section 5.9 points -- when a site says no, in any
dialect, the answer is a reported finding, never a workaround.
"""

from __future__ import annotations

from typing import Awaitable, Callable
from urllib.robotparser import RobotFileParser

from app.research import errors

ROBOTS_PATH = "/robots.txt"
# RFC 9309 section 2.5 sets the floor a crawler must parse at 500 KiB.
# Anything past it is not a robots file, it is a payload.
ROBOTS_MAX_BYTES = 512 * 1024

# What (int status, str body) | error-code a robots fetch hands back.
RobotsFetcher = Callable[[str], Awaitable["tuple[int, str] | str"]]


def product_token(user_agent: str) -> str:
    """The name a robots.txt `User-agent:` line would address us by.

    `AnchorBot/1.0 (personal, ...)` -> `AnchorBot`. RobotFileParser
    lowercases and substring-matches, so handing it the full header with
    its parenthetical would make us match lines nobody wrote.
    """
    return user_agent.strip().split("/", 1)[0].split()[0] if user_agent.strip() else "*"


class RobotsCache:
    """One robots decision per host, for the lifetime of one job.

    Scoped to a job rather than to the process: a long-lived cache would
    keep serving a rule the site has since changed, and a job is short
    enough that re-fetching per job costs one request.
    """

    def __init__(self, *, fetch: RobotsFetcher, user_agent: str) -> None:
        self._fetch = fetch
        self._agent = product_token(user_agent)
        self._decisions: dict[str, RobotFileParser | str] = {}

    async def allows(self, origin: str, url: str) -> str | None:
        """None if we may fetch `url`, else the error code that refused it.

        `origin` is `scheme://host[:port]` -- the cache key, because
        robots.txt is scoped to exactly that triple and not to the
        registrable domain.
        """
        parser = self._decisions.get(origin)
        if parser is None:
            parser = await self._load(origin)
            self._decisions[origin] = parser
        if isinstance(parser, str):
            return parser
        if parser.can_fetch(self._agent, url):
            return None
        return errors.ROBOTS_DISALLOW

    async def _load(self, origin: str) -> RobotFileParser | str:
        result = await self._fetch(origin + ROBOTS_PATH)
        if isinstance(result, str):
            # The robots fetch itself was refused or failed. We do not
            # know the rules, so we do not fetch. A blocked address or a
            # bad scheme keeps its own code; everything else reads as a
            # disallow, which is what it functionally is.
            if result in (errors.BLOCKED_PRIVATE_IP, errors.BLOCKED_SCHEME, errors.DNS_ERROR):
                return result
            return errors.ROBOTS_DISALLOW

        status, body = result
        parser = RobotFileParser()
        if status in (401, 403):
            # "You may not read the rules" reads as "you may not read".
            parser.disallow_all = True
            return parser
        if 400 <= status < 500:
            # No robots file. RFC 9309: allow all.
            parser.allow_all = True
            return parser
        if status >= 500 or not 200 <= status < 300:
            return errors.ROBOTS_DISALLOW
        parser.parse(body.splitlines())
        return parser

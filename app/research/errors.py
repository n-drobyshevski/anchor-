"""The closed set of fetch failure codes (phase-4 plan section 5).

Every way a fetch can fail resolves to one of these short codes. They are
what `study_clip.fetch_error` stores, what a finished job reports to the
user, and the only fetch detail that may ever reach a log line -- plan
section 12: "Logs contain no URL paths, page text, card text, quotes, or
topics. Only IDs, domains, codes, counts and cost."

Codes, not free text, because the alternative is an exception message
from a stranger's web server going straight into our database and our
logs. `error_code` on `study_job` carries the same rule.

The set is closed on purpose: tests assert that every code a fetch can
return is a member, so a new failure path cannot invent an unreviewed
string on its way past the privacy rule.
"""

from __future__ import annotations

# --- refused before any packet left the machine -------------------------
BLOCKED_SCHEME = "blocked_scheme"
"""Not http or https. Excludes file:, ftp:, gopher:, data:, and friends."""

BLOCKED_USERINFO = "blocked_userinfo"
"""The URL carried `user:pass@`. Credentials in a URL are never ours."""

BLOCKED_MALFORMED_URL = "blocked_malformed_url"
"""Unparseable, hostless, bad port, or a host we cannot encode as IDNA."""

BLOCKED_PRIVATE_IP = "blocked_private_ip"
"""The host resolved to (or literally was) a non-public address."""

BLOCKED_DOMAIN = "blocked_domain"
"""Public and reachable, but outside the packet allowlist (/study only)."""

DNS_ERROR = "dns_error"
"""The host does not resolve at all."""

# --- refused by the site's own rules ------------------------------------
ROBOTS_DISALLOW = "robots_disallow"
"""robots.txt tells our User-Agent not to fetch this path. We obey."""

# --- refused by our limits ----------------------------------------------
TOO_MANY_REDIRECTS = "too_many_redirects"
TOO_LARGE = "too_large"
BAD_CONTENT_TYPE = "bad_content_type"
TIMEOUT = "timeout"

# --- the far end said no ------------------------------------------------
HTTP_ERROR = "http_error"
"""Any non-2xx that is not a redirect. Covers the 403/429 a site returns
when it does not want bots; plan section 5.9 forbids working around it."""

NETWORK_ERROR = "network_error"
"""Connection reset, TLS failure, malformed response."""

# --- we got the page and it was useless ---------------------------------
EMPTY_EXTRACTION = "empty_extraction"
"""Fetched and decoded, but no main text survived extraction -- a
JavaScript shell, a CAPTCHA interstitial, or a page that is all chrome."""

FETCH_ERROR_CODES: frozenset[str] = frozenset(
    {
        BLOCKED_SCHEME,
        BLOCKED_USERINFO,
        BLOCKED_MALFORMED_URL,
        BLOCKED_PRIVATE_IP,
        BLOCKED_DOMAIN,
        DNS_ERROR,
        ROBOTS_DISALLOW,
        TOO_MANY_REDIRECTS,
        TOO_LARGE,
        BAD_CONTENT_TYPE,
        TIMEOUT,
        HTTP_ERROR,
        NETWORK_ERROR,
        EMPTY_EXTRACTION,
    }
)

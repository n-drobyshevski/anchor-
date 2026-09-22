"""URL and address admission control (phase-4 plan section 5, steps 1-3).

This module decides what we are willing to talk to. It performs no I/O of
its own -- DNS resolution is passed in -- so the entire policy is unit
testable without a socket, which is the only way a rule like this stays
correct.

**The policy is an allowlist, not a denylist.** An address is acceptable
only if `ipaddress` calls it global, and then only if it survives a short
list of things `is_global` still lets through. Written the other way
round -- reject 10/8, reject 127/8, reject ... -- it would be one missing
range away from being a server-side request forgery, and the missing
range is always the one nobody thought of. Measured against CPython
3.12, `is_global` is already False for every private, loopback,
link-local, reserved, unspecified, documentation and carrier-grade-NAT
address, in both families.

What `is_global` does **not** catch, verified 2026-09-22, and why each
clause below exists:

- `224.0.0.1` and `ff02::1` -- multicast is global.
- `fec0::1` -- deprecated IPv6 site-local is global and is not private.
- `::127.0.0.1` -- an IPv4-compatible IPv6 address is global, and
  `.ipv4_mapped` is None for it, so unwrapping it needs its own branch.
- `64:ff9b::7f00:1` -- the NAT64 well-known prefix wrapping 127.0.0.1 is
  global, and `ipaddress` has no property for it at all.

We reject **every** IPv4-in-IPv6 form outright, including `::ffff:8.8.8.8`
whose embedded address is perfectly public. A real website's AAAA record
is never one of these, so the only thing the unwrap-and-revalidate path
would buy us is a second place that has to stay correct forever. The
redundant property checks are kept alongside `is_global` deliberately:
they cost nothing and they are what plan section 5.2 names by hand.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Iterable, Sequence
from urllib.parse import urlsplit, urlunsplit

from app.research import errors

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

ALLOWED_SCHEMES = ("http", "https")
DEFAULT_PORTS = {"http": 80, "https": 443}

# Named here rather than trusted to `is_global`, because plan section 5.2
# names it and because a property's definition is CPython's to change.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
# RFC 6052's well-known prefix for IPv4/IPv6 translation. `ipaddress`
# offers no property for it.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")


def is_public_address(ip: IPAddress) -> bool:
    """True only for an address we are willing to open a socket to.

    See the module docstring for why this is shaped as an allowlist and
    why each extra clause is here.
    """
    if not ip.is_global:
        return False
    # Redundant against is_global on CPython 3.12, and kept: these are
    # the rejections plan section 5.2 names, and a property that quietly
    # changes meaning should cost us a test failure, not a fetch.
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    if isinstance(ip, ipaddress.IPv4Address):
        return ip not in _CGNAT
    if ip.is_site_local:
        return False
    # Every IPv4-in-IPv6 encoding, public payload or not.
    if ip.ipv4_mapped is not None or ip.sixtofour is not None or ip.teredo is not None:
        return False
    if ip in _NAT64 or ip in _NAT64_LOCAL:
        return False
    # ::a.b.c.d, the deprecated IPv4-compatible form. No property covers
    # it, and ::1 is the member of it that matters.
    if int(ip) < (1 << 32):
        return False
    return True


@dataclass(frozen=True)
class Target:
    """A URL we have decided is syntactically admissible.

    `host` is the IDNA-encoded, lowercased, dot-stripped hostname -- what
    goes in the Host header, what TLS validates the certificate against,
    and what we hand to the resolver. `literal_ip` is set when the URL
    named an address instead of a name, in which case no DNS happens and
    that address is vetted directly.
    """

    url: str
    scheme: str
    host: str
    port: int
    path: str
    literal_ip: IPAddress | None = None

    @property
    def origin(self) -> str:
        """`scheme://host[:port]` -- the scope robots.txt applies to.

        Not the registrable domain: robots.txt binds to exactly this
        triple, so `https://old.reddit.com` and `https://reddit.com` are
        two origins with two files and possibly two answers.
        """
        host = f"[{self.host}]" if ":" in self.host else self.host
        if self.port == DEFAULT_PORTS[self.scheme]:
            return f"{self.scheme}://{host}"
        return f"{self.scheme}://{host}:{self.port}"


def _encode_host(host: str) -> str | None:
    """Lowercase, strip the FQDN dot, and punycode a non-ASCII hostname.

    Uses the stdlib `idna` codec rather than the `idna` package: it is
    IDNA 2003 and therefore stricter than a browser, which for us is the
    safe direction -- a name it refuses is a name we do not fetch.
    """
    host = host.strip().rstrip(".").lower()
    if not host:
        return None
    if host.isascii():
        return host
    try:
        return host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return None


def _is_plausible_hostname(host: str) -> bool:
    """Could this name belong to a page on the public web?

    Two rules, both aimed at the numeric host forms that `ipaddress`
    refuses to parse and that libc's resolver then happily turns into a
    private address anyway:

    - **It must contain a dot.** `localhost`, `metadata` and any other
      single-label internal name are not pages we fetch, and neither are
      `2130706433` or `0x7f000001` -- both of which glibc's getaddrinfo
      resolves to 127.0.0.1.
    - **Its last label must not be numeric or hex-prefixed.** No public
      suffix is a number, so `127.1`, `0177.0.0.1` and `0x7f.0.0.1`
      cannot be anything but an address in disguise.

    Both are belt to `vet_addresses`' braces -- resolving those names
    would produce a private address and be refused a step later -- and
    both are here because being refused for the right reason, before a
    DNS query goes out, is the difference between a rule and a
    coincidence.
    """
    if "." not in host:
        return False
    last = host.rsplit(".", 1)[-1]
    if not last or last.isdigit() or last.lower().startswith("0x"):
        return False
    return True


def parse_target(raw_url: str) -> Target | str:
    """A `Target`, or the error code that refused it (plan section 5.1).

    Returning the code rather than raising keeps every caller on one
    path: whatever comes back that is not a Target is exactly what goes
    into `study_clip.fetch_error`.
    """
    try:
        parts = urlsplit(raw_url.strip())
    except ValueError:
        return errors.BLOCKED_MALFORMED_URL

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return errors.BLOCKED_SCHEME
    scheme = parts.scheme.lower()

    # Checked before anything else about the authority: a URL carrying
    # credentials is one we were not meant to be given.
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return errors.BLOCKED_USERINFO

    try:
        raw_host = parts.hostname
        port = parts.port
    except ValueError:
        # urlsplit defers port validation to .port, which raises on a
        # non-numeric or out-of-range port.
        return errors.BLOCKED_MALFORMED_URL
    if not raw_host:
        return errors.BLOCKED_MALFORMED_URL

    host = _encode_host(raw_host)
    if host is None:
        return errors.BLOCKED_MALFORMED_URL

    if port is None:
        port = DEFAULT_PORTS[scheme]
    if not 1 <= port <= 65535:
        return errors.BLOCKED_MALFORMED_URL

    literal_ip: IPAddress | None = None
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        if not is_public_address(literal_ip):
            return errors.BLOCKED_PRIVATE_IP
    elif not _is_plausible_hostname(host):
        return errors.BLOCKED_MALFORMED_URL

    path = parts.path or "/"
    # The authority is rebuilt rather than passed through, so that the URL
    # we store, log the domain of, and deduplicate on is already
    # normalised: punycode host, lowercased, default port dropped.
    # Fragments go too -- they never reach the server, and keeping them
    # would make the same page look like two.
    netloc = f"[{host}]" if isinstance(literal_ip, ipaddress.IPv6Address) else host
    if port != DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"
    url = urlunsplit((scheme, netloc, parts.path, parts.query, ""))
    return Target(
        url=url, scheme=scheme, host=host, port=port, path=path, literal_ip=literal_ip
    )


def vet_addresses(addresses: Iterable[IPAddress]) -> list[IPAddress] | str:
    """Every address, or the code that refused the whole set.

    All-or-nothing on purpose (plan section 5.2: "reject if **any**
    resolved address is private"). A name that answers with one public
    and one loopback address is a name being used to walk us inside; the
    right response is to refuse the name, not to pick the nice address
    and hope the connector agrees with our choice.
    """
    vetted = list(addresses)
    if not vetted:
        return errors.DNS_ERROR
    if not all(is_public_address(ip) for ip in vetted):
        return errors.BLOCKED_PRIVATE_IP
    return vetted


def domain_matches(host: str, allowed: Sequence[str]) -> bool:
    """Is `host` inside one of the allowlisted domains?

    Exact match or a subdomain, compared on label boundaries: `reddit.com`
    admits `reddit.com` and `old.reddit.com`, and refuses both
    `reddit.com.evil.io` and `notreddit.com`.

    Deliberately not Public-Suffix-List based. A PSL would let us talk
    about registrable domains in general, at the cost of a dependency
    plus a data file that is wrong the moment it goes stale. The
    allowlist here is three to seven domains chosen by hand, so suffix
    matching on label boundaries is both sufficient and strictly the
    safer failure mode: it can only ever refuse too much.
    """
    host = host.strip().rstrip(".").lower()
    if not host:
        return False
    for entry in allowed:
        domain = entry.strip().rstrip(".").lower()
        if not domain:
            continue
        if host == domain or host.endswith("." + domain):
            return True
    return False

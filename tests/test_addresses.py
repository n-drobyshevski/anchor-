"""Address and URL admission control (phase-4 plan sections 5.1-5.2, 14).

The point of this file is coverage of the *ranges*, not of the code path.
A server-side request forgery is one missing prefix, so every family of
address the plan names gets a row here, and every row is asserted
individually rather than folded into a loop with one assert at the end --
when this fails it should name the address that got through.
"""

from __future__ import annotations

import ipaddress

import pytest

from app.research import errors
from app.research.addresses import (
    domain_matches,
    is_public_address,
    parse_target,
    vet_addresses,
)

# Every one of these must be refused. The comment is why it is here and
# not merely why it is private -- several are global by `ipaddress`.
BLOCKED = [
    ("0.0.0.0", "unspecified"),
    ("10.0.0.1", "RFC 1918"),
    ("172.16.0.1", "RFC 1918"),
    ("192.168.1.1", "RFC 1918"),
    ("127.0.0.1", "loopback -- the acceptance checklist's /read target"),
    ("127.1.2.3", "the rest of loopback/8, not just .0.1"),
    ("169.254.169.254", "link-local: the cloud metadata endpoint"),
    ("100.64.0.1", "CGNAT -- is_private is False for this one"),
    ("100.127.255.254", "the far end of CGNAT/10"),
    ("192.0.0.1", "IETF protocol assignments"),
    ("192.0.2.1", "TEST-NET-1"),
    ("198.18.0.1", "benchmarking"),
    ("198.51.100.1", "TEST-NET-2"),
    ("203.0.113.1", "TEST-NET-3"),
    ("224.0.0.1", "multicast -- is_global is True for this one"),
    ("239.255.255.250", "SSDP multicast"),
    ("240.0.0.1", "reserved"),
    ("255.255.255.255", "broadcast"),
    ("::", "unspecified"),
    ("::1", "IPv6 loopback"),
    ("fe80::1", "IPv6 link-local"),
    ("fc00::1", "IPv6 ULA"),
    ("fd12:3456::1", "IPv6 ULA, the half everyone actually uses"),
    ("fec0::1", "deprecated site-local -- is_global is True for this one"),
    ("ff02::1", "IPv6 multicast"),
    ("2001:db8::1", "documentation"),
    ("::ffff:127.0.0.1", "IPv4-mapped loopback"),
    ("::ffff:8.8.8.8", "IPv4-mapped public: rejected as a class, see module doc"),
    ("::127.0.0.1", "IPv4-compatible loopback -- is_global is True"),
    ("64:ff9b::7f00:1", "NAT64 of 127.0.0.1 -- is_global is True"),
    ("64:ff9b:1::7f00:1", "local-use NAT64 prefix"),
    ("2002:7f00:0001::", "6to4 wrapping 127.0.0.1"),
    ("2001:0:4136:e378:8000:63bf:3fff:fdd2", "Teredo"),
]

ALLOWED = ["8.8.8.8", "1.1.1.1", "140.82.121.4", "2606:4700::1111", "2a00:1450:4001:80f::200e"]


@pytest.mark.parametrize("address,reason", BLOCKED, ids=[a for a, _ in BLOCKED])
def test_non_public_addresses_are_refused(address, reason):
    assert is_public_address(ipaddress.ip_address(address)) is False, reason


@pytest.mark.parametrize("address", ALLOWED)
def test_ordinary_public_addresses_are_accepted(address):
    assert is_public_address(ipaddress.ip_address(address)) is True


def test_a_single_private_address_refuses_the_whole_name():
    """Plan section 5.2: reject if *any* resolved address is private.

    A name that answers with one public and one loopback address is a
    name being used to walk us inside. Picking the public one and hoping
    the connector agrees is how this bug gets written.
    """
    mixed = [ipaddress.ip_address("8.8.8.8"), ipaddress.ip_address("127.0.0.1")]
    assert vet_addresses(mixed) == errors.BLOCKED_PRIVATE_IP


def test_all_public_addresses_survive_in_order():
    addresses = [ipaddress.ip_address(a) for a in ("8.8.8.8", "1.1.1.1")]
    assert vet_addresses(addresses) == addresses


def test_a_name_that_does_not_resolve_is_a_dns_error_not_a_block():
    assert vet_addresses([]) == errors.DNS_ERROR


@pytest.mark.parametrize(
    "url,code",
    [
        ("file:///etc/passwd", errors.BLOCKED_SCHEME),
        ("ftp://example.com/x", errors.BLOCKED_SCHEME),
        ("gopher://example.com/", errors.BLOCKED_SCHEME),
        ("data:text/html,<b>hi</b>", errors.BLOCKED_SCHEME),
        ("javascript:alert(1)", errors.BLOCKED_SCHEME),
        ("https://user:pass@example.com/", errors.BLOCKED_USERINFO),
        ("https://user@example.com/", errors.BLOCKED_USERINFO),
        ("https://example.com@evil.io/", errors.BLOCKED_USERINFO),
        ("https:///nohost", errors.BLOCKED_MALFORMED_URL),
        ("https://example.com:0/", errors.BLOCKED_MALFORMED_URL),
        ("https://example.com:99999/", errors.BLOCKED_MALFORMED_URL),
        ("https://example.com:abc/", errors.BLOCKED_MALFORMED_URL),
        ("http://127.0.0.1:5432", errors.BLOCKED_PRIVATE_IP),
        ("http://169.254.169.254/latest/meta-data/", errors.BLOCKED_PRIVATE_IP),
        ("http://[::1]/", errors.BLOCKED_PRIVATE_IP),
        ("http://[::ffff:127.0.0.1]/", errors.BLOCKED_PRIVATE_IP),
        # Numeric host forms ipaddress refuses to parse but glibc's
        # getaddrinfo resolves to 127.0.0.1 anyway.
        ("http://0x7f000001/", errors.BLOCKED_MALFORMED_URL),
        ("http://2130706433/", errors.BLOCKED_MALFORMED_URL),
        ("http://127.1/", errors.BLOCKED_MALFORMED_URL),
        ("http://0177.0.0.1/", errors.BLOCKED_MALFORMED_URL),
        ("http://0x7f.0.0.1/", errors.BLOCKED_MALFORMED_URL),
        ("http://localhost/", errors.BLOCKED_MALFORMED_URL),
        ("http://metadata/", errors.BLOCKED_MALFORMED_URL),
    ],
)
def test_refused_urls(url, code):
    assert parse_target(url) == code


@pytest.mark.parametrize("host", ["example.com", "old.reddit.com", "face.cab", "xn--e1afmkfd.xn--p1ai"])
def test_ordinary_hostnames_survive_the_numeric_host_rule(host):
    """The numeric-host rule must not eat real names, including one
    whose every character is a hex digit (`face.cab`)."""
    assert parse_target(f"https://{host}/x").host == host


def test_an_ip_literal_url_is_vetted_without_dns():
    """`literal_ip` set means no name lookup happens at all."""
    target = parse_target("http://8.8.8.8/x")
    assert target.literal_ip == ipaddress.ip_address("8.8.8.8")
    assert target.host == "8.8.8.8"


def test_the_stored_url_is_normalised():
    """Punycode, lowercased, default port dropped, fragment gone.

    The URL we store is the URL we deduplicate on and the one whose
    domain we log, so two spellings of one page must not look like two.
    """
    target = parse_target("HTTPS://Example.COM:443/A/b?q=1#section")
    assert target.url == "https://example.com/A/b?q=1"
    assert target.host == "example.com"
    assert target.port == 443


def test_a_non_default_port_survives_normalisation():
    target = parse_target("http://example.com:8080/x")
    assert target.url == "http://example.com:8080/x"
    assert target.origin == "http://example.com:8080"


def test_an_internationalised_domain_is_punycoded():
    target = parse_target("https://пример.рф/страница")
    assert target.host == "xn--e1afmkfd.xn--p1ai"
    assert target.url.startswith("https://xn--e1afmkfd.xn--p1ai/")


def test_origin_is_the_robots_scope_not_the_registrable_domain():
    assert parse_target("https://old.reddit.com/r/x").origin == "https://old.reddit.com"
    assert parse_target("https://reddit.com/r/x").origin == "https://reddit.com"


@pytest.mark.parametrize(
    "host,expected",
    [
        ("reddit.com", True),
        ("old.reddit.com", True),
        ("www.old.reddit.com", True),
        ("REDDIT.COM", True),
        ("reddit.com.", True),
        ("reddit.com.evil.io", False),
        ("notreddit.com", False),
        ("xreddit.com", False),
        ("reddit.como", False),
        ("evil.io", False),
        ("com", False),
        ("", False),
    ],
)
def test_domain_allowlist_matches_on_label_boundaries(host, expected):
    assert domain_matches(host, ["reddit.com"]) is expected


def test_an_empty_allowlist_admits_nothing():
    assert domain_matches("reddit.com", []) is False
    assert domain_matches("reddit.com", ["", "  "]) is False

"""app/research/sources.py: source tiers and the reading order.

Pure functions on domains and URLs; no database, no network. The URLs
are synthetic.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.research import sources

SEP = "https://plato.stanford.edu/entries/cybernetics/"
IEP = "https://iep.utm.edu/cybernetics/"
WIKI_EN = "https://en.wikipedia.org/wiki/Cybernetics"
WIKI_EN_2 = "https://en.wikipedia.org/wiki/Requisite_variety"
WIKI_RU = "https://ru.wikipedia.org/wiki/Кибернетика"
ARXIV = "https://arxiv.org/abs/2401.00001"
PHILPAPERS = "https://philpapers.org/rec/EXAMPLE"


@pytest.mark.parametrize(
    ("domain", "tier"),
    [
        ("plato.stanford.edu", sources.REFERENCE),
        ("iep.utm.edu", sources.REFERENCE),
        ("en.wikipedia.org", sources.ENCYCLOPEDIA),
        ("fr.wikipedia.org", sources.ENCYCLOPEDIA),
        ("arxiv.org", sources.PRIMARY),
        ("www.pangaro.com", sources.PRIMARY),
        ("asc-cybernetics.org", sources.PRIMARY),
        ("philpapers.org", sources.INDEX),
        ("reddit.com", sources.OTHER),
        # Label boundaries, as the allowlist matches them.
        ("notwikipedia.org", sources.OTHER),
        ("stanford.edu", sources.OTHER),
    ],
)
def test_tiers(domain, tier):
    assert sources.tier_of(domain) is tier


def test_every_lens_packet_domain_has_a_named_tier():
    """A domain added to the shipped packet without a tier would read as
    `other`, unlabelled: name it here when the packet grows."""
    for domain in Settings(_env_file=None).PACKET_LENS:
        assert sources.tier_of(domain) is not sources.OTHER, domain


def test_only_other_goes_unlabelled():
    for _entry, tier in sources.TIERS:
        assert tier.label


@pytest.mark.parametrize(
    ("domain", "owner"),
    [
        ("en.wikipedia.org", "wikipedia.org"),
        ("ru.wikipedia.org", "wikipedia.org"),
        ("plato.stanford.edu", "stanford.edu"),
        ("arxiv.org", "arxiv.org"),
        ("WWW.Pangaro.com.", "pangaro.com"),
    ],
)
def test_publisher(domain, owner):
    assert sources.publisher(domain) == owner


def test_two_pages_of_one_encyclopedia_no_longer_take_both_reads():
    """The production case: the provider ranked two Wikipedia pages first.
    The second publisher is read second, and the better tier first."""
    order = sources.reading_order([WIKI_EN, WIKI_EN_2, SEP])
    assert order == [SEP, WIKI_EN, WIKI_EN_2]


def test_every_publisher_once_before_any_twice():
    order = sources.reading_order([WIKI_EN, WIKI_RU, ARXIV, PHILPAPERS, IEP])
    assert order == [IEP, WIKI_EN, ARXIV, PHILPAPERS, WIKI_RU]


def test_one_publisher_keeps_the_providers_order():
    """/study ref is three Wikipedias: one publisher, so nothing moves."""
    assert sources.reading_order([WIKI_RU, WIKI_EN, WIKI_EN_2]) == [WIKI_RU, WIKI_EN, WIKI_EN_2]


def test_the_provider_still_orders_within_a_tier():
    assert sources.reading_order([IEP, SEP]) == [IEP, SEP]
    assert sources.reading_order([ARXIV, "https://pangaro.com/x"]) == [ARXIV, "https://pangaro.com/x"]


def test_nothing_is_added_or_dropped():
    urls = [PHILPAPERS, WIKI_EN, SEP, WIKI_EN_2, ARXIV, IEP]
    assert sorted(sources.reading_order(urls)) == sorted(urls)
    assert sources.reading_order([]) == []

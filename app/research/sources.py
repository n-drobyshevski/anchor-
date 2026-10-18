"""Which pages a research reads first, and what kind of source each is.

Two rules on top of the packet allowlist, both pure and both by code:
nothing here asks a model, and nothing here widens what may be read.

**Tiers.** A packet's domains are not equally good evidence. Wikipedia's
own reliable-sources policy separates a peer-reviewed reference work
from a crowd-edited tertiary summary, from a primary text (a preprint,
an author's own site), from an index page that holds an abstract at
best. `TIERS` names that order for the domains the packets ship with;
a domain it does not name is `OTHER`, ranked with the primary sources
and shown without a label. The tier is derived from the clip's domain
whenever it is needed, never stored, so retiering a domain changes
every card's label at once.

**Diversity.** The search provider ranks for relevance, and a relevance
ranking lets one site take every slot: a lens research once read two
Wikipedia pages and nothing else. `reading_order` reorders the
candidates so the pages read first come from different publishers
(maximal marginal relevance at its simplest, with "same publisher" as
the only redundancy it knows): every publisher's first page, best tier
first and provider order within a tier, then every publisher's second
page, and so on. The provider still decides which pages of one
publisher come first.

**Publisher** is the host's last two labels (`ru.wikipedia.org` and
`en.wikipedia.org` are one publisher, `plato.stanford.edu` is
`stanford.edu`). Not a Public Suffix List, for the reason
app/research/addresses.py's `domain_matches` gives: the packets are a
dozen hand-picked domains, and two labels is right for every one.
Cross-checking (app/research/crosscheck.py) counts a second source only
when its publisher differs, so two pages of one encyclopedia never pass
for independent confirmation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.research.addresses import domain_matches


@dataclass(frozen=True)
class Tier:
    """A kind of source: its rank (lower reads first) and the label the
    result message and the inbox note show beside its domain ("" shows
    none)."""

    code: str
    rank: int
    label: str


REFERENCE = Tier("reference", 0, "рецензируемая энциклопедия")
ENCYCLOPEDIA = Tier("encyclopedia", 1, "Википедия")
PRIMARY = Tier("primary", 2, "первоисточник")
OTHER = Tier("other", 2, "")
INDEX = Tier("index", 3, "каталог")

# The packets' domains (app/config.py's PACKET_*), each matched on label
# boundaries like the allowlist itself. First match wins.
TIERS: tuple[tuple[str, Tier], ...] = (
    ("plato.stanford.edu", REFERENCE),
    ("iep.utm.edu", REFERENCE),
    ("wikipedia.org", ENCYCLOPEDIA),
    ("arxiv.org", PRIMARY),
    ("pangaro.com", PRIMARY),
    ("asc-cybernetics.org", PRIMARY),
    ("philpapers.org", INDEX),
)


def tier_of(domain: str) -> Tier:
    """The tier of a clip's domain, `OTHER` when `TIERS` does not name it."""
    for entry, tier in TIERS:
        if domain_matches(domain, (entry,)):
            return tier
    return OTHER


def publisher(domain: str) -> str:
    """The host's last two labels, lowercased (module docstring)."""
    labels = [label for label in domain.strip().rstrip(".").lower().split(".") if label]
    return ".".join(labels[-2:])


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def reading_order(urls: Sequence[str]) -> list[str]:
    """`urls` (provider order, already admitted and deduped by
    app/research/search.py) reordered for reading (module docstring):
    round by round, one page per publisher a round, best tier first and
    provider order within a tier. The same URLs, none added or dropped."""
    seen: dict[str, int] = {}
    keyed = []
    for position, url in enumerate(urls):
        host = _host(url)
        owner = publisher(host)
        round_number = seen.get(owner, 0)
        seen[owner] = round_number + 1
        keyed.append(((round_number, tier_of(host).rank, position), url))
    return [url for _key, url in sorted(keyed)]


__all__ = [
    "ENCYCLOPEDIA",
    "INDEX",
    "OTHER",
    "PRIMARY",
    "REFERENCE",
    "TIERS",
    "Tier",
    "publisher",
    "reading_order",
    "tier_of",
]

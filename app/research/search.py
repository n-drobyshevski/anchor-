"""URL discovery, and nothing else (phase-4 plan sections 2 and 6).

**This module exists to throw almost everything away.** It asks a model
with the `web` plugin attached to look for pages on a topic, and then
keeps exactly one thing from the answer: the list of URLs in the
`url_citation` annotations. The model's prose is discarded. The search
engine's page excerpts are discarded. The titles are kept only to show
the user later, never to decide anything.

That is the whole design. Everything a search engine and a model
produced about those pages is text chosen by strangers, and this bot
reads pages with its own fetcher (`app/research/fetch.py`) under its
own rules. Letting a provider's summary stand in for that would put
unvetted page text one step from a prompt, which is the failure mode
the entire phase is built to avoid.

**This is the only module in the tree that may ask for a web search.**
`tests/test_web_search_isolation.py` names it and nothing else, and
`app/llm/openrouter.py` is the only module that may build the request.
Neither rule is a style preference: a second search call site is a
second place where the user's words leave for a third party, and the
point of the research loop is that there is exactly one, with a quota
on it.

**The provider's domain filter is a request; the allowlist is a rule.**
`include_domains` is sent because it makes the results better, and its
answer is re-filtered in code regardless (plan section 2: "code
**always** post-filters to the packet allowlist"). A provider that
ignores the hint, changes its semantics, or falls back to another
engine changes nothing about what we will fetch.

No database access here. The "already clipped recently" set is passed
in, so the whole module is exercisable against a scripted provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Collection, Sequence

from app.llm.provider import Citation, LLMMessage, LLMProvider, LLMResponse, WebSearch
from app.research.addresses import Target, domain_matches, parse_target

# Plan section 6 caps this at 5. Named here rather than taken from
# config because it is a property of the request we are willing to
# make, not a knob: a larger page of results costs more input tokens
# for candidates we would never get to fetch (RESEARCH_MAX_PINS is 2).
MAX_RESULTS = 5

# Failure codes, the same closed-set convention as app/research/errors.py.
NO_RESULTS = "no_results"

# Plan section 6, verbatim.
PROMPT_WITH_SITES = "Найди страницы по теме «{topic}» на сайтах: {domains}."
# The single reformulation, topic only. Plan section 6: "reformulate
# once (topic only, no site hint)". The allowlist still applies in code
# afterwards -- dropping the hint widens what the provider looks at,
# never what we are willing to read.
PROMPT_TOPIC_ONLY = "Найди страницы по теме «{topic}»."


@dataclass(frozen=True)
class SearchOutcome:
    """What one round of discovery produced.

    `responses` is carried out so the caller can ledger every call it
    paid for -- including the ones that returned nothing usable, which
    are exactly the calls a naive accounting would forget.
    """

    urls: tuple[str, ...] = ()
    error_code: str | None = None
    responses: tuple[LLMResponse, ...] = field(default=())

    @property
    def calls(self) -> int:
        return len(self.responses)


def build_prompt(topic: str, domains: Sequence[str] | None) -> str:
    if domains:
        return PROMPT_WITH_SITES.format(topic=topic, domains=", ".join(domains))
    return PROMPT_TOPIC_ONLY.format(topic=topic)


def _admissible(citation: Citation, allowed_domains: Sequence[str]) -> Target | None:
    """The normalised target for a citation we would be willing to fetch.

    Four rules from plan section 6, applied to a URL a model handed us:

    1. `parse_target` -- http(s) only, no userinfo, a plausible public
       hostname. Shared with the fetcher, so "what counts as a URL" has
       one definition rather than two.
    2. The packet allowlist, matched on label boundaries, so
       `reddit.com.evil.io` is refused however it was ranked. An empty
       allowlist admits nothing rather than everything.
    3. Normalisation, so two spellings of one page dedupe as one.

    The fourth -- "not clipped recently" -- needs the database and is
    applied by the caller in `filter_citations`.
    """
    target = parse_target(citation.url)
    if isinstance(target, str):
        return None
    # Fails closed. An empty allowlist means a packet with nothing in it,
    # and the one thing it must never mean is "no restriction" -- that
    # would turn a misconfigured packet into a search of the open web,
    # silently, at exactly the moment nobody is watching.
    # `app/research/jobs.py` refuses such a job before reaching here;
    # this is the second line.
    if not allowed_domains or not domain_matches(target.host, allowed_domains):
        return None
    return target


def filter_citations(
    citations: Sequence[Citation],
    *,
    allowed_domains: Sequence[str],
    recent_urls: Collection[str] = (),
) -> list[str]:
    """Provider order in, our order out -- minus everything inadmissible.

    Plan section 6: "Ranking follows the provider's order." So this
    filters and dedupes without reordering: the provider is better at
    relevance than we are, and it is only trusted for relevance.

    `recent_urls` is the set of URLs already clipped in the last 30
    days, normalised the same way. Re-reading a page we read last week
    spends a fetch and a distill to produce cards the user has already
    seen and decided about.
    """
    kept: list[str] = []
    seen: set[str] = set()
    for citation in citations:
        target = _admissible(citation, allowed_domains)
        if target is None:
            continue
        if target.url in seen or target.url in recent_urls:
            continue
        seen.add(target.url)
        kept.append(target.url)
    return kept


async def find_urls(
    provider: LLMProvider,
    *,
    topic: str,
    allowed_domains: Sequence[str],
    recent_urls: Collection[str] = (),
    job_id: int,
    max_calls: int,
) -> SearchOutcome:
    """Up to two searches for pages on `topic`, then the surviving URLs.

    The first asks for the packet's sites by name. If nothing survives
    the code filter -- the provider found nothing, or found only pages
    outside the allowlist -- it reformulates once without the site hint
    and filters again (plan section 6). Still nothing is
    `failed:no_results`.

    `max_calls` is what is left of `RESEARCH_MAX_SEARCHES` for this
    job, so a job that has already searched cannot get two more tries
    by asking again.

    Every call made is returned in `responses`, whether or not it
    produced a usable URL. A search that found nothing still cost a
    plugin fee and a prompt.
    """
    responses: list[LLMResponse] = []
    attempts = [list(allowed_domains), []]

    for attempt, domains in enumerate(attempts):
        if len(responses) >= max_calls:
            break
        response = await provider.complete(
            [LLMMessage(role="user", content=build_prompt(topic, domains))],
            conversation_id=f"anchor-search-{job_id}-{attempt}",
            web_search=WebSearch(
                max_results=MAX_RESULTS,
                # The hint, on the first attempt only. The rule still
                # applies to both (see filter_citations).
                include_domains=tuple(domains),
            ),
        )
        responses.append(response)
        urls = filter_citations(
            response.citations,
            allowed_domains=allowed_domains,
            recent_urls=recent_urls,
        )
        if urls:
            return SearchOutcome(urls=tuple(urls), responses=tuple(responses))

    return SearchOutcome(error_code=NO_RESULTS, responses=tuple(responses))

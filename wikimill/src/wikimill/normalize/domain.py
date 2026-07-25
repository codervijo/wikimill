"""Registrable-domain extraction via the Public Suffix List.

The PSL is the only defensible definition of "a domain you could own": naive
last-two-labels splitting gets `news.bbc.co.uk` wrong (`co.uk` is not
registrable) and would put nonsense in an acquisition export.

**The suffix list is never fetched at runtime.** `suffix_list_urls=()` pins
tldextract to its bundled snapshot, which keeps ingestion deterministic and
keeps a crawler from making a surprise network call during what is supposed to
be a purely local stage. Refreshing it is a dependency bump, visible in review.

`is_private_suffix` reports whether the host sits under a PSL *private-section*
suffix. That is deliberately a **fact, not a verdict**. The private section
contains user-content platforms (`blogspot.com`, `github.io`) where a subdomain
is never acquireable — but also regional and institutional registries
(`poznan.pl`, `org.ru`, `ras.ru`, all seen in real enwiki data) where one may
well be. The PSL cannot tell those apart, so this module records the signal and
leaves the judgement to scoring (v1.I). An earlier version called this
`is_user_content_suffix` and hard-excluded such domains from candidacy, which
would have silently dropped real finds.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

# Wikimedia's own projects are not "external" for this tool's purpose.
WIKIMEDIA_SUFFIXES = (
    "wikipedia.org", "wikimedia.org", "wiktionary.org", "wikiquote.org",
    "wikibooks.org", "wikisource.org", "wikinews.org", "wikiversity.org",
    "wikidata.org", "wikivoyage.org", "mediawiki.org", "wikimediafoundation.org",
    "toolforge.org", "wmflabs.org", "wmcloud.org", "wikimedia.de",
)

# Permanently-live identifier infrastructure: never acquireable, and left
# unfiltered they would dominate the queue. Deliberately a short, defensible
# list — hosts that resolve identifiers and nothing else. Big sites that merely
# *contain* identifiers (ncbi.nlm.nih.gov, arxiv.org) are real content and are
# NOT listed: over-flagging would silently drop genuine candidates.
RESOLVER_DOMAINS = (
    "doi.org", "handle.net", "purl.org", "n2t.net", "identifiers.org",
    "worldcat.org", "isni.org", "viaf.org", "openurl.ac.uk",
)


@dataclass(frozen=True)
class DomainInfo:
    host: str
    registrable_domain: str
    public_suffix: str
    is_ip: bool
    is_private_suffix: bool
    is_wikimedia: bool
    is_resolver: bool

    @property
    def acquireable_candidate(self) -> bool:
        """Whether this host could *ever* be an acquisition candidate.

        Only unambiguous exclusions: a bare IP has no domain to buy, and
        Wikimedia and identifier-resolver hosts are permanent infrastructure.

        `is_private_suffix` deliberately does NOT exclude. It cannot distinguish
        `foo.blogspot.com` (never acquireable) from `wbc.poznan.pl` (quite
        possibly acquireable), so it travels to scoring and to the export as a
        visible flag rather than silently removing a candidate.
        """
        return not (self.is_ip or self.is_wikimedia or self.is_resolver)


@lru_cache(maxsize=1)
def _extractor():
    try:
        import tldextract
    except ImportError as exc:  # pragma: no cover - declared dependency
        from ..errors import ConfigError

        raise ConfigError(
            f"tldextract is not installed ({exc}).",
            remediation="Run `uv sync`, or rebuild the image if pyproject changed.",
        ) from exc
    return tldextract.TLDExtract(
        suffix_list_urls=(),          # never fetch — bundled snapshot only
        fallback_to_snapshot=True,
        include_psl_private_domains=True,
    )


def _endswith_domain(host: str, domains: tuple[str, ...]) -> bool:
    """True when host equals one of `domains` or is a subdomain of one.

    Suffix-matching on the *label boundary*, so `notdoi.org` does not match
    `doi.org`.
    """
    for domain in domains:
        if host == domain or host.endswith("." + domain):
            return True
    return False


@lru_cache(maxsize=100_000)
def analyse(host: str) -> DomainInfo:
    """Classify a hostname. Cached — ingest sees the same hosts constantly."""
    lowered = host.lower().rstrip(".")
    result = _extractor()(lowered)
    is_ip = bool(result.ipv4 or getattr(result, "ipv6", ""))
    registrable = "" if is_ip else result.top_domain_under_public_suffix
    return DomainInfo(
        host=lowered,
        registrable_domain=registrable,
        public_suffix=result.suffix,
        is_ip=is_ip,
        is_private_suffix=bool(getattr(result, "is_private", False)),
        is_wikimedia=_endswith_domain(lowered, WIKIMEDIA_SUFFIXES),
        is_resolver=_endswith_domain(lowered, RESOLVER_DOMAINS),
    )

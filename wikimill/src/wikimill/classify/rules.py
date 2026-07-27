"""The classifier: one stored observation in, one verdict out.

**A pure function.** It takes an `Observation` — reconstructed either from a
fresh fetch or from a `url_checks` row read back months later — and returns a
`Verdict`. No network, no database, no clock. That is what makes an improved
classifier able to re-judge history offline, with no refetching and no extra
load on anyone's server (architecture.md §2).

Rule ordering is by *confidence*, not convenience: transport-level facts first
(a DNS failure is not a matter of opinion), then status codes, then content
heuristics, which are the only guesswork here.

**`unregistered` is unreachable from this module, by construction.** It requires
two resolvers agreeing plus RDAP confirmation (v1.G). A false "available domain"
is the most expensive error this tool can make, so there is deliberately no code
path from an HTTP observation to that verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..constants import CLASSIFIER_VERSION, UrlState
from . import signals


@dataclass(frozen=True)
class Observation:
    """What a single check saw. Constructible from a FetchResult or a DB row."""

    url: str
    http_status: int | None = None
    final_url: str | None = None
    redirect_count: int = 0
    cross_domain_redirect: bool = False
    content_type: str | None = None
    content_length: int | None = None
    page_title: str | None = None
    evidence: str | None = None
    error_kind: str | None = None
    robots_decision: str | None = None
    fetched: bool = True

    @classmethod
    def from_row(cls, row) -> Observation:
        return cls(
            url=row["url_normalized"] if "url_normalized" in row.keys() else "",
            http_status=row["http_status"],
            final_url=row["final_url"],
            redirect_count=row["redirect_count"] or 0,
            cross_domain_redirect=bool(row["cross_domain_redirect"]),
            content_type=row["content_type"],
            content_length=row["content_length"],
            page_title=row["page_title"],
            evidence=row["evidence_blob"],
            error_kind=row["error_kind"],
            robots_decision=row["robots_decision"],
            fetched=row["http_status"] is not None or row["error_kind"] is not None,
        )

    @classmethod
    def from_result(cls, result, url: str, robots_decision: str | None = None) -> Observation:
        return cls(
            url=url,
            http_status=result.http_status,
            final_url=result.final_url,
            redirect_count=result.redirect_count,
            cross_domain_redirect=False,
            content_type=result.content_type,
            content_length=result.content_length,
            page_title=result.page_title,
            evidence=result.evidence_blob,
            error_kind=result.error_kind,
            robots_decision=robots_decision,
        )


@dataclass
class Verdict:
    classification: str
    reasons: list[str] = field(default_factory=list)
    confidence: float = 1.0
    version: int = CLASSIFIER_VERSION


# Transport error kinds (from crawl/fetcher.py) mapped to their classification.
# Sub-kinds are preserved as reasons: "the cert expired" and "the host is gone"
# lead to opposite conclusions about a domain.
_DNS_KINDS = {"dns_nxdomain", "dns_error"}
_TLS_KINDS = {
    "tls_cert_expired",
    "tls_hostname_mismatch",
    "tls_chain_untrusted",
    "tls_error",
}
_TRANSIENT_KINDS = {
    "connect_timeout",
    "read_timeout",
    "pool_timeout",
    "timeout",
    "connect_error",
    "read_error",
    "protocol_error",
    "connection_refused",
}
_PERMANENT_KINDS = {
    "redirect_loop",
    "too_many_redirects",
    "no_host",
    "unsupported_protocol",
    "blocked_address",
}


def classify(obs: Observation, policy=None) -> Verdict:
    """Judge one observation. Pure — same input, same verdict, forever."""
    # 0. Never fetched.
    if obs.robots_decision and "disallow" in obs.robots_decision.lower():
        return Verdict(UrlState.BLOCKED_BY_ROBOTS, [obs.robots_decision], 1.0)
    if not obs.fetched:
        return Verdict(UrlState.UNCLASSIFIED, ["no observation recorded"], 0.0)

    # 1. Transport-level facts — not matters of opinion.
    kind = obs.error_kind
    if kind:
        base = kind.split(":")[0]
        if base in _DNS_KINDS:
            return Verdict(UrlState.DNS_FAILURE, [f"error_kind={kind}"], 1.0)
        if base in _TLS_KINDS:
            return Verdict(UrlState.TLS_FAILURE, [f"error_kind={kind}"], 1.0)
        if base in _TRANSIENT_KINDS:
            return Verdict(
                UrlState.TEMPORARILY_UNAVAILABLE, [f"error_kind={kind}"], 0.9
            )
        if base in _PERMANENT_KINDS:
            return Verdict(UrlState.UNCLASSIFIED, [f"error_kind={kind}"], 0.5)
        return Verdict(UrlState.UNCLASSIFIED, [f"unmapped error_kind={kind}"], 0.3)

    status = obs.http_status
    if status is None:
        return Verdict(UrlState.UNCLASSIFIED, ["no status and no error"], 0.0)

    # 2. Status codes.
    if status in (404, 410):
        return Verdict(UrlState.HARD_404, [f"HTTP {status}"], 1.0)
    if status == 429 or status >= 500:
        return Verdict(UrlState.TEMPORARILY_UNAVAILABLE, [f"HTTP {status}"], 1.0)

    if 200 <= status < 300:
        return _classify_content(obs, policy)

    if 400 <= status < 500:
        # 401/403/451 and friends: the page is not accessible to us, but the
        # host answered — which is the question this tool is actually asking.
        # Recording it as `live` with the status in the reasons is more honest
        # than implying the domain is dead.
        return Verdict(
            UrlState.LIVE,
            [f"HTTP {status} — access restricted, host is alive"],
            0.8,
        )

    return Verdict(UrlState.UNCLASSIFIED, [f"unhandled HTTP {status}"], 0.2)


def _classify_content(obs: Observation, policy=None) -> Verdict:
    """2xx: the only place guesswork lives. Ordered most- to least-valuable."""
    title, body = obs.page_title, obs.evidence
    reasons: list[str] = []

    strong_park, weak_park = signals.parking_signals(title, body, policy)
    sale = signals.for_sale_signals(title, body, policy)

    # for_sale = parked AND an explicit sale offer. The two together are the
    # highest-value verdict the crawler alone can produce.
    if sale and (strong_park or len(sale) >= 2):
        reasons = [f"sale:{s}" for s in sale] + [f"parking:{p}" for p in strong_park]
        return Verdict(UrlState.FOR_SALE, reasons, 0.9)

    if strong_park:
        reasons = [f"parking:{p}" for p in strong_park]
        if weak_park:
            reasons += [f"weak:{w}" for w in weak_park]
        return Verdict(UrlState.PARKED, reasons, 0.85)

    # A sale offer with no parking provider signature — real but less certain.
    if sale:
        return Verdict(UrlState.FOR_SALE, [f"sale:{s}" for s in sale], 0.6)

    verdict = _soft_404(obs, policy)
    if verdict is not None:
        return verdict

    if obs.redirect_count:
        reasons = [f"{obs.redirect_count} redirect(s) to {obs.final_url}"]
        if obs.cross_domain_redirect:
            # The interesting case: the domain itself may have changed hands.
            reasons.append("cross-domain — possible handover")
        return Verdict(UrlState.REDIRECT, reasons, 0.95)

    return Verdict(UrlState.LIVE, [f"HTTP {obs.http_status}"], 0.9)


def _soft_404(obs: Observation, policy=None) -> Verdict | None:
    """A 200 that is really a not-found page.

    Scored and corroborated rather than fired on a single match: marking a live
    site dead is the error the operator would act on, so one weak signal is
    never enough.
    """
    title_hits, body_hits = signals.soft_404_signals(obs.page_title, obs.evidence, policy)
    score = 0
    reasons: list[str] = []

    if title_hits:
        score += 2
        reasons += [f"title:{h}" for h in title_hits]
    if body_hits:
        score += 2
        reasons += [f"body:{h}" for h in body_hits]

    # A deep path that lands on the site root is the classic soft 404.
    if obs.redirect_count and obs.final_url:
        original_path = urlsplit(obs.url).path.strip("/")
        final_path = urlsplit(obs.final_url).path.strip("/")
        if original_path and not final_path:
            score += 1
            reasons.append("deep path redirected to site root")

    thin = policy.classify.thin_body_bytes if policy else signals.THIN_BODY_BYTES
    if (obs.content_length or 0) < thin:
        score += 1
        reasons.append(f"thin body ({obs.content_length}b)")

    if score >= 2:
        return Verdict(UrlState.SOFT_404, reasons, min(0.5 + 0.15 * score, 0.9))
    return None

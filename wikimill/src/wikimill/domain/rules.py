"""Domain classification — the only place `unregistered` can be established.

A pure function over a DNS result and an RDAP result, mirroring the URL
classifier (v1.F): same shape, same versioning, same offline re-judgeability.

**The gate on `unregistered` is deliberately hard to pass:** at least two
independent resolvers must return NXDOMAIN *and* the authoritative registry must
return an explicit 404. Either alone is insufficient. A false "available domain"
is the most expensive error this tool can make — the operator would act on it,
try to buy something that is not for sale, and lose trust in every other row.

`no_rdap_for_tld` is a first-class outcome, not a failure. Measured against the
live IANA bootstrap (2026-07-25), `.de`, `.es`, `.io` and `.ru` publish no RDAP
service at all, so domains under them can never be *confirmed* unregistered.
Reporting that honestly is the whole point: an unverifiable domain is recorded
as unverifiable, never optimistically as available.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..constants import CLASSIFIER_VERSION, DomainState
from .dns import DnsResult, DnsStatus
from .rdap import RdapResult, RdapStatus, expires_within

# How far ahead an expiry date counts as "expiring".
EXPIRY_WINDOW_DAYS = 60


@dataclass
class DomainVerdict:
    state: str
    reasons: list[str] = field(default_factory=list)
    confidence: float = 1.0
    version: int = CLASSIFIER_VERSION


def classify(
    dns: DnsResult,
    rdap: RdapResult,
    *,
    url_states: dict[str, int] | None = None,
    expiry_window_days: int = EXPIRY_WINDOW_DAYS,
) -> DomainVerdict:
    """Judge one domain from its DNS and RDAP evidence.

    `url_states` is the tally of URL-level classifications on this domain, used
    only to lift `parked` / `for_sale` up to the domain — the crawler already
    saw the parking page, so re-deriving it here would be duplicated guesswork.
    """
    reasons: list[str] = []

    # -- the unregistered gate --------------------------------------------
    if dns.status is DnsStatus.NXDOMAIN and dns.confirmed_nxdomain:
        if rdap.status is RdapStatus.NOT_FOUND:
            return DomainVerdict(
                DomainState.UNREGISTERED,
                [
                    f"NXDOMAIN confirmed by {sum(1 for a in dns.answers if a.status is DnsStatus.NXDOMAIN)} resolvers",
                    "RDAP: no such domain",
                ],
                1.0,
            )
        if rdap.status is RdapStatus.NO_RDAP_FOR_TLD:
            # DNS says gone, but nothing authoritative can confirm it. Saying
            # "available" here would be a guess dressed as a fact.
            return DomainVerdict(
                DomainState.NO_RDAP_FOR_TLD,
                [
                    "NXDOMAIN confirmed by DNS",
                    "this TLD publishes no RDAP service — cannot confirm availability",
                ],
                0.5,
            )
        if rdap.status is RdapStatus.UNAVAILABLE:
            return DomainVerdict(
                DomainState.UNKNOWN,
                ["NXDOMAIN confirmed by DNS", f"RDAP unavailable ({rdap.error})"],
                0.4,
            )
        # RDAP says registered while DNS says gone: an undelegated but very much
        # owned domain. Common during transfers and after a registrar hold.
        reasons.append("NXDOMAIN, but RDAP reports it registered — not delegated")

    if dns.status is DnsStatus.NXDOMAIN and not dns.confirmed_nxdomain:
        return DomainVerdict(
            DomainState.UNKNOWN,
            ["a single resolver returned NXDOMAIN — not corroborated"],
            0.3,
        )

    # -- registered domains ------------------------------------------------
    if rdap.status is RdapStatus.REGISTERED:
        if rdap.expiring:
            return DomainVerdict(
                DomainState.EXPIRING,
                reasons + [f"EPP status: {', '.join(rdap.statuses)}"],
                0.95,
            )
        if expires_within(rdap.expiry, expiry_window_days):
            return DomainVerdict(
                DomainState.EXPIRING,
                reasons + [f"expires {rdap.expiry} (within {expiry_window_days}d)"],
                0.9,
            )

    # -- lift URL-level parking up to the domain ---------------------------
    lifted = _from_url_states(url_states or {})
    if lifted is not None:
        state, why = lifted
        return DomainVerdict(state, reasons + why, 0.85)

    if rdap.status is RdapStatus.REGISTERED:
        detail = f"registered{f' via {rdap.registrar}' if rdap.registrar else ''}"
        return DomainVerdict(DomainState.ACTIVE, reasons + [detail], 0.95)

    if rdap.status is RdapStatus.NO_RDAP_FOR_TLD:
        if dns.status in (DnsStatus.OK, DnsStatus.NO_RECORDS):
            return DomainVerdict(
                DomainState.ACTIVE, reasons + ["resolves; TLD has no RDAP"], 0.7
            )
        return DomainVerdict(
            DomainState.NO_RDAP_FOR_TLD, reasons + ["TLD publishes no RDAP"], 0.5
        )

    if dns.status in (DnsStatus.OK, DnsStatus.NO_RECORDS):
        return DomainVerdict(
            DomainState.ACTIVE,
            reasons + [f"resolves; RDAP {rdap.status}"],
            0.6,
        )

    return DomainVerdict(
        DomainState.UNKNOWN,
        reasons + [f"dns={dns.status} rdap={rdap.status}"],
        0.2,
    )


def _from_url_states(tally: dict[str, int]) -> tuple[str, list[str]] | None:
    """Promote a parking verdict the crawler already reached at URL level.

    Requires a majority, so one parked URL on an otherwise healthy site does not
    condemn the whole domain.
    """
    total = sum(tally.values())
    if not total:
        return None
    for state in (DomainState.FOR_SALE, DomainState.PARKED):
        count = tally.get(state, 0)
        if count and count * 2 >= total:
            return state, [f"{count}/{total} of this domain's URLs classified {state}"]
    return None

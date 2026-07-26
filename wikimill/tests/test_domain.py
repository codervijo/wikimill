"""Domain checks: multi-resolver DNS, RDAP bootstrap, and the unregistered gate.

Hermetic — DNS goes through a fake query function, RDAP through a fake fetch.
The gate on `unregistered` gets the most attention here, because a false
"available domain" is the error the operator would act on and lose money to.
"""

from __future__ import annotations

import json

import pytest

from wikimill.constants import DomainState
from wikimill.domain.dns import DnsResult, DnsStatus, ResolverAnswer, lookup
from wikimill.domain.rdap import (
    Bootstrap,
    RdapResult,
    RdapStatus,
    expires_within,
    load_bootstrap,
    query,
)
from wikimill.domain.rules import classify

RESOLVERS = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]


def fake_dns(per_resolver: dict[str, ResolverAnswer]):
    def _query(ip, name, timeout):
        return per_resolver[ip]

    return _query


def nx(ip):
    return ResolverAnswer(resolver=ip, status=DnsStatus.NXDOMAIN)


def ok(ip, ns=("ns1.example.net",)):
    return ResolverAnswer(resolver=ip, status=DnsStatus.OK, ns_records=list(ns))


# -- multi-resolver reconciliation -----------------------------------------


def test_all_resolvers_nxdomain_is_confirmed():
    result = lookup("gone.example", RESOLVERS,
                    query=fake_dns({ip: nx(ip) for ip in RESOLVERS}))
    assert result.status is DnsStatus.NXDOMAIN
    assert result.confirmed_nxdomain
    assert result.resolvers_agreed


def test_two_of_three_nxdomain_is_still_confirmed():
    answers = {RESOLVERS[0]: nx(RESOLVERS[0]), RESOLVERS[1]: nx(RESOLVERS[1]),
               RESOLVERS[2]: ResolverAnswer(RESOLVERS[2], DnsStatus.TIMEOUT)}
    result = lookup("gone.example", RESOLVERS, query=fake_dns(answers))
    assert result.confirmed_nxdomain
    assert not result.resolvers_agreed  # reported, not hidden


def test_single_resolver_nxdomain_is_never_enough():
    """A false NXDOMAIN would fabricate an available domain — the most
    expensive error this tool can make."""
    answers = {RESOLVERS[0]: nx(RESOLVERS[0]),
               RESOLVERS[1]: ResolverAnswer(RESOLVERS[1], DnsStatus.TIMEOUT),
               RESOLVERS[2]: ResolverAnswer(RESOLVERS[2], DnsStatus.TIMEOUT)}
    result = lookup("maybe.example", RESOLVERS, query=fake_dns(answers))
    assert not result.confirmed_nxdomain


def test_one_positive_answer_beats_two_nxdomains():
    """Disagreement errs toward 'registered'. A missed candidate costs nothing;
    a fabricated one costs real money."""
    answers = {RESOLVERS[0]: nx(RESOLVERS[0]), RESOLVERS[1]: nx(RESOLVERS[1]),
               RESOLVERS[2]: ok(RESOLVERS[2])}
    result = lookup("live.example", RESOLVERS, query=fake_dns(answers))
    assert result.status is DnsStatus.OK
    assert not result.confirmed_nxdomain


def test_no_answer_means_registered_but_undelegated():
    answers = {ip: ResolverAnswer(ip, DnsStatus.NO_RECORDS) for ip in RESOLVERS}
    result = lookup("quiet.example", RESOLVERS, query=fake_dns(answers))
    assert result.status is DnsStatus.NO_RECORDS
    assert not result.confirmed_nxdomain


# -- RDAP bootstrap ---------------------------------------------------------

BOOTSTRAP = json.dumps({
    "version": "1.0",
    "publication": "2026-07-23T02:00:03Z",
    "services": [
        [["com", "net"], ["https://rdap.verisign.com/com/v1/"]],
        [["uk"], ["https://rdap.nominet.uk/uk/"]],
        [["co.uk"], ["https://rdap.nominet.uk/couk/"]],
    ],
})


def test_bootstrap_maps_tlds():
    b = Bootstrap.from_json(BOOTSTRAP)
    assert b.base_url("example.com") == "https://rdap.verisign.com/com/v1/"
    assert b.base_url("example.net") == "https://rdap.verisign.com/com/v1/"


def test_bootstrap_prefers_the_longest_match():
    """RFC 9224 label-wise longest match — co.uk must beat uk."""
    b = Bootstrap.from_json(BOOTSTRAP)
    assert b.base_url("bbc.co.uk") == "https://rdap.nominet.uk/couk/"
    assert b.base_url("bbc.uk") == "https://rdap.nominet.uk/uk/"


def test_uncovered_tld_returns_none():
    """Real gap: .de, .es, .io and .ru publish no RDAP service at all."""
    assert Bootstrap.from_json(BOOTSTRAP).base_url("example.de") is None


def test_bootstrap_is_cached_and_not_refetched(tmp_path):
    calls = []

    def fetch(url):
        calls.append(url)
        return 200, BOOTSTRAP

    assert load_bootstrap(tmp_path, fetch) is not None
    assert load_bootstrap(tmp_path, fetch) is not None
    assert len(calls) == 1  # second load came from disk


def test_stale_cache_is_preferred_over_no_data(tmp_path):
    """An out-of-date TLD map beats performing no domain checks at all."""
    (tmp_path / "rdap-bootstrap.json").write_text(BOOTSTRAP, encoding="utf-8")
    import os, time
    old = time.time() - 30 * 86_400
    os.utime(tmp_path / "rdap-bootstrap.json", (old, old))
    result = load_bootstrap(tmp_path, lambda url: (None, ""))
    assert result is not None and len(result) > 0


# -- RDAP queries -----------------------------------------------------------

REGISTERED = json.dumps({
    "status": ["client transfer prohibited"],
    "events": [{"eventAction": "expiration", "eventDate": "2030-01-01T00:00:00Z"}],
    "entities": [{"roles": ["registrar"],
                  "vcardArray": ["vcard", [["fn", {}, "text", "Example Registrar"]]]}],
})


def test_registered_domain_parsed():
    b = Bootstrap.from_json(BOOTSTRAP)
    result = query("example.com", b, lambda url: (200, REGISTERED))
    assert result.status is RdapStatus.REGISTERED
    assert result.registrar == "Example Registrar"
    assert result.expiry.startswith("2030")


def test_404_is_not_found():
    b = Bootstrap.from_json(BOOTSTRAP)
    assert query("gone.com", b, lambda url: (404, "")).status is RdapStatus.NOT_FOUND


@pytest.mark.parametrize("status", [429, 500, 503, None])
def test_errors_are_unavailable_never_not_found(status):
    """A rate-limited registry means we do not know. Recording that as
    'not found' would fabricate an available domain."""
    b = Bootstrap.from_json(BOOTSTRAP)
    result = query("example.com", b, lambda url: (status, ""))
    assert result.status is RdapStatus.UNAVAILABLE
    assert result.status is not RdapStatus.NOT_FOUND


def test_uncovered_tld_is_its_own_status():
    b = Bootstrap.from_json(BOOTSTRAP)
    assert query("example.de", b, lambda url: (200, "{}")).status is RdapStatus.NO_RDAP_FOR_TLD


def test_malformed_json_is_unavailable():
    b = Bootstrap.from_json(BOOTSTRAP)
    assert query("example.com", b, lambda url: (200, "not json")).status is RdapStatus.UNAVAILABLE


@pytest.mark.parametrize("epp", ["pendingDelete", "redemption period", "clientHold"])
def test_epp_winding_down_statuses_detected(epp):
    b = Bootstrap.from_json(BOOTSTRAP)
    body = json.dumps({"status": [epp]})
    assert query("example.com", b, lambda url: (200, body)).expiring


def test_expires_within_window():
    from datetime import UTC, datetime, timedelta
    now = datetime(2026, 7, 25, tzinfo=UTC)
    soon = (now + timedelta(days=20)).isoformat()
    far = (now + timedelta(days=400)).isoformat()
    assert expires_within(soon, 60, now=now)
    assert not expires_within(far, 60, now=now)
    assert not expires_within(None, 60, now=now)


# -- the unregistered gate --------------------------------------------------


def dns_nx(count=3):
    answers = [nx(f"r{i}") for i in range(count)]
    return DnsResult(status=DnsStatus.NXDOMAIN, answers=answers, resolvers_agreed=True)


def dns_ok():
    return DnsResult(status=DnsStatus.OK, answers=[ok("r0"), ok("r1")],
                     resolvers_agreed=True, ns_records=["ns1.example.net"])


def test_unregistered_requires_both_signals():
    verdict = classify(dns_nx(), RdapResult(RdapStatus.NOT_FOUND))
    assert verdict.state == DomainState.UNREGISTERED
    assert verdict.confidence == 1.0


def test_nxdomain_without_rdap_confirmation_is_not_unregistered():
    verdict = classify(dns_nx(), RdapResult(RdapStatus.UNAVAILABLE, error="HTTP 429"))
    assert verdict.state != DomainState.UNREGISTERED
    assert verdict.state == DomainState.UNKNOWN


def test_nxdomain_on_a_tld_without_rdap_cannot_be_confirmed():
    """.de/.es/.io/.ru have no RDAP. Claiming availability would be a guess
    dressed as a fact."""
    verdict = classify(dns_nx(), RdapResult(RdapStatus.NO_RDAP_FOR_TLD))
    assert verdict.state == DomainState.NO_RDAP_FOR_TLD
    assert any("cannot confirm" in r for r in verdict.reasons)


def test_single_resolver_nxdomain_never_reaches_unregistered():
    result = DnsResult(status=DnsStatus.NXDOMAIN, answers=[nx("r0")])
    verdict = classify(result, RdapResult(RdapStatus.NOT_FOUND))
    assert verdict.state != DomainState.UNREGISTERED


def test_rdap_registered_while_dns_says_gone_is_not_unregistered():
    """Undelegated but owned — common during transfers and registrar holds."""
    verdict = classify(dns_nx(), RdapResult(RdapStatus.REGISTERED))
    assert verdict.state != DomainState.UNREGISTERED


# -- other domain states ----------------------------------------------------


def test_active_domain():
    verdict = classify(dns_ok(), RdapResult(RdapStatus.REGISTERED, registrar="R"))
    assert verdict.state == DomainState.ACTIVE


def test_epp_status_makes_it_expiring():
    rdap = RdapResult(RdapStatus.REGISTERED, statuses=["pendingDelete"])
    assert classify(dns_ok(), rdap).state == DomainState.EXPIRING


def test_near_expiry_makes_it_expiring():
    from datetime import UTC, datetime, timedelta
    soon = (datetime.now(UTC) + timedelta(days=10)).isoformat()
    rdap = RdapResult(RdapStatus.REGISTERED, expiry=soon)
    assert classify(dns_ok(), rdap).state == DomainState.EXPIRING


def test_parked_is_lifted_from_url_verdicts():
    """The crawler already saw the parking page; re-deriving it here would be
    duplicated guesswork."""
    verdict = classify(dns_ok(), RdapResult(RdapStatus.REGISTERED),
                       url_states={"parked": 3, "live": 1})
    assert verdict.state == DomainState.PARKED


def test_one_parked_url_does_not_condemn_a_healthy_domain():
    verdict = classify(dns_ok(), RdapResult(RdapStatus.REGISTERED),
                       url_states={"parked": 1, "live": 9})
    assert verdict.state == DomainState.ACTIVE


def test_for_sale_outranks_parked():
    verdict = classify(dns_ok(), RdapResult(RdapStatus.REGISTERED),
                       url_states={"for_sale": 2, "parked": 1})
    assert verdict.state == DomainState.FOR_SALE


def test_expiring_outranks_a_parked_page():
    """A registration winding down is more actionable than a parking page."""
    rdap = RdapResult(RdapStatus.REGISTERED, statuses=["redemptionPeriod"])
    verdict = classify(dns_ok(), rdap, url_states={"parked": 5})
    assert verdict.state == DomainState.EXPIRING


def test_every_verdict_records_why():
    for dns_result, rdap in [
        (dns_nx(), RdapResult(RdapStatus.NOT_FOUND)),
        (dns_ok(), RdapResult(RdapStatus.REGISTERED)),
        (dns_nx(), RdapResult(RdapStatus.NO_RDAP_FOR_TLD)),
    ]:
        assert classify(dns_result, rdap).reasons

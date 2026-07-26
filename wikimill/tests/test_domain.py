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





def test_near_expiry_alone_does_not_make_it_expiring():
    """Corrected after real data: "expires within 60 days" flagged ca.gov,
    gao.gov and osce.org — institutions on ordinary annual renewal cycles. About
    a sixth of all domains sit in any 60-day window at any moment, so the date
    predicts almost nothing. It now drives recheck urgency, not state."""
    from datetime import UTC, datetime, timedelta
    soon = (datetime.now(UTC) + timedelta(days=10)).isoformat()
    rdap = RdapResult(RdapStatus.REGISTERED, expiry=soon)
    assert classify(dns_ok(), rdap).state == DomainState.ACTIVE


def test_auto_renew_period_is_not_expiring():
    """autoRenewPeriod is the grace window *after* an automatic renewal — the
    opposite of expiring. It flagged wildlifetrusts.org, valid until 2031."""
    rdap = RdapResult(
        RdapStatus.REGISTERED,
        statuses=["client transfer prohibited", "auto renew period"],
        expiry="2031-06-29T00:00:00Z",
    )
    assert classify(dns_ok(), rdap).state == DomainState.ACTIVE


def test_pending_restore_is_not_expiring():
    """Someone actively reclaiming a domain is not a signal it is becoming
    available."""
    rdap = RdapResult(RdapStatus.REGISTERED, statuses=["pending restore"])
    assert classify(dns_ok(), rdap).state == DomainState.ACTIVE


@pytest.mark.parametrize("epp", ["pendingDelete", "redemption period", "clientHold"])
def test_registry_lifecycle_statuses_do_make_it_expiring(epp):
    rdap = RdapResult(RdapStatus.REGISTERED, statuses=[epp])
    verdict = classify(dns_ok(), rdap)
    assert verdict.state == DomainState.EXPIRING
    assert any("lifecycle" in r for r in verdict.reasons)


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
    rdap = RdapResult(RdapStatus.REGISTERED, statuses=["redemption period"])
    verdict = classify(dns_ok(), rdap, url_states={"parked": 5})
    assert verdict.state == DomainState.EXPIRING


def test_every_verdict_records_why():
    for dns_result, rdap in [
        (dns_nx(), RdapResult(RdapStatus.NOT_FOUND)),
        (dns_ok(), RdapResult(RdapStatus.REGISTERED)),
        (dns_nx(), RdapResult(RdapStatus.NO_RDAP_FOR_TLD)),
    ]:
        assert classify(dns_result, rdap).reasons


# -- parallel execution ------------------------------------------------------


def test_rdap_stays_bounded_per_registry(tmp_path, monkeypatch):
    """`.com` is ~41% of a tail sweep, so partitioning by registry would cap the
    speedup. Instead each registry gets a small semaphore — this asserts the
    bound actually holds under concurrency."""
    import threading
    from wikimill.domain import runner as dr

    live = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def slow_query(domain, bootstrap, fetch):
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        try:
            import time as _t
            _t.sleep(0.02)
            return RdapResult(RdapStatus.REGISTERED)
        finally:
            with lock:
                live["now"] -= 1

    monkeypatch.setattr(dr.rdap_mod, "query", slow_query)
    monkeypatch.setattr(dr.rdap_mod, "load_bootstrap",
                        lambda *_a, **_k: Bootstrap.from_json(BOOTSTRAP))
    monkeypatch.setattr(dr.dns_mod, "lookup", lambda *_a, **_k: dns_ok())

    from wikimill.config import load
    from wikimill.logging import RunLog
    from wikimill.storage import open_db

    monkeypatch.setenv("WIKIMILL_CONTACT", "ops@example.org")
    cfg = load(tmp_path)
    with open_db(cfg.db_path) as conn:
        for i in range(40):
            conn.execute(
                "INSERT INTO domains (registrable_domain, public_suffix, state, first_seen,"
                " wiki_page_count) VALUES (?,?,?,?,?)",
                (f"host{i}.com", "com", "unknown", "2026-07-25T00:00:00+00:00", 1))

    dr.run(cfg, RunLog("check", tmp_path / "l", quiet=True), limit=40, concurrency=16)
    assert live["peak"] <= dr.RDAP_CONCURRENCY_PER_REGISTRY, (
        f"{live['peak']} concurrent requests hit one registry; "
        f"limit is {dr.RDAP_CONCURRENCY_PER_REGISTRY}")


def test_a_failing_probe_does_not_hang_the_collector(tmp_path, monkeypatch):
    """The crawl's silent-hang bug, in a new stage: a worker that dies without
    reporting leaves the collector waiting forever."""
    from wikimill.domain import runner as dr
    from wikimill.config import load
    from wikimill.logging import RunLog
    from wikimill.storage import open_db

    monkeypatch.setattr(dr.dns_mod, "lookup",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(dr.rdap_mod, "load_bootstrap", lambda *_a, **_k: None)
    monkeypatch.setenv("WIKIMILL_CONTACT", "ops@example.org")
    cfg = load(tmp_path)
    with open_db(cfg.db_path) as conn:
        conn.execute(
            "INSERT INTO domains (registrable_domain, public_suffix, state, first_seen,"
            " wiki_page_count) VALUES ('boom.com','com','unknown','2026-07-25T00:00:00+00:00',1)")
    stats = dr.run(cfg, RunLog("check", tmp_path / "l", quiet=True), limit=5)
    assert stats.checked == 0  # reported, not hung

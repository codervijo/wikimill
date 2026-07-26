"""The classifier: the eleven-state vocabulary and the state machine.

The classifier is a pure function, so these tests are just inputs and outputs —
no network, no database, no clock. That testability is the point of the design,
not a side effect of it.
"""

from __future__ import annotations

import pytest

from wikimill.classify.rules import Observation, classify
from wikimill.classify.state import (
    HARD_404_CONFIRMATIONS,
    is_terminal,
    recheck_seconds,
)
from wikimill.constants import RECHECK_INTERVALS, UrlState


def obs(**kw) -> Observation:
    kw.setdefault("url", "http://example.com/page")
    return Observation(**kw)


# -- transport-level facts --------------------------------------------------


@pytest.mark.parametrize("kind", ["dns_nxdomain", "dns_error"])
def test_dns_failures(kind):
    assert classify(obs(error_kind=kind)).classification == UrlState.DNS_FAILURE


@pytest.mark.parametrize(
    "kind",
    ["tls_cert_expired", "tls_hostname_mismatch", "tls_chain_untrusted", "tls_error"],
)
def test_tls_failures(kind):
    assert classify(obs(error_kind=kind)).classification == UrlState.TLS_FAILURE


def test_tls_subkind_survives_into_the_reasons():
    """The sub-kind is the signal — 'cert expired' means neglect, which is a
    leading indicator; 'host gone' means something else entirely."""
    verdict = classify(obs(error_kind="tls_cert_expired"))
    assert any("tls_cert_expired" in r for r in verdict.reasons)


@pytest.mark.parametrize("kind", ["connect_timeout", "read_timeout", "connection_refused"])
def test_transient_transport_errors(kind):
    assert classify(obs(error_kind=kind)).classification == UrlState.TEMPORARILY_UNAVAILABLE


def test_unknown_error_kind_is_unclassified_not_guessed():
    verdict = classify(obs(error_kind="something_new"))
    assert verdict.classification == UrlState.UNCLASSIFIED
    assert verdict.confidence < 0.5


# -- status codes -----------------------------------------------------------


@pytest.mark.parametrize("status", [404, 410])
def test_hard_404(status):
    assert classify(obs(http_status=status)).classification == UrlState.HARD_404


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_server_errors_are_temporary(status):
    assert classify(obs(http_status=status)).classification == UrlState.TEMPORARILY_UNAVAILABLE


def test_plain_200_is_live():
    verdict = classify(obs(http_status=200, content_length=9000, page_title="A Site"))
    assert verdict.classification == UrlState.LIVE


@pytest.mark.parametrize("status", [401, 403, 451])
def test_restricted_access_means_the_host_is_alive(status):
    """The question this tool asks is whether the domain is alive. A 403 answers
    yes — recording it as dead would be wrong."""
    verdict = classify(obs(http_status=status, content_length=900))
    assert verdict.classification == UrlState.LIVE
    assert str(status) in verdict.reasons[0]


# -- robots -----------------------------------------------------------------


def test_robots_disallow_short_circuits_everything():
    verdict = classify(obs(robots_decision="disallowed by robots.txt", fetched=False))
    assert verdict.classification == UrlState.BLOCKED_BY_ROBOTS


# -- parked / for sale ------------------------------------------------------


def test_parking_provider_signature():
    verdict = classify(
        obs(http_status=200, content_length=3000,
            evidence="<script src='http://sedoparking.com/x.js'></script>")
    )
    assert verdict.classification == UrlState.PARKED
    assert any("sedoparking" in r for r in verdict.reasons)


def test_parked_plus_sale_offer_is_for_sale():
    verdict = classify(
        obs(http_status=200, content_length=3000,
            page_title="example.com",
            evidence="parkingcrew.net — this domain is for sale, buy this domain")
    )
    assert verdict.classification == UrlState.FOR_SALE


def test_sale_offer_alone_is_lower_confidence():
    """No parking signature, so it is reported but not asserted."""
    verdict = classify(
        obs(http_status=200, content_length=3000,
            evidence="hello, this domain is for sale, contact us")
    )
    assert verdict.classification == UrlState.FOR_SALE
    assert verdict.confidence < 0.8


def test_weak_parking_words_alone_do_not_park_a_live_site():
    """'related searches' appears on plenty of real pages. Marking a live site
    parked is the error the operator would act on."""
    verdict = classify(
        obs(http_status=200, content_length=9000, page_title="My Blog",
            evidence="<h2>Related searches</h2><p>ordinary article text</p>")
    )
    assert verdict.classification == UrlState.LIVE


def test_article_about_parking_is_not_parked():
    verdict = classify(
        obs(http_status=200, content_length=9000,
            page_title="How domain parking works",
            evidence="An article explaining the economics of domain parking.")
    )
    assert verdict.classification == UrlState.LIVE


# -- soft 404 ---------------------------------------------------------------


def test_soft_404_from_title_and_body():
    verdict = classify(
        obs(http_status=200, content_length=1200, page_title="404 Not Found",
            evidence="<h1>Page not found</h1>")
    )
    assert verdict.classification == UrlState.SOFT_404


def test_thin_body_alone_is_not_enough():
    """A legitimately tiny page exists — a redirect stub, an API root."""
    verdict = classify(obs(http_status=200, content_length=100, evidence="ok"))
    assert verdict.classification != UrlState.SOFT_404


def test_deep_path_redirected_to_root_plus_thin_body():
    verdict = classify(
        obs(url="http://example.com/deep/article/123", http_status=200,
            content_length=200, redirect_count=1, final_url="http://example.com/",
            evidence="home")
    )
    assert verdict.classification == UrlState.SOFT_404
    assert any("site root" in r for r in verdict.reasons)


def test_normal_page_with_the_word_error_is_not_a_soft_404():
    verdict = classify(
        obs(http_status=200, content_length=20000, page_title="Standard Error in Statistics",
            evidence="<p>The standard error of the mean is …</p>")
    )
    assert verdict.classification == UrlState.LIVE


# -- redirects --------------------------------------------------------------


def test_redirect_to_live_page():
    verdict = classify(
        obs(http_status=200, content_length=9000, redirect_count=1,
            final_url="https://example.com/new", evidence="content")
    )
    assert verdict.classification == UrlState.REDIRECT


def test_cross_domain_redirect_is_flagged_as_possible_handover():
    verdict = classify(
        obs(http_status=200, content_length=9000, redirect_count=1,
            cross_domain_redirect=True, final_url="https://nytimes.com/",
            evidence="content")
    )
    assert verdict.classification == UrlState.REDIRECT
    assert any("handover" in r for r in verdict.reasons)


def test_redirect_ending_in_404_is_still_a_404():
    """The page is gone regardless of how many hops it took to find that out."""
    verdict = classify(obs(http_status=404, redirect_count=2))
    assert verdict.classification == UrlState.HARD_404


# -- `unregistered` is unreachable from HTTP --------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"error_kind": "dns_nxdomain"},
        {"http_status": 404},
        {"http_status": 200, "evidence": "this domain is for sale"},
        {"error_kind": "connect_error"},
    ],
)
def test_no_http_observation_can_yield_unregistered(kwargs):
    """A false 'available domain' is the most expensive error this tool can
    make. It requires two resolvers plus RDAP (v1.G) — there is deliberately no
    path to it from a fetch."""
    assert classify(obs(**kwargs)).classification != UrlState.UNREGISTERED


# -- state machine ----------------------------------------------------------


def test_unregistered_is_rechecked_fastest_of_the_settled_states():
    """It is the most volatile record in the database: anyone can register it
    tomorrow. (`temporarily_unavailable` is shorter still, but that is the start
    of a retry backoff, not a standing cadence.)"""
    settled = {
        state: secs
        for state, secs in RECHECK_INTERVALS.items()
        if state != UrlState.TEMPORARILY_UNAVAILABLE
    }
    assert recheck_seconds(UrlState.UNREGISTERED) == min(settled.values())


@pytest.mark.parametrize(
    "title",
    ["Standard Error in Statistics", "Trial and Error", "Error Analysis in Physics"],
)
def test_real_titles_containing_error_are_not_soft_404s(title):
    """Regression: bare 'error' as a title marker matched genuine pages."""
    verdict = classify(
        obs(http_status=200, content_length=20000, page_title=title,
            evidence="<p>a real article</p>")
    )
    assert verdict.classification == UrlState.LIVE


def test_unregistered_is_never_terminal():
    assert not is_terminal(UrlState.UNREGISTERED, 99)


def test_live_is_rechecked_patiently():
    assert recheck_seconds(UrlState.LIVE) > recheck_seconds(UrlState.PARKED)


def test_hard_404_becomes_terminal_only_after_confirmations():
    assert not is_terminal(UrlState.HARD_404, HARD_404_CONFIRMATIONS - 1)
    assert is_terminal(UrlState.HARD_404, HARD_404_CONFIRMATIONS)


def test_hard_404_backs_off_but_is_capped():
    first = recheck_seconds(UrlState.HARD_404)
    later = recheck_seconds(UrlState.HARD_404, repeats=3)
    assert later > first
    assert recheck_seconds(UrlState.HARD_404, repeats=99) == 180 * 86_400


def test_high_value_states_do_not_back_off():
    """The value of a `parked` or `for_sale` observation is its freshness."""
    for state in (UrlState.PARKED, UrlState.FOR_SALE, UrlState.UNREGISTERED):
        assert recheck_seconds(state, repeats=5) == recheck_seconds(state)


@pytest.mark.parametrize(
    "state",
    [UrlState.LIVE, UrlState.PARKED, UrlState.FOR_SALE, UrlState.DNS_FAILURE,
     UrlState.TLS_FAILURE, UrlState.SOFT_404, UrlState.REDIRECT],
)
def test_nothing_else_is_terminal(state):
    assert not is_terminal(state, 99)


def test_classifier_is_deterministic():
    o = obs(http_status=200, content_length=5000, evidence="sedoparking.com")
    assert classify(o).classification == classify(o).classification

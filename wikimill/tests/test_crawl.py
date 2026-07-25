"""The crawler: SSRF guards, robots.txt, politeness, and the fetcher.

Hermetic throughout — every HTTP interaction goes through httpx.MockTransport
and every DNS lookup through a fake resolver. No test here touches the network.
"""

from __future__ import annotations

import socket

import httpx
import pytest

from wikimill.crawl import fetcher as fetcher_mod
from wikimill.crawl.fetcher import extract_title, fetch
from wikimill.crawl.guard import (
    BlockedAddress,
    check_address,
    classify_address,
    resolve_and_check,
)
from wikimill.crawl.politeness import (
    CIRCUIT_THRESHOLD,
    HostState,
    Politeness,
    backoff_delay,
    should_retry,
)
from wikimill.crawl.robots import evaluate, verdict_for_status


def fake_resolver(mapping: dict[str, list[str]]):
    def _resolve(host, *_a, **_kw):
        if host not in mapping:
            raise socket.gaierror(-2, "Name or service not known")
        return [(2, 1, 6, "", (addr, 0)) for addr in mapping[host]]

    return _resolve


PUBLIC = fake_resolver({"example.com": ["93.184.216.34"], "other.com": ["93.184.216.35"]})


# -- SSRF guards ------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "reason"),
    [
        ("127.0.0.1", "loopback"),
        ("::1", "loopback"),
        ("10.0.0.5", "private range"),
        ("172.16.0.1", "private range"),
        ("192.168.1.1", "private range"),
        ("169.254.169.254", "cloud metadata endpoint"),
        ("169.254.1.1", "link-local"),
        ("0.0.0.0", "unspecified"),
        ("fc00::1", "private range"),
    ],
)
def test_blocked_ranges(address, reason):
    import ipaddress

    assert classify_address(ipaddress.ip_address(address)) == reason


@pytest.mark.parametrize("address", ["93.184.216.34", "8.8.8.8", "2606:4700::1111"])
def test_public_addresses_allowed(address):
    import ipaddress

    assert classify_address(ipaddress.ip_address(address)) is None


def test_ipv4_mapped_ipv6_loopback_is_blocked():
    """`::ffff:127.0.0.1` reaches v4 loopback while slipping past v6 predicates."""
    with pytest.raises(BlockedAddress):
        check_address("evil.example", "::ffff:127.0.0.1")


def test_host_that_is_itself_a_private_literal_is_blocked():
    with pytest.raises(BlockedAddress):
        check_address("127.0.0.1", "127.0.0.1")


def test_any_private_address_blocks_the_host():
    """A host resolving to both public and private is a classic bypass —
    contacting it would be a coin flip."""
    resolver = fake_resolver({"mixed.example": ["93.184.216.34", "127.0.0.1"]})
    with pytest.raises(BlockedAddress):
        resolve_and_check("mixed.example", resolver=resolver)


def test_public_host_resolves():
    result = resolve_and_check("example.com", resolver=PUBLIC)
    assert result.addresses == ("93.184.216.34",)


def test_unresolvable_host_raises_lookup_error():
    with pytest.raises(LookupError):
        resolve_and_check("nope.invalid", resolver=PUBLIC)


# -- fetcher ----------------------------------------------------------------


def transport(handler):
    return httpx.MockTransport(handler)


def client_for(handler):
    return fetcher_mod.build_client(transport=transport(handler))


def test_successful_fetch_records_evidence():
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<html><head><title>Hello</title></head><body>hi</body></html>",
        )

    with client_for(handler) as client:
        result = fetch(client, "http://example.com/", user_agent="wikimill/test",
                       resolver=PUBLIC)
    assert result.http_status == 200
    assert result.page_title == "Hello"
    assert result.body_sha256
    assert result.evidence_blob.startswith("<html>")
    assert result.latency_ms is not None
    assert result.ok


def test_404_is_recorded_not_raised():
    with client_for(lambda r: httpx.Response(404, content=b"gone")) as client:
        result = fetch(client, "http://example.com/x", user_agent="ua", resolver=PUBLIC)
    assert result.http_status == 404
    assert result.ok  # a 404 is a successful observation, not a fetch error


def test_redirect_chain_is_followed_and_recorded():
    def handler(request):
        if request.url.path == "/a":
            return httpx.Response(301, headers={"location": "http://example.com/b"})
        return httpx.Response(200, content=b"<title>End</title>")

    with client_for(handler) as client:
        result = fetch(client, "http://example.com/a", user_agent="ua", resolver=PUBLIC)
    assert result.redirect_count == 1
    assert result.redirects[0].status == 301
    assert result.final_url == "http://example.com/b"
    assert result.page_title == "End"


def test_every_redirect_hop_is_ssrf_checked():
    """Checking only the first hop is how redirect-to-internal slips through."""
    resolver = fake_resolver({
        "example.com": ["93.184.216.34"],
        "evil.example": ["169.254.169.254"],
    })

    def handler(request):
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://evil.example/"})
        return httpx.Response(200, content=b"secrets")

    with client_for(handler) as client:
        result = fetch(client, "http://example.com/", user_agent="ua", resolver=resolver)
    assert result.error_kind == "blocked_address"
    assert "169.254.169.254" in result.error_detail


def test_redirect_loop_detected():
    def handler(request):
        return httpx.Response(302, headers={"location": "http://example.com/loop"})

    with client_for(handler) as client:
        result = fetch(client, "http://example.com/loop", user_agent="ua",
                       resolver=PUBLIC)
    assert result.error_kind == "redirect_loop"
    assert not result.transient  # a loop is permanent, never retried


def test_redirect_cap_enforced():
    counter = {"n": 0}

    def handler(request):
        counter["n"] += 1
        return httpx.Response(
            302, headers={"location": f"http://example.com/{counter['n']}"}
        )

    with client_for(handler) as client:
        result = fetch(client, "http://example.com/0", user_agent="ua",
                       resolver=PUBLIC, max_redirects=3)
    assert result.error_kind == "too_many_redirects"


def test_body_is_capped_while_streaming():
    """A hostile server must not be able to make us buffer an endless body."""
    big = b"x" * 50_000
    with client_for(lambda r: httpx.Response(200, content=big)) as client:
        result = fetch(client, "http://example.com/", user_agent="ua",
                       resolver=PUBLIC, max_body=1024)
    assert result.content_length == 1024


def test_evidence_blob_is_bounded():
    with client_for(lambda r: httpx.Response(200, content=b"y" * 100_000)) as client:
        result = fetch(client, "http://example.com/", user_agent="ua", resolver=PUBLIC)
    assert len(result.evidence_blob) <= 8 * 1024


def test_robots_sized_fetch_is_not_capped_at_the_evidence_limit():
    """A robots.txt over 8 KB must arrive whole: truncating it drops the later
    rules and could let us fetch something the site disallowed."""
    body = b"# pad\n" * 3000 + b"User-agent: *\nDisallow: /secret\n"
    assert len(body) > 8 * 1024
    with client_for(lambda r: httpx.Response(200, content=body)) as client:
        result = fetch(client, "http://example.com/robots.txt", user_agent="ua",
                       resolver=PUBLIC, max_body=512 * 1024, max_evidence=512 * 1024)
    assert "Disallow: /secret" in result.evidence_blob
    verdict = evaluate(result.evidence_blob, "wikimill", "http://example.com/secret/x")
    assert not verdict.allowed


def test_dns_failure_is_recorded_with_its_kind():
    with client_for(lambda r: httpx.Response(200)) as client:
        result = fetch(client, "http://nope.invalid/", user_agent="ua", resolver=PUBLIC)
    assert result.error_kind.startswith("dns")
    assert not result.ok


def test_timeout_is_transient():
    def handler(request):
        raise httpx.ConnectTimeout("timed out")

    with client_for(handler) as client:
        result = fetch(client, "http://example.com/", user_agent="ua", resolver=PUBLIC)
    assert result.error_kind == "connect_timeout"
    assert result.transient  # retrying may help


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("certificate has expired", "tls_cert_expired"),
        ("hostname mismatch, certificate is not valid", "tls_hostname_mismatch"),
        ("self signed certificate in chain", "tls_chain_untrusted"),
        ("connection refused", "connection_refused"),
    ],
)
def test_tls_subkinds_are_not_lumped_together(message, kind):
    """'The cert expired' and 'the host is gone' lead to completely different
    conclusions about a domain — collapsing them discards the signal."""

    def handler(request):
        raise httpx.ConnectError(message)

    with client_for(handler) as client:
        result = fetch(client, "http://example.com/", user_agent="ua", resolver=PUBLIC)
    assert result.error_kind == kind


def test_retry_after_seconds_parsed():
    def handler(request):
        return httpx.Response(429, headers={"retry-after": "12"})

    with client_for(handler) as client:
        result = fetch(client, "http://example.com/", user_agent="ua", resolver=PUBLIC)
    assert result.retry_after == 12.0


def test_user_agent_is_sent_and_not_spoofed():
    seen = {}

    def handler(request):
        seen["ua"] = request.headers.get("user-agent")
        return httpx.Response(200)

    with client_for(handler) as client:
        fetch(client, "http://example.com/", user_agent="wikimill/0.1.0 (+me@example.org)",
              resolver=PUBLIC)
    assert seen["ua"] == "wikimill/0.1.0 (+me@example.org)"
    assert "Mozilla" not in seen["ua"]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"<title>Simple</title>", "Simple"),
        (b"<TITLE>Upper</TITLE>", "Upper"),
        (b"<title>\n  spaced\n  out\n</title>", "spaced out"),
        (b"<title>with <b>tags</b></title>", "with tags"),
        (b"<html>no title</html>", None),
    ],
)
def test_title_extraction(body, expected):
    assert extract_title(body) == expected


# -- robots.txt -------------------------------------------------------------


ROBOTS = "User-agent: *\nDisallow: /private\nCrawl-delay: 5\n"


def test_disallowed_path_blocked():
    assert not evaluate(ROBOTS, "wikimill", "http://example.com/private/x").allowed


def test_allowed_path_permitted():
    assert evaluate(ROBOTS, "wikimill", "http://example.com/public").allowed


def test_crawl_delay_read():
    assert evaluate(ROBOTS, "wikimill", "http://example.com/ok").crawl_delay == 5.0


@pytest.mark.parametrize("status", [404, 410, 400])
def test_4xx_means_allow_all(status):
    """RFC 9309: robots.txt 'unavailable' — nothing has been restricted."""
    assert verdict_for_status(status, "", "wikimill", "http://example.com/x").allowed


@pytest.mark.parametrize("status", [500, 503, 429])
def test_5xx_and_429_mean_disallow_all(status):
    """RFC 9309: 'unreachable' — a struggling server must not be hammered on
    the assumption that silence means consent."""
    assert not verdict_for_status(status, "", "wikimill", "http://example.com/x").allowed


def test_unreachable_robots_means_disallow():
    assert not verdict_for_status(None, "", "wikimill", "http://example.com/x").allowed


# -- politeness -------------------------------------------------------------


def test_backoff_is_bounded_and_jittered():
    for attempt in range(1, 6):
        assert 0.0 <= backoff_delay(attempt, jitter=lambda: 1.0) <= 60.0
    assert backoff_delay(1, base=2, jitter=lambda: 1.0) == 2.0
    assert backoff_delay(3, base=2, jitter=lambda: 1.0) == 8.0


def test_backoff_full_jitter_can_be_short():
    """Full jitter, so retries do not synchronise into a storm."""
    assert backoff_delay(5, jitter=lambda: 0.0) == 0.0


def test_only_transient_failures_retry():
    assert should_retry(1, transient=True)
    assert not should_retry(1, transient=False)


def test_retries_are_bounded():
    assert not should_retry(99, transient=True)


def test_circuit_breaker_trips_after_repeated_failures():
    state = HostState(delay=0.0)
    for _ in range(CIRCUIT_THRESHOLD):
        state.record(success=False)
    assert state.tripped


def test_success_resets_the_failure_run():
    state = HostState(delay=0.0)
    state.record(success=False)
    state.record(success=True)
    assert state.consecutive_failures == 0
    assert not state.tripped


def test_site_crawl_delay_can_only_slow_us_down():
    """We honour a site asking for more time; we never use it to go faster."""
    politeness = Politeness(default_delay=2.0)
    assert politeness.for_host("a.example", crawl_delay=10.0).delay == 10.0
    assert politeness.for_host("b.example", crawl_delay=0.1).delay == 2.0


def test_first_request_to_a_host_does_not_wait():
    slept: list[float] = []
    state = HostState(delay=5.0)
    assert state.wait(sleep=slept.append, now=lambda: 100.0) == 0.0
    assert slept == []


def test_second_request_waits_out_the_remaining_delay():
    slept: list[float] = []
    state = HostState(delay=5.0)
    state.wait(sleep=slept.append, now=lambda: 100.0)   # t=100, no wait
    state.wait(sleep=slept.append, now=lambda: 102.0)   # 2s elapsed of 5s
    assert slept == [pytest.approx(3.0)]


def test_no_wait_once_the_delay_has_already_passed():
    slept: list[float] = []
    state = HostState(delay=1.0)
    state.wait(sleep=slept.append, now=lambda: 100.0)
    state.wait(sleep=slept.append, now=lambda: 200.0)
    assert slept == []

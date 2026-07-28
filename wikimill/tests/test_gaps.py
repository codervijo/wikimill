"""Archive-gap checks (v4.B).

There are three answers here, not two — a copy exists, no copy exists, or we
could not ask — and almost every test below defends the boundary between the
last two. Reporting "no copy anywhere" for a citation that merely timed out
tells the operator a source is permanently lost while it sits in the archive.
That is the expensive mistake this stage can make, and `has_snapshot` is
nullable specifically to prevent it.

The second theme is that **archived is not the same as recovered**. The Internet
Archive faithfully preserves 404 pages, and a capture of a not-found page
recovers nothing.

Hermetic throughout: `httpx.MockTransport`, so the suite never contacts
archive.org.
"""

from __future__ import annotations

import httpx
import pytest

from wikimill import gaps as gaps_mod
from wikimill import wayback as wayback_mod
from wikimill.constants import DomainState, UrlState
from wikimill.errors import ConfigError
from wikimill.storage import open_db

NOW = "2026-07-25T00:00:00+00:00"
RUN = "20260701"


def client_returning(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def snapshot(status="200", available=True, timestamp="20130919044612"):
    return httpx.Response(200, json={"archived_snapshots": {"closest": {
        "status": status, "available": available,
        "url": f"http://web.archive.org/web/{timestamp}/http://gone.example/a",
        "timestamp": timestamp,
    }}})


def no_snapshot():
    return httpx.Response(200, json={"archived_snapshots": {}})


def seed(conn, *, url_hash="h1", url="http://gone.example/a",
         domain="gone.example", domain_state=DomainState.UNREGISTERED,
         url_state=UrlState.UNCLASSIFIED, archive_url=None, pages=(1,)):
    conn.execute(
        "INSERT OR IGNORE INTO domains (registrable_domain, public_suffix, state, "
        "first_seen, wiki_page_count, wiki_link_count, url_count) VALUES (?,?,?,?,?,?,?)",
        (domain, "example", domain_state, NOW, len(pages), len(pages), 1),
    )
    did = conn.execute(
        "SELECT domain_id FROM domains WHERE registrable_domain=?", (domain,)
    ).fetchone()["domain_id"]
    conn.execute(
        "INSERT OR IGNORE INTO urls (url_hash, url_normalized, normalizer_version, "
        "domain_id, scheme, state, first_seen) VALUES (?,?,?,?,?,?,?)",
        (url_hash, url, 1, did, "http", url_state, NOW),
    )
    for page_id in pages:
        conn.execute(
            "INSERT OR IGNORE INTO wiki_pages (page_id, lang, title, ms_offset, "
            "dump_run, ingested_at) VALUES (?,?,?,?,?,?)",
            (page_id, "en", f"Article {page_id}", 570, RUN, NOW),
        )
        conn.execute(
            "INSERT OR IGNORE INTO external_links (page_id, lang, url_raw, url_hash, "
            "dump_run, first_seen, last_seen, archive_url) VALUES (?,?,?,?,?,?,?,?)",
            (page_id, "en", url, url_hash, RUN, NOW, NOW, archive_url),
        )
    return did


# -- the three answers ------------------------------------------------------


def test_a_usable_capture_is_recoverable():
    result = wayback_mod.check_url(client_returning(lambda r: snapshot()), "http://x/")
    assert result.answered and result.has_snapshot and result.recoverable


def test_an_empty_archive_is_a_real_answer_of_no():
    """`archived_snapshots: {}` is the archive telling us it has nothing. This
    is the only shape that may set has_snapshot=False."""
    result = wayback_mod.check_url(client_returning(lambda r: no_snapshot()), "http://x/")
    assert result.answered
    assert result.has_snapshot is False
    assert not result.recoverable


def test_a_transport_failure_is_unknown_not_lost():
    """The boundary this module exists to defend. A timeout must never be
    recorded as "no copy exists anywhere"."""
    def handler(request):
        raise httpx.ConnectTimeout("slow")

    result = wayback_mod.check_url(client_returning(handler), "http://x/")
    assert not result.answered
    assert result.has_snapshot is None, "a failed request was read as 'no snapshot'"


def test_a_rate_limit_is_unknown_and_carries_retry_after():
    result = wayback_mod.check_url(
        client_returning(
            lambda r: httpx.Response(429, headers={"Retry-After": "60"}, json={})
        ),
        "http://x/",
    )
    assert result.has_snapshot is None
    assert result.error_kind == "http:429"
    assert result.retry_after == 60.0


def test_malformed_json_is_unknown():
    for body in (b"<html>not json", b'"a string"', b"[1,2,3]"):
        result = wayback_mod.check_url(
            client_returning(lambda r, b=body: httpx.Response(200, content=b)),
            "http://x/",
        )
        assert result.has_snapshot is None, f"{body!r} was treated as an answer"


def test_a_server_error_stops_rather_than_hammering():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, json={})

    wayback_mod.check_url(client_returning(handler), "http://x/")
    assert calls["n"] == 1


# -- archived is not recovered ----------------------------------------------


def test_a_capture_of_a_404_recovers_nothing():
    """The Internet Archive faithfully preserves not-found pages. A snapshot
    exists, and the citation is still lost."""
    result = wayback_mod.check_url(
        client_returning(lambda r: snapshot(status="404")), "http://x/"
    )
    assert result.has_snapshot is True
    assert not result.recoverable


def test_available_false_is_no_snapshot():
    result = wayback_mod.check_url(
        client_returning(lambda r: snapshot(available=False)), "http://x/"
    )
    assert result.has_snapshot is False


def test_a_missing_status_is_trusted():
    """Some responses omit `status`. Refusing them would discard real captures."""
    body = {"archived_snapshots": {"closest": {
        "available": True, "url": "http://web.archive.org/web/1/http://x/",
        "timestamp": "20130101000000",
    }}}
    result = wayback_mod.check_url(
        client_returning(lambda r: httpx.Response(200, json=body)), "http://x/"
    )
    assert result.recoverable


# -- etiquette --------------------------------------------------------------


def test_the_snapshot_is_requested_for_the_dump_run_not_today():
    """The version Wikipedia cited is the one that matters. A capture from
    after the site died — or after the domain changed hands — is not the source
    the citation meant."""
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return snapshot()

    wayback_mod.check_url(client_returning(handler), "http://x/", timestamp="20260701")
    assert seen["timestamp"] == "20260701"


def test_a_contact_user_agent_is_required():
    """They run this for free. A tool that will not say who it is does not get
    to make the request."""
    with pytest.raises(ConfigError):
        wayback_mod.require_identity(None)
    with pytest.raises(ConfigError):
        wayback_mod.require_identity("python-httpx/0.27")
    assert wayback_mod.require_identity("wikimill/0.1.0 (+ops@example.org)")


def test_there_is_no_concurrency_knob():
    from wikimill.policy import Gaps

    assert not any("concurren" in f for f in Gaps.__dataclass_fields__)


# -- selection --------------------------------------------------------------


def test_dead_domains_are_selected_without_any_crawling(tmp_path):
    """A citation to an unregistered domain is dead by definition — the domain
    does not exist, so neither does the page. No crawl needed."""
    with open_db(tmp_path / "w.db") as conn:
        seed(conn, url_state=UrlState.UNCLASSIFIED)   # never crawled
        assert [t.url_hash for t in gaps_mod.select_targets(conn)] == ["h1"]


def test_a_url_wikipedia_already_archived_is_never_asked_about(tmp_path):
    """13.2% of citations already carry an archive URL. Asking the Internet
    Archive about those spends their capacity to learn what we were told."""
    with open_db(tmp_path / "w.db") as conn:
        seed(conn, archive_url="http://web.archive.org/web/2016/http://gone.example/a")
        assert gaps_mod.select_targets(conn) == []


def test_one_archived_citation_covers_the_url(tmp_path):
    """If any citation of a URL records an archive, a copy demonstrably exists;
    the others do not need a request to establish it."""
    with open_db(tmp_path / "w.db") as conn:
        seed(conn, pages=(1,))
        conn.execute(
            "INSERT INTO external_links (page_id, lang, url_raw, url_hash, dump_run, "
            "first_seen, last_seen, archive_url) VALUES (2,'en',?,?,?,?,?,?)",
            ("http://gone.example/a", "h1", RUN, NOW, NOW, "http://web.archive.org/x"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO wiki_pages (page_id, lang, title, ms_offset, "
            "dump_run, ingested_at) VALUES (2,'en','Article 2',570,?,?)", (RUN, NOW)
        )
        assert gaps_mod.select_targets(conn) == []


def test_live_citations_are_not_selected(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        seed(conn, domain="alive.example", domain_state=DomainState.ACTIVE,
             url_state=UrlState.LIVE, url="http://alive.example/a", url_hash="h2")
        assert gaps_mod.select_targets(conn) == []


def test_crawl_derived_dead_states_join_the_pool(tmp_path):
    """As the crawl progresses, hard_404 and dns_failure arrive without any
    change here."""
    with open_db(tmp_path / "w.db") as conn:
        seed(conn, domain="ok.example", domain_state=DomainState.ACTIVE,
             url_state=UrlState.HARD_404, url="http://ok.example/gone", url_hash="h3")
        assert [t.url_hash for t in gaps_mod.select_targets(conn)] == ["h3"]


def test_the_most_cited_url_is_asked_about_first(tmp_path):
    """A URL cited by forty articles is forty broken references. If --limit
    cuts the run short, those are the requests worth making."""
    with open_db(tmp_path / "w.db") as conn:
        seed(conn, url_hash="few", url="http://a.example/x", domain="a.example",
             pages=(1,))
        seed(conn, url_hash="many", url="http://b.example/x", domain="b.example",
             pages=(2, 3, 4))
        assert [t.url_hash for t in gaps_mod.select_targets(conn, limit=1)] == ["many"]


def test_an_answered_url_is_not_asked_again(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        seed(conn)
        conn.execute(
            "INSERT INTO archive_checks (url_hash, checked_at, has_snapshot, "
            "api_endpoint) VALUES ('h1', ?, 0, 'x')", (NOW,)
        )
        assert gaps_mod.select_targets(conn) == []
        assert len(gaps_mod.select_targets(conn, force=True)) == 1


def test_a_failed_check_leaves_the_url_in_the_queue(tmp_path):
    """"We could not ask" is not an answer, so it must not retire the URL."""
    with open_db(tmp_path / "w.db") as conn:
        seed(conn)
        conn.execute(
            "INSERT INTO archive_checks (url_hash, checked_at, has_snapshot, "
            "api_endpoint, error_kind) VALUES ('h1', ?, NULL, 'x', 'http:429')", (NOW,)
        )
        assert len(gaps_mod.select_targets(conn)) == 1


# -- the stage --------------------------------------------------------------


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKIMILL_CONTACT", "ops@example.org")
    monkeypatch.setattr("wikimill.config.repo_root", lambda: tmp_path)
    from wikimill.config import load as load_config
    return load_config()


def run_gaps(cfg, handler, **kwargs):
    from wikimill.logging import RunLog

    with RunLog("export", cfg.logs_dir, quiet=True) as log:
        return gaps_mod.run(cfg, log, client=client_returning(handler),
                            delay=0, sleep=lambda _s: None, **kwargs)


def db(cfg):
    return open_db(cfg.db_path)


def test_a_recoverable_citation_is_reported(cfg):
    with db(cfg) as conn:
        seed(conn)
    stats = run_gaps(cfg, lambda r: snapshot())
    assert stats.recoverable == 1 and stats.lost == 0


def test_a_lost_citation_is_reported(cfg):
    with db(cfg) as conn:
        seed(conn)
    stats = run_gaps(cfg, lambda r: no_snapshot())
    assert stats.lost == 1 and stats.recoverable == 0


def test_an_archived_404_counts_as_lost_and_is_called_out(cfg):
    with db(cfg) as conn:
        seed(conn)
    stats = run_gaps(cfg, lambda r: snapshot(status="404"))
    assert stats.lost == 1
    assert stats.archived_capture_unusable == 1


def test_a_failure_is_stored_as_an_error_never_as_lost(cfg):
    with db(cfg) as conn:
        seed(conn)
    stats = run_gaps(cfg, lambda r: httpx.Response(429, json={}))
    assert stats.unknown == 1 and stats.lost == 0
    with db(cfg) as conn:
        row = conn.execute("SELECT * FROM archive_checks").fetchone()
        assert row["has_snapshot"] is None
        assert row["error_kind"] == "http:429"
        assert gaps_mod.verdict(gaps_mod.latest(conn, "h1")) == gaps_mod.UNKNOWN


def test_the_human_cost_is_counted_without_double_counting(cfg):
    """One article citing two dead URLs is one affected article, not two.
    Overstating how much of Wikipedia is affected is the wrong direction."""
    with db(cfg) as conn:
        seed(conn, url_hash="h1", url="http://a.example/x", domain="a.example",
             pages=(1, 2))
        seed(conn, url_hash="h2", url="http://b.example/x", domain="b.example",
             pages=(2, 3))
    stats = run_gaps(cfg, lambda r: no_snapshot())
    assert stats.citations_affected == 4
    assert stats.articles_affected == 3


def test_observations_accumulate(cfg):
    with db(cfg) as conn:
        seed(conn)
    run_gaps(cfg, lambda r: no_snapshot())
    run_gaps(cfg, lambda r: snapshot(), force=True)
    with db(cfg) as conn:
        assert conn.execute("SELECT COUNT(*) FROM archive_checks").fetchone()[0] == 2
        assert gaps_mod.verdict(gaps_mod.latest(conn, "h1")) == gaps_mod.RECOVERABLE


def test_nothing_is_requested_when_there_is_nothing_to_ask(cfg):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return no_snapshot()

    with db(cfg) as conn:
        seed(conn, archive_url="http://web.archive.org/x")
    stats = run_gaps(cfg, handler)
    assert stats.selected == 0 and calls["n"] == 0


def test_dry_run_asks_nothing(cfg):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return no_snapshot()

    with db(cfg) as conn:
        seed(conn)
    stats = run_gaps(cfg, handler, dry_run=True)
    assert stats.selected == 1 and calls["n"] == 0
    with db(cfg) as conn:
        assert conn.execute("SELECT COUNT(*) FROM archive_checks").fetchone()[0] == 0


def test_the_stage_never_changes_a_url_or_domain_state(cfg):
    """Evidence, not a verdict — the rule every stage in this project obeys."""
    with db(cfg) as conn:
        seed(conn, url_state=UrlState.UNCLASSIFIED)
    run_gaps(cfg, lambda r: no_snapshot())
    with db(cfg) as conn:
        assert conn.execute(
            "SELECT state FROM urls WHERE url_hash='h1'"
        ).fetchone()["state"] == UrlState.UNCLASSIFIED
        assert conn.execute(
            "SELECT state FROM domains WHERE registrable_domain='gone.example'"
        ).fetchone()["state"] == DomainState.UNREGISTERED


def test_the_stage_reports_liveness(cfg):
    """It is a long serial network stage, so it must be watchable like the
    others."""
    import contextlib

    from wikimill.progress import open_progress_db, snapshot as progress_snapshot

    with db(cfg) as conn:
        seed(conn)
    run_gaps(cfg, lambda r: no_snapshot())
    with contextlib.closing(open_progress_db(cfg.state_dir)) as beat:
        views = progress_snapshot(beat)
    assert any(v.stage == "gaps" and v.outcome == "ok" for v in views)


# -- backpressure -----------------------------------------------------------


def test_repeated_refusals_stop_the_run(cfg):
    """archive.org rate-limits this endpoint hard — the first live run drew a
    429 on request one. Continuing to ask while a service says "slow down" is
    both rude and pointless, since every answer would be an error anyway."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429, json={})

    with db(cfg) as conn:
        for i in range(30):
            seed(conn, url_hash=f"h{i}", url=f"http://d{i}.example/x",
                 domain=f"d{i}.example", pages=(i + 1,))

    stats = run_gaps(cfg, handler)
    assert stats.circuit_tripped
    assert calls["n"] == gaps_mod.CIRCUIT_THRESHOLD, "kept asking after being refused"
    assert stats.lost == 0, "a refused request was recorded as lost"


def test_the_unasked_urls_stay_queued_after_a_trip(cfg):
    """Stopping early must not retire the work — that is the whole reason it is
    safe to stop."""
    with db(cfg) as conn:
        for i in range(30):
            seed(conn, url_hash=f"h{i}", url=f"http://d{i}.example/x",
                 domain=f"d{i}.example", pages=(i + 1,))

    run_gaps(cfg, lambda r: httpx.Response(429, json={}))
    with db(cfg) as conn:
        remaining = gaps_mod.select_targets(conn)
    assert len(remaining) >= 30 - gaps_mod.CIRCUIT_THRESHOLD


def test_a_success_resets_the_breaker(cfg):
    """Occasional refusals in a long run are normal and must not accumulate
    into a trip across unrelated URLs."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        # Fail, succeed, fail, succeed … never 5 in a row.
        return httpx.Response(429, json={}) if calls["n"] % 2 else no_snapshot()

    with db(cfg) as conn:
        for i in range(12):
            seed(conn, url_hash=f"h{i}", url=f"http://d{i}.example/x",
                 domain=f"d{i}.example", pages=(i + 1,))

    stats = run_gaps(cfg, handler)
    assert not stats.circuit_tripped
    assert stats.lost > 0 and stats.unknown > 0

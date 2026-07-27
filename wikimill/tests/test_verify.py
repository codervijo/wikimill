"""Live-wiki verification (v2.F).

Two things are being protected here, and they pull in different directions.

**Correctness.** The Action API answers rate limits and replication lag with
HTTP 200 and an `error` object in the body. A client that only checks status
codes records "0 articles link here" for what was actually "we were not allowed
to ask" — a false, maximally confident signal about the strongest claim the
export makes. Several tests exist only to pin that distinction.

**Etiquette.** This is the one stage that talks to Wikimedia rather than reading
their dumps, and the things that protect them — serial requests, `maxlag`, a
contact User-Agent, obeying `Retry-After` — are asserted here rather than left
to reviewers to notice.

Every test is hermetic: `httpx.MockTransport` throughout, so the suite never
sends a packet to en.wikipedia.org.
"""

from __future__ import annotations

import httpx
import pytest

from wikimill import verify as verify_mod
from wikimill.constants import DomainState
from wikimill.errors import ConfigError
from wikimill.storage import open_db
from wikimill.wiki import usage as usage_mod

NOW = "2026-07-25T00:00:00+00:00"


def client_returning(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok_response(titles, continue_token=None):
    body = {"query": {"exturlusage": [{"title": t} for t in titles]}}
    if continue_token:
        body["continue"] = continue_token
    return httpx.Response(200, json=body)


def domain(conn, name, *, state=DomainState.UNREGISTERED, pages=3):
    conn.execute(
        "INSERT INTO domains (registrable_domain, public_suffix, state, first_seen, "
        "wiki_page_count, wiki_link_count, url_count) VALUES (?,?,?,?,?,?,?)",
        (name, "com", state, NOW, pages, pages, 1),
    )
    return conn.execute(
        "SELECT domain_id FROM domains WHERE registrable_domain=?", (name,)
    ).fetchone()["domain_id"]


# -- counting ---------------------------------------------------------------


def test_counts_distinct_live_articles():
    client = client_returning(lambda r: ok_response(["Alpha", "Beta", "Gamma"]))
    result = usage_mod.check_domain(client, "example.com")
    assert result.ok and result.live_page_count == 3


def test_no_results_means_the_wiki_no_longer_links_there():
    client = client_returning(lambda r: ok_response([]))
    result = usage_mod.check_domain(client, "gone.example")
    assert result.ok and result.live_page_count == 0


def test_the_same_article_twice_counts_once():
    """`exturlusage` returns one row per link occurrence, so an article citing a
    domain three times appears three times. The export's claim is *distinct
    pages*, and conflating the two would inflate every count."""
    client = client_returning(lambda r: ok_response(["Alpha", "Alpha", "Beta"]))
    assert usage_mod.check_domain(client, "example.com").live_page_count == 2


def test_pagination_is_followed():
    pages = [
        ok_response(["A", "B"], {"eucontinue": "1", "continue": "-||"}),
        ok_response(["C"]),
    ]
    calls = {"n": 0}

    def handler(request):
        response = pages[calls["n"]]
        calls["n"] += 1
        return response

    result = usage_mod.check_domain(client_returning(handler), "example.com")
    assert result.live_page_count == 3
    assert not result.truncated


def test_a_widely_cited_domain_is_truncated_not_guessed():
    """A domain cited by tens of thousands of articles is not an acquisition
    candidate, so an exact count buys nothing — but the number must be marked
    as a floor rather than silently presented as a total."""
    client = client_returning(
        lambda r: ok_response(["A"], {"eucontinue": "next", "continue": "-||"})
    )
    result = usage_mod.check_domain(client, "bbc.co.uk")
    assert result.truncated


# -- errors must never look like zero ---------------------------------------


def test_an_api_error_body_is_not_a_zero_count():
    """The core correctness guard. HTTP 200 + {"error": ...} is how the Action
    API reports rate limits and lag. Reading that as "nothing links here" would
    invent the strongest possible negative signal out of a refusal to answer."""
    client = client_returning(
        lambda r: httpx.Response(200, json={"error": {"code": "maxlag",
                                                      "info": "Waiting for a DB"}})
    )
    result = usage_mod.check_domain(client, "example.com")
    assert not result.ok
    assert result.live_page_count is None
    assert result.error_kind == "api:maxlag"


def test_a_transport_failure_is_not_a_zero_count():
    def handler(request):
        raise httpx.ConnectError("no route")

    result = usage_mod.check_domain(client_returning(handler), "example.com")
    assert not result.ok and result.live_page_count is None


def test_malformed_json_is_not_a_zero_count():
    client = client_returning(lambda r: httpx.Response(200, content=b"<html>"))
    result = usage_mod.check_domain(client, "example.com")
    assert result.error_kind == "malformed_json"
    assert result.live_page_count is None


def test_a_429_is_recorded_with_its_retry_after():
    client = client_returning(
        lambda r: httpx.Response(429, headers={"Retry-After": "30"}, json={})
    )
    result = usage_mod.check_domain(client, "example.com")
    assert result.error_kind == "http:429"
    assert result.retry_after == 30.0


def test_a_server_error_stops_rather_than_hammering():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, json={})

    usage_mod.check_domain(client_returning(handler), "example.com")
    assert calls["n"] == 1, "kept requesting from a struggling server"


# -- etiquette --------------------------------------------------------------


def test_every_request_carries_maxlag():
    """The canonical good-citizen parameter: it lets Wikimedia refuse us when
    replication is behind, instead of us adding load to a struggling cluster."""
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return ok_response([])

    usage_mod.check_domain(client_returning(handler), "example.com")
    assert seen["maxlag"] == str(usage_mod.DEFAULT_MAXLAG)


def test_queries_are_scoped_to_articles():
    """Namespace 0 only — the same slice ingest keeps, so the live number is
    comparable with the dump number rather than counting talk pages too."""
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return ok_response([])

    usage_mod.check_domain(client_returning(handler), "example.com")
    assert seen["eunamespace"] == "0"


def test_a_contact_user_agent_is_required():
    """Wikimedia's User-Agent policy asks for real contact information. A tool
    that quietly degrades to a generic string is the client that policy exists
    to keep out."""
    with pytest.raises(ConfigError):
        usage_mod.require_identity(None)
    with pytest.raises(ConfigError):
        usage_mod.require_identity("python-httpx/0.27")
    assert usage_mod.require_identity("wikimill/0.1.0 (+ops@example.org)")


def test_there_is_no_concurrency_knob():
    """v2.I parallelised domain checks because registries are many independent
    operators. This is one operator's shared cluster, which asks bots to run
    series of requests serially — so the absence of a knob is the design."""
    from wikimill.policy import Verify

    assert not any("concurren" in f for f in Verify.__dataclass_fields__)


# -- the stage --------------------------------------------------------------


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKIMILL_CONTACT", "ops@example.org")
    monkeypatch.setattr("wikimill.config.repo_root", lambda: tmp_path)
    from wikimill.config import load as load_config
    return load_config()


def run_verify(cfg, handler, **kwargs):
    from wikimill.logging import RunLog

    with RunLog("export", cfg.logs_dir, quiet=True) as log:
        return verify_mod.run(
            cfg, log,
            states=[DomainState.UNREGISTERED],
            min_pages=1,
            client=client_returning(handler),
            delay=0,
            sleep=lambda _s: None,
            **kwargs,
        )


def test_a_domain_that_lost_citations_is_reported(cfg, tmp_path):
    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        domain(conn, "fading.com", pages=8)

    stats = run_verify(cfg, lambda r: ok_response(["A", "B", "C"]))
    assert stats.reduced == 1
    assert stats.citations_lost == 5


def test_a_domain_the_wiki_no_longer_cites_is_called_out(cfg, tmp_path):
    """The headline case: the export was about to claim 8 citations that no
    longer exist."""
    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        domain(conn, "gone.com", pages=8)

    stats = run_verify(cfg, lambda r: ok_response([]))
    assert stats.vanished == 1 and stats.reduced == 0


def test_an_unchanged_domain_is_confirmed(cfg, tmp_path):
    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        domain(conn, "steady.com", pages=2)

    stats = run_verify(cfg, lambda r: ok_response(["A", "B"]))
    assert stats.unchanged == 1 and stats.citations_lost == 0


def test_a_live_count_above_the_dump_count_is_not_a_signal(cfg, tmp_path):
    """The ingest is a slice of enwiki while the API counts all of it, so
    `live > dump` is the expected case and says nothing about whether editors
    kept or dropped anything. It must never be recorded as loss, and must never
    raise or lower an acquisition score.

    Measured on the real corpus: a 27,152-page slice, and every verified domain
    came back with a far larger live count.
    """
    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        did = domain(conn, "growing.com", pages=1)

    stats = run_verify(cfg, lambda r: ok_response(["A", "B", "C"]))
    assert stats.beyond_slice == 1 and stats.citations_lost == 0
    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        assert verify_mod.citations_lost(conn, did) == 0


def test_the_loss_figure_is_a_lower_bound_never_an_invention(cfg, tmp_path):
    """The asymmetry is what makes this safe. The dump count is drawn from a
    subset of the wiki, so `dump > live` can only happen when the wiki really
    did shrink — the reported loss under-states the true one and cannot
    fabricate it.

    Here: the slice saw 6, the whole live wiki now sees 2. The true loss is at
    least 4, and 4 is what gets recorded.
    """
    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        did = domain(conn, "shrinking.com", pages=6)

    run_verify(cfg, lambda r: ok_response(["A", "B"]))
    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        row = verify_mod.latest(conn, did)
        assert verify_mod.citations_lost(conn, did) == 4
        assert row["dump_page_count"] - row["live_page_count"] == 4


def test_a_failed_check_is_stored_as_an_error_not_a_zero(cfg, tmp_path):
    """"We could not ask" must never later read as "the answer was zero"."""
    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        did = domain(conn, "unknown.com", pages=5)

    stats = run_verify(
        cfg, lambda r: httpx.Response(200, json={"error": {"code": "maxlag"}})
    )
    assert stats.failed == 1 and stats.vanished == 0
    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        row = conn.execute("SELECT * FROM wiki_usage_checks").fetchone()
        assert row["error_kind"] == "api:maxlag"
        assert row["live_page_count"] is None
        assert verify_mod.citations_lost(conn, did) == 0


def test_a_truncated_count_never_feeds_the_loss_figure(cfg, tmp_path):
    """A truncated count is a floor, so it can only understate the live side —
    which means it could overstate the loss. It is excluded entirely."""
    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        did = domain(conn, "bbc.co.uk", pages=900)

    stats = run_verify(
        cfg, lambda r: ok_response(["A"], {"eucontinue": "x", "continue": "-||"})
    )
    assert stats.truncated == 1 and stats.citations_lost == 0
    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        assert verify_mod.citations_lost(conn, did) == 0


def test_observations_accumulate_rather_than_overwrite(cfg, tmp_path):
    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        domain(conn, "fading.com", pages=8)

    run_verify(cfg, lambda r: ok_response(["A", "B"]))
    run_verify(cfg, lambda r: ok_response(["A"]))
    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM wiki_usage_checks"
        ).fetchone()[0] == 2


def test_verification_never_changes_a_domain_state(cfg, tmp_path):
    """Evidence, not a verdict — the same rule the crawler and the diff obey."""
    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        did = domain(conn, "gone.com", state=DomainState.ACTIVE, pages=8)

    run_verify(cfg, lambda r: ok_response([]))
    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        assert conn.execute(
            "SELECT state FROM domains WHERE domain_id=?", (did,)
        ).fetchone()["state"] == DomainState.ACTIVE


def test_nothing_is_requested_when_there_are_no_candidates(cfg, tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return ok_response([])

    with open_db(tmp_path / "state" / "wikimill.db"):
        pass
    stats = run_verify(cfg, handler)
    assert stats.selected == 0 and calls["n"] == 0


# -- scoring and export -----------------------------------------------------


def test_lost_citations_raise_the_score(cfg, tmp_path):
    from wikimill import score as score_mod

    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        did = domain(conn, "fading.com", pages=8)
        row = conn.execute(
            "SELECT * FROM domains WHERE domain_id=?", (did,)
        ).fetchone()
        before = score_mod.score_domain(row, *score_mod.evidence_for(conn, did)).total

    run_verify(cfg, lambda r: ok_response(["A"]))
    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        row = conn.execute("SELECT * FROM domains WHERE domain_id=?", (did,)).fetchone()
        after = score_mod.score_domain(row, *score_mod.evidence_for(conn, did)).total
    assert after > before


def test_dump_diff_and_live_check_do_not_double_count(cfg, tmp_path):
    """v2.G and v2.F measure the same phenomenon by different means. The
    dump-to-dump window sits inside the dump-to-now window, so summing them
    would move a domain up the shortlist twice for one removal."""
    from wikimill import diff as diff_mod
    from wikimill import score as score_mod

    db = tmp_path / "state" / "wikimill.db"
    with open_db(db) as conn:
        did = domain(conn, "fading.com", pages=8)
        for run in ("20260601", "20260701"):
            conn.execute(
                "INSERT INTO wiki_pages (page_id, lang, title, ms_offset, dump_run, "
                "ingested_at) VALUES (1,'en','A',1,?,?)", (run, NOW)
            )
        conn.execute(
            "INSERT INTO urls (url_hash, url_normalized, normalizer_version, "
            "domain_id, scheme, state, first_seen) VALUES "
            "('h','http://fading.com/a',1,?,'http','dns_failure',?)", (did, NOW)
        )
        conn.execute(
            "INSERT INTO external_links (page_id, lang, url_raw, url_hash, dump_run, "
            "first_seen, last_seen) VALUES (1,'en','http://fading.com/a','h',"
            "'20260601',?,?)", (NOW, NOW)
        )
        diff_mod.compute(conn, "20260601", "20260701")
        assert diff_mod.removal_counts(conn, did) == 1

    run_verify(cfg, lambda r: ok_response(["A", "B", "C", "D", "E", "F", "G"]))
    with open_db(db) as conn:
        assert verify_mod.citations_lost(conn, did) == 1
        _states, kinds = score_mod.evidence_for(conn, did)
        # max(1, 1) — not 2.
        assert kinds["__wiki_removed__"] == 1


def test_the_export_shows_the_live_count_beside_the_snapshot(cfg, tmp_path):
    from wikimill import export as export_mod

    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        domain(conn, "fading.com", pages=8)

    run_verify(cfg, lambda r: ok_response(["A", "B"]))
    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        record = export_mod.collect(conn, [DomainState.UNREGISTERED], 1)[0]
    assert record["wiki_pages"] == 8
    assert record["wiki_pages_live"] == 2
    assert record["wiki_verified_at"]


def test_an_unverified_domain_leaves_the_column_blank(cfg, tmp_path):
    """Blank means "not checked"; a zero would mean "checked, nothing links
    here". Opposite claims about the strongest evidence in the file."""
    from wikimill import export as export_mod

    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        domain(conn, "unasked.com", pages=4)
        record = export_mod.collect(conn, [DomainState.UNREGISTERED], 1)[0]
    assert record["wiki_pages_live"] == ""
    assert record["wiki_verified_at"] == ""


def test_a_truncated_count_is_marked_as_a_floor_in_the_export(cfg, tmp_path):
    from wikimill import export as export_mod

    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        domain(conn, "bbc.co.uk", pages=900)

    run_verify(
        cfg, lambda r: ok_response(["A"], {"eucontinue": "x", "continue": "-||"})
    )
    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        record = export_mod.collect(conn, [DomainState.UNREGISTERED], 1)[0]
    assert str(record["wiki_pages_live"]).endswith("+")


def test_export_without_verify_makes_no_requests(tmp_path, monkeypatch):
    """`--verify` is opt-in and never implied. A plain export stays offline and
    deterministic, which is what its digest is a digest of."""
    from typer.testing import CliRunner

    from wikimill.cli import app

    monkeypatch.setenv("WIKIMILL_CONTACT", "ops@example.org")
    monkeypatch.setattr("wikimill.config.repo_root", lambda: tmp_path)

    def explode(*args, **kwargs):
        raise AssertionError("export made a network request without --verify")

    monkeypatch.setattr("wikimill.verify.run", explode)
    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        domain(conn, "example.com", pages=2)

    result = CliRunner().invoke(app, ["export", "--format", "jsonl"])
    assert result.exit_code == 0

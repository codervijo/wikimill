"""Scoring, inspection, and the candidate export.

The export is the deliverable, so two of its properties get the most attention:
it must be **deterministic** (so two exports diff meaningfully) and it must be
**attributable** (anchor text is CC BY-SA content).
"""

from __future__ import annotations

import json

import pytest

from wikimill import export as export_mod
from wikimill import inspect as inspect_mod
from wikimill import score as score_mod
from wikimill.constants import DomainState, UrlState
from wikimill.storage import open_db

NOW = "2026-07-25T00:00:00+00:00"


def build(conn, *, domain="gone-example.com", state=DomainState.UNREGISTERED,
          pages=3, url_state=UrlState.DNS_FAILURE, enriched=True, private=0):
    conn.execute(
        "INSERT INTO domains (registrable_domain, public_suffix, is_private_suffix, "
        "state, first_seen, last_checked, wiki_page_count, wiki_link_count, url_count) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (domain, "example", private, state, NOW, NOW, pages, pages, 1),
    )
    domain_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO urls (url_hash, url_normalized, normalizer_version, domain_id, "
        "scheme, state, first_seen) VALUES (?,?,?,?,?,?,?)",
        (f"h-{domain}", f"http://{domain}/a", 1, domain_id, "http", url_state, NOW),
    )
    conn.execute(
        "INSERT INTO wiki_pages (page_id, lang, title, ms_offset, dump_run, ingested_at)"
        " VALUES (?,?,?,?,?,?)",
        (hash(domain) % 100000, "en", f"Article about {domain}", 570, "20260701", NOW),
    )
    page_id = hash(domain) % 100000
    conn.execute(
        "INSERT INTO external_links (page_id, lang, url_raw, url_hash, dump_run, "
        "first_seen, last_seen, section, anchor_text, link_kind, enrich_status, "
        "dead_link_tagged) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (page_id, "en", f"http://{domain}/a", f"h-{domain}", "20260701", NOW, NOW,
         "History" if enriched else None,
         "The Source" if enriched else None,
         "citation" if enriched else None,
         "done" if enriched else "pending",
         1 if enriched else 0),
    )
    return domain_id


# -- scoring ----------------------------------------------------------------


def test_unregistered_outranks_active(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        build(conn, domain="gone-example.com", state=DomainState.UNREGISTERED)
        build(conn, domain="live-example.com", state=DomainState.ACTIVE,
              url_state=UrlState.LIVE)
        score_mod.rescore_all(conn)
        rows = {
            r["registrable_domain"]: r["candidate_score"]
            for r in conn.execute("SELECT registrable_domain, candidate_score FROM domains")
        }
    assert rows["gone-example.com"] > rows["live-example.com"]


def test_more_citing_pages_scores_higher(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        build(conn, domain="many-example.com", pages=9)
        build(conn, domain="few-example.com", pages=1)
        score_mod.rescore_all(conn)
        rows = {
            r["registrable_domain"]: r["candidate_score"]
            for r in conn.execute("SELECT registrable_domain, candidate_score FROM domains")
        }
    assert rows["many-example.com"] > rows["few-example.com"]


def test_citation_weight_is_capped(tmp_path):
    """A domain cited by 500 articles is not 50× one cited by 10."""
    with open_db(tmp_path / "w.db") as conn:
        build(conn, domain="huge-example.com", pages=500)
        score_mod.rescore_all(conn)
        row = conn.execute("SELECT score_explanation FROM domains").fetchone()
    parsed = json.loads(row["score_explanation"])
    citations = next(c for c in parsed["components"] if c["name"] == "citations")
    assert citations["points"] == score_mod.CITATION_POINTS_CAP


def test_private_suffix_penalises_but_does_not_exclude(tmp_path):
    """The PSL cannot tell blogspot.com from poznan.pl, so the uncertainty is
    priced in rather than resolved by guessing."""
    with open_db(tmp_path / "w.db") as conn:
        build(conn, domain="a.blogspot.com", private=1)
        score_mod.rescore_all(conn)
        row = conn.execute("SELECT candidate_score, score_explanation FROM domains").fetchone()
    parsed = json.loads(row["score_explanation"])
    assert any(c["points"] < 0 for c in parsed["components"])
    assert row["candidate_score"] is not None  # still scored, still listable


def test_every_score_explains_itself(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        build(conn)
        score_mod.rescore_all(conn)
        row = conn.execute("SELECT score_explanation FROM domains").fetchone()
    parsed = json.loads(row["score_explanation"])
    assert parsed["components"]
    for component in parsed["components"]:
        assert component["detail"]


def test_rescoring_is_idempotent(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        build(conn)
        score_mod.rescore_all(conn)
        first = conn.execute("SELECT candidate_score FROM domains").fetchone()[0]
        score_mod.rescore_all(conn)
        second = conn.execute("SELECT candidate_score FROM domains").fetchone()[0]
    assert first == second


# -- export -----------------------------------------------------------------


def test_export_writes_candidates(tmp_path):
    out = tmp_path / "candidates.csv"
    with open_db(tmp_path / "w.db") as conn:
        build(conn)
        score_mod.rescore_all(conn)
        stats = export_mod.write(conn, out, states=[DomainState.UNREGISTERED],
                                 min_pages=1, fmt="csv", when=NOW)
    assert stats.rows == 1
    assert "gone-example.com" in out.read_text()


def test_digest_is_stable_across_runs_at_different_times(tmp_path):
    """Acceptance criterion 19. Regression: the digest once covered the whole
    file including the generated-at header, so it changed on every run even when
    nothing about the findings had — which is the one question it exists to
    answer. This test deliberately does NOT pin the timestamp; the earlier one
    did, and so missed the bug entirely."""
    with open_db(tmp_path / "w.db") as conn:
        build(conn)
        score_mod.rescore_all(conn)
        a = export_mod.write(conn, tmp_path / "a.csv", states=[DomainState.UNREGISTERED],
                             min_pages=1, fmt="csv", when="2026-07-25T00:00:00+00:00")
        b = export_mod.write(conn, tmp_path / "b.csv", states=[DomainState.UNREGISTERED],
                             min_pages=1, fmt="csv", when="2026-08-01T12:34:56+00:00")
    assert a.sha256 == b.sha256, "same data at a different time must hash the same"


def test_data_is_byte_identical_apart_from_the_header(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        build(conn)
        score_mod.rescore_all(conn)
        export_mod.write(conn, tmp_path / "a.csv", states=[DomainState.UNREGISTERED],
                         min_pages=1, fmt="csv", when="2026-07-25T00:00:00+00:00")
        export_mod.write(conn, tmp_path / "b.csv", states=[DomainState.UNREGISTERED],
                         min_pages=1, fmt="csv", when="2026-08-01T12:34:56+00:00")
    strip = lambda p: [l for l in p.read_text().splitlines() if not l.startswith("#")]
    assert strip(tmp_path / "a.csv") == strip(tmp_path / "b.csv")


def test_changed_data_changes_the_digest(tmp_path):
    """The digest must still react to what it is supposed to track."""
    with open_db(tmp_path / "w.db") as conn:
        build(conn)
        score_mod.rescore_all(conn)
        before = export_mod.write(conn, tmp_path / "a.csv",
                                  states=[DomainState.UNREGISTERED], min_pages=1,
                                  fmt="csv", when=NOW)
        build(conn, domain="second-example.com")
        score_mod.rescore_all(conn)
        after = export_mod.write(conn, tmp_path / "b.csv",
                                 states=[DomainState.UNREGISTERED], min_pages=1,
                                 fmt="csv", when=NOW)
    assert before.sha256 != after.sha256


def test_ordering_is_deterministic_not_insertion_order(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        for name in ("zeta-example.com", "alpha-example.com", "mid-example.com"):
            build(conn, domain=name, pages=2)
        score_mod.rescore_all(conn)
        rows = export_mod.collect(conn, [DomainState.UNREGISTERED], 1)
    names = [r["domain"] for r in rows]
    assert names == sorted(names)  # equal scores fall back to the name


def test_export_carries_the_licence_and_attribution(tmp_path):
    """Anchor text and section names are CC BY-SA excerpts, so an export is
    attributable by construction."""
    out = tmp_path / "c.csv"
    with open_db(tmp_path / "w.db") as conn:
        build(conn)
        score_mod.rescore_all(conn)
        export_mod.write(conn, out, states=[DomainState.UNREGISTERED], min_pages=1,
                         fmt="csv", when=NOW)
    body = out.read_text()
    assert "CC BY-SA 4.0" in body
    assert "https://en.wikipedia.org/wiki/Article_about_gone-example.com" in body


def test_row_carries_the_evidence_that_makes_it_actionable(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        build(conn)
        score_mod.rescore_all(conn)
        rows = export_mod.collect(conn, [DomainState.UNREGISTERED], 1)
    row = rows[0]
    assert row["example_section"] == "History"
    assert row["example_anchor"] == "The Source"
    assert row["example_link_kind"] == "citation"
    assert row["dead_link_tagged"] == "yes"


def test_min_pages_filters(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        build(conn, domain="thin-example.com", pages=1)
        score_mod.rescore_all(conn)
        assert export_mod.collect(conn, [DomainState.UNREGISTERED], 5) == []


def test_active_domains_are_not_candidates_by_default():
    assert DomainState.ACTIVE not in export_mod.DEFAULT_STATES
    assert DomainState.UNREGISTERED in export_mod.DEFAULT_STATES


def test_jsonl_format_is_parseable(tmp_path):
    out = tmp_path / "c.jsonl"
    with open_db(tmp_path / "w.db") as conn:
        build(conn)
        score_mod.rescore_all(conn)
        export_mod.write(conn, out, states=[DomainState.UNREGISTERED], min_pages=1,
                         fmt="jsonl", when=NOW)
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert "_meta" in lines[0]
    assert lines[0]["_meta"]["licence"] == "CC BY-SA 4.0"
    assert lines[1]["domain"] == "gone-example.com"


def test_no_blank_field_is_ever_guessed(tmp_path):
    """Acceptance criterion 15: an unknown stays blank, never invented."""
    with open_db(tmp_path / "w.db") as conn:
        build(conn, enriched=False)
        score_mod.rescore_all(conn)
        rows = export_mod.collect(conn, [DomainState.UNREGISTERED], 1)
    assert rows[0]["example_section"] == ""
    assert rows[0]["example_anchor"] == ""
    assert rows[0]["registrar"] == ""


def test_export_is_recorded(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        build(conn)
        score_mod.rescore_all(conn)
        export_mod.write(conn, tmp_path / "c.csv", states=[DomainState.UNREGISTERED],
                         min_pages=1, fmt="csv", when=NOW)
        row = conn.execute("SELECT row_count, sha256 FROM exports").fetchone()
    assert row["row_count"] == 1 and row["sha256"]


# -- inspect ----------------------------------------------------------------


def test_inspect_finds_a_domain(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        build(conn)
        score_mod.rescore_all(conn)
        report = inspect_mod.gather(conn, "gone-example.com")
    assert report.kind == "domain"
    assert report.domain["state"] == DomainState.UNREGISTERED
    assert report.citations and report.citations[0]["section"] == "History"


def test_inspect_accepts_a_url_and_falls_back_to_its_domain(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        build(conn)
        report = inspect_mod.gather(conn, "http://gone-example.com/a")
    assert report.found


def test_url_on_an_unlisted_tld_yields_no_domain(tmp_path):
    """`.example` is reserved by RFC 2606 but absent from the PSL, so no
    registrable domain can be derived. Reporting that beats inventing one."""
    with open_db(tmp_path / "w.db") as conn:
        report = inspect_mod.gather(conn, "http://something.example/a")
    assert not report.found


def test_inspect_reports_unknown_rather_than_inventing(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        report = inspect_mod.gather(conn, "never-seen-example.com")
    assert not report.found


def test_inspect_shows_the_score_breakdown(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        build(conn)
        score_mod.rescore_all(conn)
        report = inspect_mod.gather(conn, "gone-example.com")
    assert report.score["components"]

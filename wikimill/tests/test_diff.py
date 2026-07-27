"""Cross-dump-run diff (v2.G, prd.md §8).

The claim being tested is narrow and worth stating precisely: **a link an editor
removed between two dumps is corroboration that the link died.** Everything here
protects that claim from the ways it can be faked.

The dangerous failure is not missing a removal — it is inventing one. This tool
ingests slices, so a page absent from the newer run may simply never have been
ingested, and treating that as "an editor removed 40 citations" would fabricate
exactly the high-confidence false positive the operator would act on. Most of
these tests exist to prove that cannot happen.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from wikimill import diff as diff_mod
from wikimill import score as score_mod
from wikimill.cli import app
from wikimill.constants import DomainState, UrlState
from wikimill.storage import open_db

NOW = "2026-07-25T00:00:00+00:00"
OLD, NEW = "20260601", "20260701"
runner = CliRunner()


def page(conn, page_id, run, *, title=None):
    conn.execute(
        "INSERT OR IGNORE INTO wiki_pages (page_id, lang, title, ms_offset, dump_run, "
        "ingested_at) VALUES (?,?,?,?,?,?)",
        (page_id, "en", title or f"Article {page_id}", 100, run, NOW),
    )


def domain(conn, name, *, state=DomainState.UNKNOWN, pages=1):
    conn.execute(
        "INSERT OR IGNORE INTO domains (registrable_domain, public_suffix, state, "
        "first_seen, wiki_page_count, wiki_link_count, url_count) VALUES (?,?,?,?,?,?,?)",
        (name, "com", state, NOW, pages, pages, 1),
    )
    return conn.execute(
        "SELECT domain_id FROM domains WHERE registrable_domain=?", (name,)
    ).fetchone()["domain_id"]


def link(conn, page_id, url_hash, run, *, domain_id=None, state=UrlState.UNCLASSIFIED):
    conn.execute(
        "INSERT OR IGNORE INTO urls (url_hash, url_normalized, normalizer_version, "
        "domain_id, scheme, state, first_seen) VALUES (?,?,?,?,?,?,?)",
        (url_hash, f"http://{url_hash}.example/", 1, domain_id, "http", state, NOW),
    )
    conn.execute(
        "INSERT OR IGNORE INTO external_links (page_id, lang, url_raw, url_hash, "
        "dump_run, first_seen, last_seen) VALUES (?,?,?,?,?,?,?)",
        (page_id, "en", f"http://{url_hash}.example/", url_hash, run, NOW, NOW),
    )


def transitions(conn, from_run=OLD, to_run=NEW):
    return {
        (r["url_hash"], r["transition"])
        for r in conn.execute(
            "SELECT url_hash, transition FROM link_diffs WHERE from_run=? AND to_run=?",
            (from_run, to_run),
        )
    }


# -- the signal -------------------------------------------------------------


def test_a_link_dropped_by_an_editor_is_recorded_as_removed(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        page(conn, 1, OLD)
        page(conn, 1, NEW)
        link(conn, 1, "gone", OLD)

        stats = diff_mod.compute(conn, OLD, NEW)
        assert stats.removed == 1 and stats.added == 0
        assert transitions(conn) == {("gone", diff_mod.REMOVED)}


def test_a_new_citation_is_recorded_as_added(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        page(conn, 1, OLD)
        page(conn, 1, NEW)
        link(conn, 1, "fresh", NEW)

        stats = diff_mod.compute(conn, OLD, NEW)
        assert stats.added == 1 and stats.removed == 0
        assert transitions(conn) == {("fresh", diff_mod.ADDED)}


def test_a_surviving_link_produces_no_transition(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        page(conn, 1, OLD)
        page(conn, 1, NEW)
        link(conn, 1, "stable", OLD)
        link(conn, 1, "stable", NEW)

        stats = diff_mod.compute(conn, OLD, NEW)
        assert (stats.removed, stats.added) == (0, 0)
        assert transitions(conn) == set()


def test_the_same_url_can_move_independently_on_different_pages(tmp_path):
    """Removal is per (page, url): one article dropping a source says nothing
    about another article that kept it."""
    with open_db(tmp_path / "w.db") as conn:
        for pid in (1, 2):
            page(conn, pid, OLD)
            page(conn, pid, NEW)
        link(conn, 1, "src", OLD)               # dropped by page 1
        link(conn, 2, "src", OLD)
        link(conn, 2, "src", NEW)               # kept by page 2

        stats = diff_mod.compute(conn, OLD, NEW)
        assert stats.removed == 1


# -- what must NOT become a signal ------------------------------------------


def test_a_page_missing_from_the_newer_run_is_never_a_removal(tmp_path):
    """The core guard. This tool ingests slices, so a page absent from the newer
    run may have been deleted OR never ingested — indistinguishable from here,
    and opposite in meaning. Comparing anyway would manufacture a removal per
    link on every page an operator chose not to ingest."""
    with open_db(tmp_path / "w.db") as conn:
        page(conn, 1, OLD)          # present only in the older run
        for h in ("a", "b", "c"):
            link(conn, 1, h, OLD)
        page(conn, 2, OLD)
        page(conn, 2, NEW)          # one genuinely comparable page

        stats = diff_mod.compute(conn, OLD, NEW)
        assert stats.removed == 0, "links on an un-ingested page were counted as removed"
        assert stats.pages_compared == 1
        assert stats.pages_not_comparable == 1


def test_a_page_only_in_the_newer_run_is_never_an_addition(tmp_path):
    """Symmetric: a newly-ingested slice is not thousands of editors adding
    citations on the same day."""
    with open_db(tmp_path / "w.db") as conn:
        page(conn, 9, NEW)
        link(conn, 9, "x", NEW)
        page(conn, 1, OLD)
        page(conn, 1, NEW)

        stats = diff_mod.compute(conn, OLD, NEW)
        assert stats.added == 0


def test_no_overlap_at_all_reports_nothing_comparable(tmp_path):
    """Two disjoint slices are not a diff. Reporting zero removals here is
    correct; reporting *every* link as removed would be catastrophic."""
    with open_db(tmp_path / "w.db") as conn:
        page(conn, 1, OLD)
        link(conn, 1, "a", OLD)
        page(conn, 2, NEW)
        link(conn, 2, "b", NEW)

        stats = diff_mod.compute(conn, OLD, NEW)
        assert not stats.comparable
        assert (stats.removed, stats.added) == (0, 0)
        assert transitions(conn) == set()


def test_page_deleted_is_not_a_transition_type(tmp_path):
    """Deliberately absent, not forgotten — see the module docstring."""
    with open_db(tmp_path / "w.db") as conn:
        page(conn, 1, OLD)
        link(conn, 1, "a", OLD)
        page(conn, 2, OLD)
        page(conn, 2, NEW)
        diff_mod.compute(conn, OLD, NEW)
        kinds = {
            r["transition"] for r in conn.execute("SELECT transition FROM link_diffs")
        }
        assert kinds <= {diff_mod.REMOVED, diff_mod.ADDED}


def test_diffing_a_run_against_itself_is_a_no_op(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        page(conn, 1, NEW)
        link(conn, 1, "a", NEW)
        stats = diff_mod.compute(conn, NEW, NEW)
        assert (stats.removed, stats.added) == (0, 0)


# -- idempotency ------------------------------------------------------------


def test_recomputing_the_same_pair_changes_nothing(tmp_path):
    """Append-only observation tables: a second comparison of the same two runs
    must not double the evidence a domain is scored on."""
    with open_db(tmp_path / "w.db") as conn:
        page(conn, 1, OLD)
        page(conn, 1, NEW)
        link(conn, 1, "gone", OLD)

        diff_mod.compute(conn, OLD, NEW)
        again = diff_mod.compute(conn, OLD, NEW)
        assert again.removed == 0        # nothing new was written
        total = conn.execute("SELECT COUNT(*) n FROM link_diffs").fetchone()["n"]
        assert total == 1


# -- run bookkeeping --------------------------------------------------------


def test_runs_are_listed_oldest_first(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        page(conn, 1, NEW)
        link(conn, 1, "a", NEW)
        page(conn, 1, OLD)
        link(conn, 1, "a", OLD)
        assert diff_mod.list_runs(conn) == [OLD, NEW]


def test_previous_run_is_the_newest_older_one(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        for run in ("20260501", OLD, NEW):
            page(conn, 1, run)
            link(conn, 1, "a", run)
        assert diff_mod.previous_run(conn, NEW) == OLD
        assert diff_mod.previous_run(conn, "20260501") is None


# -- scoring ----------------------------------------------------------------


def test_removal_raises_a_domains_score(tmp_path):
    """The point of the stage: editors dropping a citation moves the domain up
    the shortlist."""
    with open_db(tmp_path / "w.db") as conn:
        did = domain(conn, "dropped.com")
        page(conn, 1, OLD)
        page(conn, 1, NEW)
        link(conn, 1, "h1", OLD, domain_id=did)

        row = conn.execute(
            "SELECT * FROM domains WHERE domain_id=?", (did,)
        ).fetchone()
        before = score_mod.score_domain(row, *score_mod.evidence_for(conn, did)).total

        diff_mod.compute(conn, OLD, NEW)
        after = score_mod.score_domain(row, *score_mod.evidence_for(conn, did)).total
        assert after > before


def test_removal_is_explained_in_the_score_breakdown(tmp_path):
    """Never a black box — `inspect` has to be able to say why."""
    with open_db(tmp_path / "w.db") as conn:
        did = domain(conn, "dropped.com")
        page(conn, 1, OLD)
        page(conn, 1, NEW)
        link(conn, 1, "h1", OLD, domain_id=did)
        diff_mod.compute(conn, OLD, NEW)

        row = conn.execute("SELECT * FROM domains WHERE domain_id=?", (did,)).fetchone()
        score = score_mod.score_domain(row, *score_mod.evidence_for(conn, did))
        names = {c.name for c in score.components}
        assert "editor removal" in names


def test_removal_never_sets_a_state(tmp_path):
    """Corroboration, not a verdict. A removed citation must not make a live
    domain look dead — only crawl and check may assign states."""
    with open_db(tmp_path / "w.db") as conn:
        did = domain(conn, "alive.com", state=DomainState.ACTIVE)
        page(conn, 1, OLD)
        page(conn, 1, NEW)
        link(conn, 1, "h1", OLD, domain_id=did, state=UrlState.LIVE)
        diff_mod.compute(conn, OLD, NEW)

        assert conn.execute(
            "SELECT state FROM domains WHERE domain_id=?", (did,)
        ).fetchone()["state"] == DomainState.ACTIVE
        assert conn.execute(
            "SELECT state FROM urls WHERE url_hash='h1'"
        ).fetchone()["state"] == UrlState.LIVE


def test_removal_points_are_tunable(tmp_path):
    from wikimill.policy import POLICY_FILENAME, load

    with open_db(tmp_path / "w.db") as conn:
        did = domain(conn, "dropped.com")
        page(conn, 1, OLD)
        page(conn, 1, NEW)
        link(conn, 1, "h1", OLD, domain_id=did)
        diff_mod.compute(conn, OLD, NEW)
        row = conn.execute("SELECT * FROM domains WHERE domain_id=?", (did,)).fetchone()
        evidence = score_mod.evidence_for(conn, did)

        (tmp_path / POLICY_FILENAME).write_text(
            "[scoring]\nwiki_removed_points = 99\n", encoding="utf-8"
        )
        tuned = score_mod.score_domain(row, *evidence, load(tmp_path)).total
        assert tuned > score_mod.score_domain(row, *evidence).total


def test_added_links_do_not_score(tmp_path):
    """A fresh citation says the source is *alive*, so it must not raise an
    acquisition score. Only removals feed the shortlist."""
    with open_db(tmp_path / "w.db") as conn:
        did = domain(conn, "cited.com")
        page(conn, 1, OLD)
        page(conn, 1, NEW)
        link(conn, 1, "h1", NEW, domain_id=did)

        row = conn.execute("SELECT * FROM domains WHERE domain_id=?", (did,)).fetchone()
        before = score_mod.score_domain(row, *score_mod.evidence_for(conn, did)).total
        diff_mod.compute(conn, OLD, NEW)
        after = score_mod.score_domain(row, *score_mod.evidence_for(conn, did)).total
        assert after == before


def test_removal_counts_are_deduplicated_across_run_pairs(tmp_path):
    """The same link removed once but compared over several run pairs is one
    removal — otherwise a domain's score would drift upward with nothing new
    having happened."""
    with open_db(tmp_path / "w.db") as conn:
        did = domain(conn, "dropped.com")
        for run in ("20260501", OLD, NEW):
            page(conn, 1, run)
        link(conn, 1, "h1", "20260501", domain_id=did)

        diff_mod.compute(conn, "20260501", OLD)
        diff_mod.compute(conn, "20260501", NEW)
        assert diff_mod.removal_counts(conn, did) == 1


# -- reporting --------------------------------------------------------------


def test_top_removed_domains_ranks_by_count(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        busy = domain(conn, "busy.com")
        quiet = domain(conn, "quiet.com")
        for pid in (1, 2, 3):
            page(conn, pid, OLD)
            page(conn, pid, NEW)
            link(conn, pid, f"busy{pid}", OLD, domain_id=busy)
        link(conn, 1, "quiet1", OLD, domain_id=quiet)

        diff_mod.compute(conn, OLD, NEW)
        assert diff_mod.top_removed_domains(conn, OLD, NEW) == [
            ("busy.com", 3),
            ("quiet.com", 1),
        ]


def test_summary_reports_both_transition_totals(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        page(conn, 1, OLD)
        page(conn, 1, NEW)
        link(conn, 1, "gone", OLD)
        link(conn, 1, "fresh", NEW)
        diff_mod.compute(conn, OLD, NEW)
        assert diff_mod.summary(conn, OLD, NEW) == {
            diff_mod.REMOVED: 1,
            diff_mod.ADDED: 1,
        }


# -- the CLI surface --------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKIMILL_CONTACT", "ops@example.org")
    monkeypatch.setattr("wikimill.config.repo_root", lambda: tmp_path)


def test_stats_diff_says_plainly_when_there_is_only_one_run(tmp_path):
    """One ingested run is the normal state, not an error. It must say so
    rather than printing zeros that read as "editors removed nothing"."""
    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        page(conn, 1, NEW)
        link(conn, 1, "a", NEW)

    result = runner.invoke(app, ["stats", "--diff"])
    assert result.exit_code == 0
    assert "needs two ingested dump runs" in result.stdout


def test_stats_diff_reports_the_latest_pair(tmp_path):
    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        did = domain(conn, "dropped.com")
        page(conn, 1, OLD)
        page(conn, 1, NEW)
        link(conn, 1, "gone", OLD, domain_id=did)
        diff_mod.compute(conn, OLD, NEW)

    result = runner.invoke(app, ["stats", "--diff"])
    assert result.exit_code == 0
    assert f"{OLD} → {NEW}" in result.stdout
    assert "dropped.com" in result.stdout


def test_stats_diff_json_shape(tmp_path):
    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        did = domain(conn, "dropped.com")
        page(conn, 1, OLD)
        page(conn, 1, NEW)
        link(conn, 1, "gone", OLD, domain_id=did)
        diff_mod.compute(conn, OLD, NEW)

    payload = json.loads(runner.invoke(app, ["stats", "--diff", "--json"]).stdout)
    assert payload["diff"]["from_run"] == OLD
    assert payload["diff"]["transitions"][diff_mod.REMOVED] == 1
    assert payload["diff"]["top_removed_domains"][0]["domain"] == "dropped.com"


def test_stats_omits_the_diff_unless_asked(tmp_path):
    payload = json.loads(runner.invoke(app, ["stats", "--json"]).stdout)
    assert "diff" not in payload
    assert "cross-dump-run diff" not in runner.invoke(app, ["stats"]).stdout


def test_the_export_carries_removal_as_its_own_column(tmp_path):
    """Sortable in a spreadsheet, not buried in the score JSON — this is the
    only evidence in the file that came from a human looking at the page."""
    from wikimill import export as export_mod

    with open_db(tmp_path / "w.db") as conn:
        did = domain(conn, "dropped.com", state=DomainState.UNREGISTERED)
        page(conn, 1, OLD)
        page(conn, 1, NEW)
        link(conn, 1, "gone", OLD, domain_id=did)
        diff_mod.compute(conn, OLD, NEW)

        assert "wiki_removed" in export_mod.COLUMNS
        records = export_mod.collect(conn, [DomainState.UNREGISTERED], 1)
        assert records[0]["wiki_removed"] == 1


def test_a_domain_with_no_removals_leaves_the_column_blank(tmp_path):
    """Blank, not 0 — an empty cell reads as "no evidence" where a zero reads
    as a measurement that came back negative."""
    from wikimill import export as export_mod

    with open_db(tmp_path / "w.db") as conn:
        did = domain(conn, "steady.com", state=DomainState.UNREGISTERED)
        page(conn, 1, NEW)
        link(conn, 1, "here", NEW, domain_id=did)

        records = export_mod.collect(conn, [DomainState.UNREGISTERED], 1)
        assert records[0]["wiki_removed"] == ""

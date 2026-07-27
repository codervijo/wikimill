"""The enrichment page cache (v2.H).

The phase was framed as throughput, but the property worth testing hardest is
different: **enrichment becomes re-runnable offline**, the way classification
already is. Improving an extraction rule should not require a 26.6 GB archive
that may be on a drive that is not plugged in.

So the load-bearing tests here are the ones that prove the archive is *not*
opened when the cache covers the work — and, on the correctness side, that a
cached page from one dump run can never be served for another.
"""

from __future__ import annotations

import pytest

from wikimill.enrich import cache as cache_mod
from wikimill.enrich import runner as enrich_runner
from wikimill.enrich.seek import Page
from wikimill.errors import DumpError
from wikimill.logging import RunLog
from wikimill.storage import open_db

NOW = "2026-07-25T00:00:00+00:00"
RUN_A, RUN_B = "20260601", "20260701"


def page(page_id, text="== History ==\nSee [http://gone.example/a The Source].", title=None):
    return Page(
        page_id=page_id,
        title=title or f"Article {page_id}",
        wikitext=text,
        is_redirect=False,
    )


# -- storing and reading ----------------------------------------------------


def test_a_stored_page_reads_back_intact(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        cache_mod.put_many(conn, {7: page(7)}, RUN_A)
        got = cache_mod.get_many(conn, {7}, RUN_A)
        assert got[7].title == "Article 7"
        assert "The Source" in got[7].wikitext


def test_a_miss_returns_nothing_rather_than_raising(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        assert cache_mod.get_many(conn, {1, 2, 3}, RUN_A) == {}


def test_an_empty_request_touches_nothing(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        assert cache_mod.get_many(conn, set(), RUN_A) == {}
        assert cache_mod.put_many(conn, {}, RUN_A) == 0


def test_lookups_are_chunked_past_the_sqlite_parameter_limit(tmp_path):
    """SQLite caps host parameters near 999. A large enrichment batch exceeds
    that, and it would only surface at the scale where discovering it late is
    most expensive."""
    with open_db(tmp_path / "w.db") as conn:
        pages = {i: page(i) for i in range(1, 1_501)}
        cache_mod.put_many(conn, pages, RUN_A)
        got = cache_mod.get_many(conn, set(pages), RUN_A)
        assert len(got) == 1_500


def test_redirects_are_not_cached(tmp_path):
    """A redirect stub carries no citation context, so storing one buys nothing
    and would let it masquerade as a cached article on the next run."""
    with open_db(tmp_path / "w.db") as conn:
        stub = Page(page_id=9, title="Old Name", wikitext="", is_redirect=True)
        assert cache_mod.put_many(conn, {9: stub}, RUN_A) == 0
        assert cache_mod.get_many(conn, {9}, RUN_A) == {}


# -- the correctness guard --------------------------------------------------


def test_a_page_cached_for_one_run_is_never_served_for_another(tmp_path):
    """The whole reason the key includes the dump run.

    Offset X in one run's archive is a different block from offset X in
    another's, and an article's text genuinely changes between runs. Serving
    across runs would attach one revision's context to a link recorded against
    a different revision — what `check_dump_runs_agree` refuses at ingest,
    except silent and after the fact.
    """
    with open_db(tmp_path / "w.db") as conn:
        cache_mod.put_many(conn, {7: page(7, "old revision")}, RUN_A)
        assert cache_mod.get_many(conn, {7}, RUN_B) == {}
        assert cache_mod.get_many(conn, {7}, RUN_A)[7].wikitext == "old revision"


def test_the_same_page_can_be_cached_for_two_runs_independently(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        cache_mod.put_many(conn, {7: page(7, "old")}, RUN_A)
        cache_mod.put_many(conn, {7: page(7, "new")}, RUN_B)
        assert cache_mod.get_many(conn, {7}, RUN_A)[7].wikitext == "old"
        assert cache_mod.get_many(conn, {7}, RUN_B)[7].wikitext == "new"


def test_re_storing_a_page_replaces_rather_than_duplicates(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        cache_mod.put_many(conn, {7: page(7, "first")}, RUN_A)
        cache_mod.put_many(conn, {7: page(7, "second")}, RUN_A)
        assert conn.execute("SELECT COUNT(*) FROM page_cache").fetchone()[0] == 1
        assert cache_mod.get_many(conn, {7}, RUN_A)[7].wikitext == "second"


# -- eviction ---------------------------------------------------------------


def test_eviction_drops_least_recently_used_first(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        cache_mod.put_many(conn, {1: page(1, "x" * 400)}, RUN_A)
        cache_mod.put_many(conn, {2: page(2, "y" * 400)}, RUN_A)
        # Touch page 2 so page 1 is the stale one.
        conn.execute("UPDATE page_cache SET last_used='2020-01-01' WHERE page_id=1")

        cache_mod.evict(conn, max_bytes=500)
        remaining = {
            r["page_id"] for r in conn.execute("SELECT page_id FROM page_cache")
        }
        assert remaining == {2}


def test_eviction_is_a_no_op_under_budget(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        cache_mod.put_many(conn, {1: page(1)}, RUN_A)
        assert cache_mod.evict(conn, max_bytes=10_000_000) == 0
        assert cache_mod.total_bytes(conn) > 0


def test_a_non_positive_budget_means_unbounded(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        cache_mod.put_many(conn, {1: page(1, "x" * 5_000)}, RUN_A)
        assert cache_mod.evict(conn, max_bytes=0) == 0
        assert cache_mod.evict(conn, max_bytes=-1) == 0
        assert conn.execute("SELECT COUNT(*) FROM page_cache").fetchone()[0] == 1


def test_clear_removes_everything_or_just_one_run(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        cache_mod.put_many(conn, {1: page(1)}, RUN_A)
        cache_mod.put_many(conn, {2: page(2)}, RUN_B)

        cache_mod.clear(conn, RUN_A)
        assert cache_mod.get_many(conn, {1}, RUN_A) == {}
        assert cache_mod.get_many(conn, {2}, RUN_B) != {}

        cache_mod.clear(conn)
        assert conn.execute("SELECT COUNT(*) FROM page_cache").fetchone()[0] == 0


def test_clearing_the_cache_never_touches_observations(tmp_path):
    """Disposable means disposable: deleting it costs time, never information."""
    with open_db(tmp_path / "w.db") as conn:
        conn.execute(
            "INSERT INTO urls (url_hash, url_normalized, normalizer_version, scheme, "
            "state, first_seen) VALUES ('h','http://x/',1,'http','live',?)", (NOW,)
        )
        cache_mod.put_many(conn, {1: page(1)}, RUN_A)
        cache_mod.clear(conn)
        assert conn.execute("SELECT COUNT(*) FROM urls").fetchone()[0] == 1


# -- the offline property ---------------------------------------------------


def build_corpus(root, *, cached: bool):
    """One pending link on one page, optionally pre-cached."""
    db = root / "state" / "wikimill.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO wiki_pages (page_id, lang, title, ms_offset, dump_run, "
            "ingested_at) VALUES (5,'en','Article 5',570,?,?)", (RUN_B, NOW)
        )
        conn.execute(
            "INSERT INTO urls (url_hash, url_normalized, normalizer_version, scheme, "
            "state, first_seen) VALUES ('h1','http://gone.example/a',1,'http',"
            "'dns_failure',?)", (NOW,)
        )
        conn.execute(
            "INSERT INTO external_links (page_id, lang, url_raw, url_hash, dump_run, "
            "first_seen, last_seen, enrich_status) VALUES "
            "(5,'en','http://gone.example/a','h1',?,?,?,'pending')",
            (RUN_B, NOW, NOW),
        )
        if cached:
            cache_mod.put_many(conn, {5: page(5)}, RUN_B)
    return db


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKIMILL_CONTACT", "ops@example.org")
    monkeypatch.setattr("wikimill.config.repo_root", lambda: tmp_path)
    from wikimill.config import load as load_config
    return load_config()


def test_a_fully_cached_batch_never_opens_the_archive(tmp_path, cfg):
    """The point of the phase. `find_archive` raises when no dump is present,
    so an empty dumps directory proves the archive was never consulted —
    enrichment ran with the drive effectively unplugged.
    """
    build_corpus(tmp_path, cached=True)
    with RunLog("enrich", cfg.logs_dir, quiet=True) as log:
        stats = enrich_runner.run(cfg, log)

    assert stats.enriched == 1
    assert stats.cache.hits == 1
    assert stats.blocks_read == 0, "a cached page still caused an archive read"


def test_without_the_cache_the_missing_archive_is_an_error(tmp_path, cfg):
    """The control for the test above: same corpus, cache bypassed, and the
    absent dump now matters. Without this, a passing test proves nothing —
    enrichment could be skipping the work entirely."""
    build_corpus(tmp_path, cached=True)
    with RunLog("enrich", cfg.logs_dir, quiet=True) as log:
        with pytest.raises(DumpError):
            enrich_runner.run(cfg, log, no_cache=True)


def test_an_uncached_batch_still_needs_the_archive(tmp_path, cfg):
    build_corpus(tmp_path, cached=False)
    with RunLog("enrich", cfg.logs_dir, quiet=True) as log:
        with pytest.raises(DumpError):
            enrich_runner.run(cfg, log)


def test_dry_run_says_when_no_archive_would_be_opened(tmp_path, cfg):
    build_corpus(tmp_path, cached=True)
    with RunLog("enrich", cfg.logs_dir, quiet=True) as log:
        stats = enrich_runner.run(cfg, log, dry_run=True)
    assert stats.candidates == 1
    assert stats.blocks_read == 0


def test_the_cache_can_be_disabled_by_policy(tmp_path, cfg):
    from wikimill.policy import POLICY_FILENAME

    build_corpus(tmp_path, cached=True)
    (tmp_path / POLICY_FILENAME).write_text(
        "[enrich]\ncache_enabled = false\n", encoding="utf-8"
    )
    with RunLog("enrich", cfg.logs_dir, quiet=True) as log:
        with pytest.raises(DumpError):
            enrich_runner.run(cfg, log)


def test_enrichment_from_cache_produces_the_same_context(tmp_path, cfg):
    """Cached extraction must agree with archive extraction, or the cache is a
    silent second implementation of the stage."""
    build_corpus(tmp_path, cached=True)
    with RunLog("enrich", cfg.logs_dir, quiet=True) as log:
        enrich_runner.run(cfg, log)

    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        row = conn.execute(
            "SELECT section, anchor_text, enrich_status FROM external_links"
        ).fetchone()
    assert row["section"] == "History"
    assert row["anchor_text"] == "The Source"
    assert row["enrich_status"] == "done"


def test_eviction_removes_only_what_the_overshoot_requires(tmp_path):
    """Batching is an implementation detail, not the eviction unit. Deleting a
    whole batch because it was convenient would evict pages already within
    budget — cheap to regenerate, but it makes the cache useless at any size
    near its cap."""
    with open_db(tmp_path / "w.db") as conn:
        for i in range(1, 11):
            cache_mod.put_many(conn, {i: page(i, "x" * 100)}, RUN_A)
            conn.execute(
                "UPDATE page_cache SET last_used=? WHERE page_id=?",
                (f"2020-01-{i:02d}", i),
            )
        # 10 pages x 100 bytes; a 700-byte budget should drop exactly 3.
        assert cache_mod.evict(conn, max_bytes=700) == 3
        remaining = {
            r["page_id"] for r in conn.execute("SELECT page_id FROM page_cache")
        }
        assert remaining == {4, 5, 6, 7, 8, 9, 10}, "evicted more than necessary"


def test_dry_run_reports_the_plan_even_with_no_archive_present(tmp_path, cfg):
    """Asking what a run would cost is what an operator does *before* deciding
    whether to go and plug the drive in. Refusing to answer because the drive
    is unplugged inverts that."""
    build_corpus(tmp_path, cached=False)
    with RunLog("enrich", cfg.logs_dir, quiet=True) as log:
        stats = enrich_runner.run(cfg, log, dry_run=True)
    assert stats.candidates == 1
    assert stats.blocks_read == 0

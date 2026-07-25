"""End-to-end ingest over fixture dumps, including the idempotency contract."""

from __future__ import annotations

import bz2
import gzip
from pathlib import Path

import pytest

from wikimill import ingest as ingest_stage
from wikimill.config import load
from wikimill.errors import DumpError
from wikimill.logging import RunLog
from wikimill.storage import open_db
from wikimill.wiki.msindex import (
    IndexEntry,
    find_index_files,
    namespace_report,
    parse_page_range,
)

RUN = "20260701"

# page_id -> (offset, title). Mirrors the real `offset:page_id:title` index.
INDEX_ROWS = [
    (601, 10, "AccessibleComputing"),
    (601, 12, "Anarchism"),
    (601, 25, "Autism"),
    (98765, 41000, "Star Trek: First Contact"),  # a legitimate colon in a title
    (98765, 41243, "Out Of Slice"),
    (98765, 41244, "Category:Physics"),  # a real namespaced entry
]

LINK_ROWS = [
    (1, 10, "http://edu.berkeley.housing.www.", "/housing/"),
    (2, 12, "http://uk.co.bbc.news.", "/news"),
    (3, 12, "http://V4.66.102.9.104.", "/"),
    (4, 25, "http://uk.co.linearb.:8080", "/p"),
    (5, 41243, "http://com.outofslice.", "/"),      # outside the slice
    (6, 999999, "http://com.unknownpage.", "/"),    # page not in the index
    (7, 10, "irc://com.freenode.", "/wikipedia"),   # non-crawlable scheme
    (8, 10, "ftp://org.example.ftp.", "/pub"),      # non-crawlable scheme
]


@pytest.fixture
def dumps(tmp_path):
    """A miniature but structurally faithful pair of dumps."""
    d = tmp_path / "state" / "dumps"
    d.mkdir(parents=True)

    index_body = "".join(f"{o}:{p}:{t}\n" for o, p, t in INDEX_ROWS).encode()
    with bz2.open(
        d / f"enwiki-{RUN}-pages-articles-multistream-index.txt.bz2", "wb"
    ) as fh:
        fh.write(index_body)

    tuples = ",".join(
        f"({i},{p},'{dom}','{path}')" for i, p, dom, path in LINK_ROWS
    ).encode()
    body = (
        b"CREATE TABLE `externallinks` (`el_id` int);\n"
        b"INSERT INTO `externallinks` VALUES " + tuples + b";\n"
    )
    with gzip.open(d / f"enwiki-{RUN}-externallinks.sql.gz", "wb") as fh:
        fh.write(body)
    return d


@pytest.fixture
def cfg(tmp_path, dumps, monkeypatch):
    monkeypatch.setenv("WIKIMILL_CONTACT", "ops@example.org")
    monkeypatch.delenv("WIKIMILL_DUMPS_DIR", raising=False)
    return load(tmp_path)


@pytest.fixture
def log(tmp_path):
    return RunLog("ingest", tmp_path / "logs", quiet=True)


def test_ingest_inserts_links_in_slice(cfg, log):
    stats = ingest_stage.run(cfg, log, pages="p1p41242")
    assert stats.pages_indexed == 4  # 10, 12, 25, 41000
    assert stats.links_inserted == 4  # rows 1-4; 5/6 out of slice, 7/8 not crawlable


def test_urls_are_reconstructed_correctly(cfg, log):
    ingest_stage.run(cfg, log, pages="p1p41242")
    with open_db(cfg.db_path) as conn:
        urls = {
            r["url_raw"]
            for r in conn.execute("SELECT url_raw FROM external_links")
        }
    assert "http://www.housing.berkeley.edu/housing/" in urls
    assert "http://news.bbc.co.uk/news" in urls
    assert "http://66.102.9.104/" in urls          # IP not reversed
    assert "http://linearb.co.uk:8080/p" in urls   # port split correctly


def test_non_crawlable_schemes_counted_not_queued(cfg, log):
    stats = ingest_stage.run(cfg, log, pages="p1p41242")
    assert stats.skipped_scheme == {"irc": 1, "ftp": 1}
    with open_db(cfg.db_path) as conn:
        rows = conn.execute(
            "SELECT COUNT(*) c FROM external_links WHERE url_raw LIKE 'irc%' "
            "OR url_raw LIKE 'ftp%'"
        ).fetchone()
    assert rows["c"] == 0


def test_pages_outside_the_slice_are_excluded(cfg, log):
    ingest_stage.run(cfg, log, pages="p1p41242")
    with open_db(cfg.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM external_links WHERE page_id=41243"
        ).fetchone()["c"] == 0


def test_links_from_unknown_pages_are_excluded(cfg, log):
    """`el_from` not present in the index is how the namespace filter works."""
    ingest_stage.run(cfg, log, pages="p1p41242")
    with open_db(cfg.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM external_links WHERE page_id=999999"
        ).fetchone()["c"] == 0


def test_wiki_pages_records_offset_and_title(cfg, log):
    """ms_offset is what makes v1.H enrichment a seek instead of a scan."""
    ingest_stage.run(cfg, log, pages="p1p41242")
    with open_db(cfg.db_path) as conn:
        row = conn.execute(
            "SELECT title, ms_offset FROM wiki_pages WHERE page_id=12"
        ).fetchone()
    assert row["title"] == "Anarchism"
    assert row["ms_offset"] == 601


def test_context_columns_are_null_after_ingest(cfg, log):
    """The whole cheapest-first design: no wikitext is read at this stage."""
    ingest_stage.run(cfg, log, pages="p1p41242")
    with open_db(cfg.db_path) as conn:
        row = conn.execute(
            "SELECT anchor_text, section, enrich_status FROM external_links LIMIT 1"
        ).fetchone()
    assert row["anchor_text"] is None
    assert row["section"] is None
    assert row["enrich_status"] == "pending"


# -- idempotency (prd.md §8, acceptance criterion 5) ------------------------


def test_reingest_inserts_nothing(cfg, tmp_path):
    first = ingest_stage.run(
        cfg, RunLog("ingest", tmp_path / "l", quiet=True), pages="p1p41242"
    )
    second = ingest_stage.run(
        cfg, RunLog("ingest", tmp_path / "l", quiet=True), pages="p1p41242"
    )
    assert first.links_inserted == 4
    assert second.links_inserted == 0
    assert second.links_duplicate == 4


def test_reingest_leaves_row_count_unchanged(cfg, tmp_path):
    for _ in range(3):
        ingest_stage.run(
            cfg, RunLog("ingest", tmp_path / "l", quiet=True), pages="p1p41242"
        )
    with open_db(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM external_links").fetchone()["c"] == 4
        assert conn.execute("SELECT COUNT(*) c FROM wiki_pages").fetchone()["c"] == 4


def test_dry_run_writes_nothing(cfg, log):
    ingest_stage.run(cfg, log, pages="p1p41242", dry_run=True)
    with open_db(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM external_links").fetchone()["c"] == 0


def test_limit_caps_inserts(cfg, log):
    stats = ingest_stage.run(cfg, log, pages="p1p41242", limit=2)
    assert stats.links_inserted <= 2


def test_no_slice_ingests_everything_indexed(cfg, log):
    """Without --pages the whole index is ingested, minus namespaced pages
    (one Category: entry in the fixture)."""
    stats = ingest_stage.run(cfg, log)
    assert stats.pages_indexed == len(INDEX_ROWS) - 1
    assert stats.links_inserted == 5  # adds page 41243


# -- dump-run pinning (prd.md §6, §20) -------------------------------------


def test_mismatched_dump_runs_are_refused(cfg, log, dumps):
    """A page revised between runs would attach context from one revision to a
    link recorded in another — wrong, and silently so."""
    stale = dumps / "enwiki-20260601-pages-articles-multistream-index.txt.bz2"
    with bz2.open(stale, "wb") as fh:
        fh.write(b"601:10:Foo\n")
    with pytest.raises(DumpError) as exc:
        ingest_stage.run(cfg, log, pages="p1p41242")
    assert "mismatch" in str(exc.value).lower()
    assert exc.value.remediation


def test_missing_sql_dump_is_a_typed_error(cfg, log, dumps):
    (dumps / f"enwiki-{RUN}-externallinks.sql.gz").unlink()
    with pytest.raises(DumpError) as exc:
        ingest_stage.run(cfg, log)
    assert exc.value.remediation


def test_missing_index_is_a_typed_error(cfg, log, dumps):
    (dumps / f"enwiki-{RUN}-pages-articles-multistream-index.txt.bz2").unlink()
    with pytest.raises(DumpError) as exc:
        ingest_stage.run(cfg, log)
    assert exc.value.remediation


# -- page ranges + namespace measurement -----------------------------------


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [("p1p41242", 1, 41242), ("1-41242", 1, 41242), ("p10p20", 10, 20)],
)
def test_parse_page_range(text, start, end):
    r = parse_page_range(text)
    assert (r.start, r.end) == (start, end)


@pytest.mark.parametrize("bad", ["", "abc", "p10", "10", "p20p10"])
def test_bad_page_range_rejected(bad):
    with pytest.raises(DumpError):
        parse_page_range(bad)


def test_find_index_files(dumps):
    assert len(find_index_files(dumps)) == 1


def test_namespaced_pages_excluded_by_default(cfg, log):
    """Measured on the real 20260701 index: 99.27% of the slice is articles, so
    intersection alone is a good but imperfect namespace filter."""
    ingest_stage.run(cfg, log)
    with open_db(cfg.db_path) as conn:
        titles = {r["title"] for r in conn.execute("SELECT title FROM wiki_pages")}
    assert "Category:Physics" not in titles
    assert "Anarchism" in titles


def test_article_with_a_colon_is_kept(cfg, log):
    """'Star Trek: First Contact' is an article, not a namespace — dropping it
    would silently lose encyclopedic pages."""
    ingest_stage.run(cfg, log)
    with open_db(cfg.db_path) as conn:
        titles = {r["title"] for r in conn.execute("SELECT title FROM wiki_pages")}
    assert "Star Trek: First Contact" in titles


def test_include_namespaces_keeps_them(cfg, log):
    ingest_stage.run(cfg, log, include_namespaces=True)
    with open_db(cfg.db_path) as conn:
        titles = {r["title"] for r in conn.execute("SELECT title FROM wiki_pages")}
    assert "Category:Physics" in titles


def test_namespace_report_counts_real_namespaces_only():
    """A colon in a title is not a namespace — 'Star Trek: First Contact' is an
    article, and must not be counted as one."""
    report = namespace_report(iter(
        [IndexEntry(o, p, t) for o, p, t in INDEX_ROWS]
    ))
    assert report["sampled"] == len(INDEX_ROWS)
    assert report["by_namespace"] == {"Category": 1}
    assert "Star Trek: First Contact" not in report["examples"].values()

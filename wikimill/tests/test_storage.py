"""Schema, migrations, and the append-only guarantee."""

from __future__ import annotations

import sqlite3

import pytest

from wikimill.storage import LATEST_VERSION, connect, counts, migrate, open_db, table_names
from wikimill.storage.db import user_version
from wikimill.errors import StorageError

EXPECTED_TABLES = {
    "crawl_runs",
    "domain_checks",
    "domains",
    "exports",
    "external_links",
    "robots_cache",
    "url_checks",
    "urls",
    "wiki_pages",
}


def test_migration_creates_every_table(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        assert set(table_names(conn)) == EXPECTED_TABLES
        assert user_version(conn) == LATEST_VERSION


def test_migration_is_idempotent(tmp_path):
    """Preflight runs before every command; migrating must be free when current."""
    db = tmp_path / "w.db"
    with open_db(db) as conn:
        first = user_version(conn)
    with open_db(db) as conn:
        before, after = migrate(conn)
        assert (before, after) == (first, first)


def test_wal_mode_enabled(tmp_path):
    """WAL is what lets stats/inspect read while a crawl writes."""
    conn = connect(tmp_path / "w.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_future_schema_is_refused(tmp_path):
    """A database from a newer build must not be silently downgraded."""
    db = tmp_path / "w.db"
    with open_db(db):
        pass
    conn = connect(db)
    conn.execute(f"PRAGMA user_version={LATEST_VERSION + 5}")
    conn.close()
    conn = connect(db)
    try:
        with pytest.raises(StorageError) as exc:
            migrate(conn)
        assert exc.value.remediation
    finally:
        conn.close()


def test_counts_starts_empty(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        assert set(counts(conn)) == EXPECTED_TABLES
        assert all(v == 0 for v in counts(conn).values())


def test_private_suffix_column_renamed(tmp_path):
    """v1.D migration: the old name overclaimed what the PSL can tell us."""
    with open_db(tmp_path / "w.db") as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(domains)")}
        assert "is_private_suffix" in cols
        assert "is_user_content_suffix" not in cols


def test_urls_records_normalizer_version(tmp_path):
    """Changing a normalization rule changes url_hash; the version makes that
    detectable instead of silently forking identity across the table."""
    with open_db(tmp_path / "w.db") as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(urls)")}
        assert "normalizer_version" in cols


def test_url_checks_is_append_only_in_shape(tmp_path):
    """Two checks of the same URL must coexist — history is the product."""
    with open_db(tmp_path / "w.db") as conn:
        for status in (200, 404):
            conn.execute(
                "INSERT INTO url_checks (url_hash, checked_at, http_status) "
                "VALUES (?, ?, ?)",
                ("abc", "2026-07-25T00:00:00+00:00", status),
            )
        rows = conn.execute(
            "SELECT http_status FROM url_checks WHERE url_hash='abc' ORDER BY id"
        ).fetchall()
        assert [r["http_status"] for r in rows] == [200, 404]


def test_external_links_unique_per_dump_run(tmp_path):
    """Re-ingesting the same slice must add nothing (prd.md §19.5)."""
    with open_db(tmp_path / "w.db") as conn:
        row = ("2026-07-25T00:00:00+00:00",)
        args = (1, "en", "https://x.example/", "hash1", "20260701", *row, *row)
        sql = (
            "INSERT INTO external_links "
            "(page_id, lang, url_raw, url_hash, dump_run, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?)"
        )
        conn.execute(sql, args)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, args)


def test_domains_unique_on_registrable_domain(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        sql = "INSERT INTO domains (registrable_domain, first_seen) VALUES (?,?)"
        conn.execute(sql, ("example.com", "2026-07-25T00:00:00+00:00"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, ("example.com", "2026-07-25T00:00:00+00:00"))

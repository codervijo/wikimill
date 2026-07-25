"""Stage 1 — ingest: the externallinks SQL dump into the link table.

This is the cheap half of the pipeline. It reads only the SQL dump (~4.9 GB)
and the multistream index (~283 MB); it never opens the 26.6 GB article dump and
never parses a byte of wikitext. Context extraction is v1.H, and runs only for
links that turn out to be interesting.

Order of work:

1. **Index → `wiki_pages`.** The multistream index gives `page_id -> (title,
   offset)` for the slice. Persisting it here means enrichment is later a seek,
   and it doubles as the namespace filter (`externallinks` has no namespace
   column, so `el_from` is intersected with these page IDs).
2. **SQL dump → `external_links`.** Stream the dump, keep rows whose `el_from`
   is in the slice, reconstruct each URL, and insert.

Idempotency (prd.md §8): keyed on `(page_id, url_hash, dump_run)`. Re-running
over the same slice inserts nothing and reports `↷ already ingested`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .constants import NORMALIZER_VERSION, RunKind, UrlState
from .errors import DumpError
from .logging import RunLog, utcnow
from .normalize import normalize, url_hash
from .storage import open_db
from .wiki import dump_sql, msindex
from .wiki.eldomain import ReconstructionError, reconstruct
from .wiki.msindex import PageRange

# `externallinks` column order, verified against the real dump's CREATE TABLE
# (2026-07-25): el_id, el_from, el_to_domain_index, el_to_path.
EL_ID, EL_FROM, EL_DOMAIN, EL_PATH = 0, 1, 2, 3

PROGRESS_EVERY = 250_000


@dataclass
class IngestStats:
    pages_indexed: int = 0
    rows_scanned: int = 0
    rows_in_slice: int = 0
    links_inserted: int = 0
    links_duplicate: int = 0
    urls_created: int = 0
    domains_created: int = 0
    archives_unwrapped: int = 0
    skipped_scheme: dict[str, int] = field(default_factory=dict)
    dropped: dict[str, int] = field(default_factory=dict)
    malformed: int = 0
    malformed_examples: list[str] = field(default_factory=list)

    def note_scheme(self, scheme: str) -> None:
        self.skipped_scheme[scheme] = self.skipped_scheme.get(scheme, 0) + 1

    def note_dropped(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1

    def note_malformed(self, detail: str) -> None:
        self.malformed += 1
        if len(self.malformed_examples) < 5:
            self.malformed_examples.append(detail)


def resolve_sql_dump(cfg: Config, explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            path = cfg.root / path
        if not path.is_file():
            raise DumpError(
                f"Dump not found: {path}",
                remediation="Pass a readable path to --dump.",
            )
        return path
    matches = sorted(cfg.dumps_dir.glob("*-externallinks.sql.gz"))
    if not matches:
        raise DumpError(
            f"No externallinks dump found in {cfg.dumps_dir}",
            remediation=(
                "Download <wiki>-<date>-externallinks.sql.gz from "
                "https://dumps.wikimedia.org/ into that directory, or pass --dump."
            ),
        )
    return matches[-1]


def check_dump_runs_agree(sql_dump: Path, index_files: list[Path]) -> str:
    """Both dumps must come from the same run (prd.md §6, §20).

    A page revised between runs would otherwise attach context from one revision
    to a link recorded in another — wrong, and silently so.
    """
    sql_run = dump_sql.dump_run_from_name(sql_dump)
    index_runs = {
        run
        for path in index_files
        if (run := dump_sql.dump_run_from_name(path)) is not None
    }
    if sql_run is None or not index_runs:
        raise DumpError(
            "Cannot determine the dump run from the filenames.",
            remediation=(
                "Keep the original dump filenames (e.g. enwiki-20260701-…), which "
                "carry the run date. wikimill pins both dumps to the same run."
            ),
        )
    if index_runs != {sql_run}:
        raise DumpError(
            f"Dump run mismatch: SQL dump is {sql_run}, index is "
            f"{', '.join(sorted(index_runs))}.",
            remediation=(
                "Use the SQL dump and the multistream index from the same run "
                "date. Mixed runs attach context from one revision to a link "
                "recorded in another."
            ),
        )
    return sql_run


def load_index(
    conn: sqlite3.Connection,
    index_files: list[Path],
    page_range: PageRange | None,
    dump_run: str,
    lang: str,
    log: RunLog,
    *,
    articles_only: bool = True,
) -> tuple[set[int], int]:
    """Persist the slice's index entries into `wiki_pages`.

    Returns (page_ids, inserted). The page-ID set is the namespace filter and
    the slice filter in one.
    """
    now = utcnow()
    page_ids: set[int] = set()
    batch: list[tuple] = []
    inserted = 0

    def flush() -> int:
        if not batch:
            return 0
        cur = conn.executemany(
            "INSERT OR IGNORE INTO wiki_pages "
            "(page_id, lang, title, ms_offset, dump_run, ingested_at) "
            "VALUES (?,?,?,?,?,?)",
            batch,
        )
        batch.clear()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    conn.execute("BEGIN")
    for entry in msindex.iter_range(
        index_files, page_range, articles_only=articles_only
    ):
        page_ids.add(entry.page_id)
        batch.append((entry.page_id, lang, entry.title, entry.offset, dump_run, now))
        if len(batch) >= 10_000:
            inserted += flush()
            log.progress(f"indexed {len(page_ids):,} pages…")
    inserted += flush()
    conn.execute("COMMIT")
    return page_ids, inserted


def ingest_links(
    conn: sqlite3.Connection,
    sql_dump: Path,
    page_ids: set[int],
    dump_run: str,
    lang: str,
    stats: IngestStats,
    log: RunLog,
    limit: int | None,
) -> None:
    """Stream the SQL dump, keeping only rows whose source page is in the slice.

    Normalization (stage 2) runs inline here rather than as a later rewrite
    pass, so `url_hash` is the normalized hash from the moment a row exists.
    A rewrite pass would have to mutate a UNIQUE key and merge the collisions
    it created — this avoids the problem instead of solving it.
    """
    now = utcnow()
    batch: list[tuple] = []
    domain_ids: dict[str, int] = {}

    def domain_id_for(norm) -> int | None:
        """Upsert the domain and return its id. Cached per run."""
        registrable = norm.domain.registrable_domain
        if not registrable:
            return None  # bare IP: crawlable, but never an acquisition candidate
        cached = domain_ids.get(registrable)
        if cached is not None:
            return cached
        cur = conn.execute(
            "INSERT OR IGNORE INTO domains "
            "(registrable_domain, public_suffix, is_private_suffix, "
            " is_resolver, state, first_seen) VALUES (?,?,?,?,?,?)",
            (
                registrable,
                norm.domain.public_suffix,
                int(norm.domain.is_private_suffix),
                int(norm.domain.is_resolver),
                "unknown",
                now,
            ),
        )
        if cur.rowcount:
            stats.domains_created += 1
        row = conn.execute(
            "SELECT domain_id FROM domains WHERE registrable_domain=?",
            (registrable,),
        ).fetchone()
        domain_ids[registrable] = row["domain_id"]
        return row["domain_id"]

    def flush() -> None:
        if not batch:
            return
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO external_links "
            "(page_id, lang, url_raw, url_hash, dump_run, first_seen, last_seen, "
            " archive_url, archive_date) VALUES (?,?,?,?,?,?,?,?,?)",
            batch,
        )
        applied = conn.total_changes - before
        stats.links_inserted += applied
        stats.links_duplicate += len(batch) - applied
        batch.clear()

    seen_urls: set[str] = set()

    def upsert_url(norm) -> None:
        """Create the queue entry for a normalized URL, once per run."""
        hashed = url_hash(norm.url)
        if hashed in seen_urls:
            return
        seen_urls.add(hashed)
        cur = conn.execute(
            "INSERT OR IGNORE INTO urls "
            "(url_hash, url_normalized, normalizer_version, domain_id, scheme, "
            " state, first_seen) VALUES (?,?,?,?,?,?,?)",
            (
                hashed,
                norm.url,
                NORMALIZER_VERSION,
                domain_id_for(norm),
                norm.scheme,
                UrlState.PENDING,
                now,
            ),
        )
        if cur.rowcount:
            stats.urls_created += 1

    conn.execute("BEGIN")
    for row in dump_sql.iter_rows(sql_dump):
        stats.rows_scanned += 1
        if stats.rows_scanned % PROGRESS_EVERY == 0:
            log.progress(
                f"scanned {stats.rows_scanned:,} rows · "
                f"{stats.links_inserted:,} links kept"
            )
        if len(row) <= EL_PATH:
            stats.note_malformed(f"short tuple: {row!r}")
            continue
        page_id = row[EL_FROM]
        if not isinstance(page_id, int) or page_id not in page_ids:
            continue
        stats.rows_in_slice += 1
        domain_index = row[EL_DOMAIN]
        if not isinstance(domain_index, str):
            stats.note_malformed(f"non-text domain index at el_id={row[EL_ID]!r}")
            continue
        path = row[EL_PATH] if isinstance(row[EL_PATH], str) else None
        try:
            rebuilt = reconstruct(domain_index, path)
        except ReconstructionError as exc:
            stats.note_malformed(f"{domain_index!r}: {exc}")
            continue
        if not rebuilt.crawlable:
            # Recorded as a count, never queued (prd.md §10 rule 10). Real dumps
            # carry irc/ftp/gopher/telnet/worldwind/mailto/news links.
            stats.note_scheme(rebuilt.scheme)
            continue

        # Stage 2 — normalize inline, so url_hash is the normalized hash from
        # the moment the row exists.
        norm = normalize(rebuilt.url)
        if not norm.keep:
            stats.note_dropped(str(norm.drop_reason))
            continue
        if norm.archive_url:
            stats.archives_unwrapped += 1

        upsert_url(norm)
        batch.append(
            (
                page_id,
                lang,
                rebuilt.url,          # what MediaWiki recorded
                url_hash(norm.url),   # identity = hash of the NORMALIZED form
                dump_run,
                now,
                now,
                norm.archive_url,
                norm.archive_date,
            )
        )
        if len(batch) >= 5_000:
            flush()
        if limit is not None and stats.links_inserted + len(batch) >= limit:
            break
    flush()
    _update_counts(conn)
    conn.execute("COMMIT")


def _update_counts(conn: sqlite3.Connection) -> None:
    """Refresh the denormalized counts `export` and scoring read.

    Recomputed from scratch rather than incremented, so they cannot drift out of
    step with the tables they summarize across repeated partial runs.
    """
    conn.execute(
        """
        UPDATE urls SET
            cite_count = (
                SELECT COUNT(*) FROM external_links e WHERE e.url_hash = urls.url_hash
            ),
            distinct_page_count = (
                SELECT COUNT(DISTINCT e.page_id) FROM external_links e
                WHERE e.url_hash = urls.url_hash
            )
        """
    )
    conn.execute(
        """
        UPDATE domains SET
            url_count = (
                SELECT COUNT(*) FROM urls u WHERE u.domain_id = domains.domain_id
            ),
            wiki_link_count = (
                SELECT COUNT(*) FROM external_links e
                JOIN urls u ON u.url_hash = e.url_hash
                WHERE u.domain_id = domains.domain_id
            ),
            wiki_page_count = (
                SELECT COUNT(DISTINCT e.page_id) FROM external_links e
                JOIN urls u ON u.url_hash = e.url_hash
                WHERE u.domain_id = domains.domain_id
            )
        """
    )


def run(
    cfg: Config,
    log: RunLog,
    *,
    dump: str | None = None,
    pages: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    include_namespaces: bool = False,
) -> IngestStats:
    """Execute the ingest stage."""
    stats = IngestStats()

    sql_dump = resolve_sql_dump(cfg, dump)
    index_files = msindex.find_index_files(cfg.dumps_dir)
    dump_run = check_dump_runs_agree(sql_dump, index_files)
    lang = dump_sql.lang_from_name(sql_dump)
    page_range = msindex.parse_page_range(pages) if pages else None

    log.ok("dump", f"{sql_dump.name} (run {dump_run}, {lang}wiki)")
    log.ok("index", f"{len(index_files)} file(s)")
    log.ok("slice", str(page_range) if page_range else "entire index (no --pages)")
    log.ok(
        "namespaces",
        "articles only (measured: the index is ~99.3% articles, not 100%)"
        if not include_namespaces
        else "including Wikipedia:/Portal:/Help:/… pages (--include-namespaces)",
    )

    if dry_run:
        # Cost must be visible before it is paid: report the work without doing it.
        log.warn("dry run", "nothing was written")
        log.progress(f"would read {sql_dump} ({sql_dump.stat().st_size / 1e9:.1f} GB)")
        for path in index_files:
            log.progress(f"would read {path.name}")
        return stats

    with open_db(cfg.db_path) as conn:
        log.progress("loading multistream index…")
        page_ids, indexed = load_index(
            conn,
            index_files,
            page_range,
            dump_run,
            lang,
            log,
            articles_only=not include_namespaces,
        )
        stats.pages_indexed = len(page_ids)
        if not page_ids:
            log.fail("index", "no pages matched the slice")
            return stats
        log.ok(
            "pages",
            f"{len(page_ids):,} in slice ({indexed:,} new, "
            f"{len(page_ids) - indexed:,} already present)",
        )

        log.progress("streaming the externallinks dump…")
        ingest_links(conn, sql_dump, page_ids, dump_run, lang, stats, log, limit)

        if stats.links_inserted:
            log.ok("links", f"{stats.links_inserted:,} inserted")
        if stats.urls_created or stats.domains_created:
            log.ok(
                "queue",
                f"{stats.urls_created:,} URLs · {stats.domains_created:,} domains",
            )
        if stats.archives_unwrapped:
            log.ok(
                "archives unwrapped",
                f"{stats.archives_unwrapped:,} (queued the origin, kept the wrapper)",
            )
        if stats.dropped:
            summary = ", ".join(
                f"{reason}:{count}"
                for reason, count in sorted(stats.dropped.items(), key=lambda kv: -kv[1])
            )
            log.warn("filtered at normalization", summary)
        if stats.links_duplicate:
            log.warn(
                "links",
                f"{stats.links_duplicate:,} already ingested (idempotent re-run)",
            )
        if not stats.links_inserted and not stats.links_duplicate:
            log.warn("links", "nothing matched the slice")
        if stats.skipped_scheme:
            summary = ", ".join(
                f"{scheme}:{count}"
                for scheme, count in sorted(
                    stats.skipped_scheme.items(), key=lambda kv: -kv[1]
                )
            )
            log.warn("non-crawlable schemes", f"recorded, not queued — {summary}")
        if stats.malformed:
            log.warn(
                "malformed rows",
                f"{stats.malformed:,} skipped (e.g. {stats.malformed_examples[0]})",
            )
        _record_run(conn, log, stats)
    return stats


def _record_run(conn: sqlite3.Connection, log: RunLog, stats: IngestStats) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO crawl_runs "
        "(run_id, kind, started_at, ended_at, counts, outcome) VALUES (?,?,?,?,?,?)",
        (
            log.run_id,
            RunKind.INGEST,
            log.started_at,
            utcnow(),
            repr(
                {
                    "pages_indexed": stats.pages_indexed,
                    "rows_scanned": stats.rows_scanned,
                    "links_inserted": stats.links_inserted,
                    "links_duplicate": stats.links_duplicate,
                }
            ),
            "ok" if not log.failed else "failed",
        ),
    )

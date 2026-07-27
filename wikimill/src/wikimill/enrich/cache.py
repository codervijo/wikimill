"""Wikitext of pages enrichment has already read (v2.H).

The headline framing of this phase was throughput — keep decompressed blocks
warm so re-enriching costs less. That is real but modest: a block decompresses
in roughly a quarter-second, and the cheapest-first pipeline means most links
never reach this stage at all.

**The larger win is that it makes re-enrichment offline**, which is the same
property classification already has. `crawl --reclassify` can re-judge every
stored observation with an improved classifier without refetching a single URL
(architecture.md §2). Extraction had no equivalent: improving `wikitext.py`'s
section or citation-kind rules meant re-seeking a 26.6 GB archive that may live
on an external drive that is not currently plugged in. With the wikitext of
already-enriched pages kept, that same improvement can be re-applied to every
past candidate with **no archive, no seek and no decompression** — and if every
candidate page is cached, `enrich` never opens the archive at all.

## The key is the dump run, not the offset

Byte offset X in one run's archive is a completely different block from offset X
in another's. An offset-keyed cache would hand one revision's wikitext to a link
recorded against another revision — precisely what `check_dump_runs_agree`
refuses to allow at ingest, except silently and after the fact. So the cache key
is `(dump_run, page_id, lang)`, and a new dump run shares nothing with the old
one. That is correct rather than wasteful: the article text may genuinely have
changed between runs, and stale context is worse than no context.

## It is disposable

Every row here can be regenerated from the archive. Deleting the cache costs
time, never information, so eviction can be brutally simple — least-recently-
used until the table fits its byte budget.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..logging import utcnow
from .seek import Page


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    stored: int = 0
    evicted: int = 0
    bytes_stored: int = 0

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return self.hits / self.lookups if self.lookups else 0.0


def get_many(
    conn: sqlite3.Connection,
    page_ids: set[int],
    dump_run: str,
    lang: str = "en",
) -> dict[int, Page]:
    """Cached pages for this run, keyed by page id. Touches `last_used`.

    Chunked because SQLite caps host parameters (default 999) and a large
    enrichment batch can exceed that — a limit that only shows up at the scale
    where it matters least to have discovered late.
    """
    if not page_ids:
        return {}

    found: dict[int, Page] = {}
    ids = list(page_ids)
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT page_id, title, wikitext FROM page_cache "
            f"WHERE dump_run=? AND lang=? AND page_id IN ({placeholders})",
            (dump_run, lang, *chunk),
        ).fetchall()
        for row in rows:
            found[row["page_id"]] = Page(
                page_id=row["page_id"],
                title=row["title"],
                wikitext=row["wikitext"],
                # Redirects are never cached (see `put_many`), so anything read
                # back is a real page. Recording it as such keeps `Page` honest
                # rather than inventing a stored column nothing would consult.
                is_redirect=False,
            )
        if found:
            conn.execute(
                f"UPDATE page_cache SET last_used=? "
                f"WHERE dump_run=? AND lang=? AND page_id IN ({placeholders})",
                (utcnow(), dump_run, lang, *chunk),
            )
    return found


def put_many(
    conn: sqlite3.Connection,
    pages: dict[int, Page],
    dump_run: str,
    lang: str = "en",
    stats: CacheStats | None = None,
) -> int:
    """Store pages read from the archive. Returns how many rows were written.

    Only pages an enrichment actually consumed are offered here, not all ~100 in
    the decompressed block: caching the neighbours would inflate the table by
    two orders of magnitude to speculate on candidates that mostly never appear.
    """
    if not pages:
        return 0
    now = utcnow()
    rows = [
        (page.page_id, lang, dump_run, page.title, page.wikitext,
         len(page.wikitext.encode("utf-8")), now, now)
        for page in pages.values()
        # A redirect carries no citation context, so caching one buys nothing
        # and would let a redirect stub masquerade as a cached article.
        if not page.is_redirect
    ]
    if not rows:
        return 0
    before = conn.total_changes
    conn.executemany(
        "INSERT OR REPLACE INTO page_cache "
        "(page_id, lang, dump_run, title, wikitext, content_bytes, cached_at, "
        " last_used) VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    written = conn.total_changes - before
    if stats is not None:
        stats.stored += written
        stats.bytes_stored += sum(r[5] for r in rows)
    return written


def total_bytes(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COALESCE(SUM(content_bytes), 0) FROM page_cache"
        ).fetchone()[0]
    )


def evict(conn: sqlite3.Connection, max_bytes: int, stats: CacheStats | None = None) -> int:
    """Drop least-recently-used rows until the cache fits its budget.

    Deleting a cached page costs time to regenerate and nothing else, so there
    is no need for the care an eviction policy would otherwise deserve. A
    non-positive budget means unbounded, which is a legitimate choice on a
    machine with disk to spare.
    """
    if max_bytes <= 0:
        return 0
    current = total_bytes(conn)
    if current <= max_bytes:
        return 0

    removed = 0
    # Oldest-used first, and only as many rows as the overshoot actually
    # requires. Deleting a whole batch because the *batch* was the unit would
    # evict pages that were within budget — cheap to regenerate, but it would
    # quietly make the cache useless at any size near the cap.
    while current > max_bytes:
        candidates = conn.execute(
            "SELECT page_id, lang, dump_run, content_bytes FROM page_cache "
            "ORDER BY last_used, page_id LIMIT 100"
        ).fetchall()
        if not candidates:
            break
        victims = []
        for row in candidates:
            if current <= max_bytes:
                break
            victims.append(row)
            current -= row["content_bytes"]
        if not victims:
            break
        conn.executemany(
            "DELETE FROM page_cache WHERE page_id=? AND lang=? AND dump_run=?",
            [(v["page_id"], v["lang"], v["dump_run"]) for v in victims],
        )
        removed += len(victims)

    if stats is not None:
        stats.evicted += removed
    return removed


def clear(conn: sqlite3.Connection, dump_run: str | None = None) -> int:
    """Drop the cache, optionally for one run. Never touches observations."""
    if dump_run:
        cur = conn.execute("DELETE FROM page_cache WHERE dump_run=?", (dump_run,))
    else:
        cur = conn.execute("DELETE FROM page_cache")
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

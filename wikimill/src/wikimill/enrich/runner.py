"""Stage 6 — enrich: the expensive half, run only on what earned it.

The shape of this function is the argument for the whole pipeline ordering:

1. Count what is pending. **One indexed query.**
2. If nothing is pending, stop — without opening the archive or the index.
3. Otherwise sort candidates by byte offset, group them by compressed block, and
   decompress each block once for every candidate page inside it.

Step 2 is acceptance criterion 12, and it is the common case on a healthy
corpus. Step 3 is what makes the uncommon case affordable: one seek and one
~100-page decompression, not a scan of 26.6 GB.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..constants import EnrichStatus, RunKind
from ..errors import DumpError
from ..logging import RunLog, utcnow
from ..policy import load as load_policy
from ..storage import open_db
from . import cache as cache_mod
from . import seek as seek_mod
from . import select as select_mod
from .wikitext import extract


@dataclass
class EnrichStats:
    pending: int = 0
    candidates: int = 0
    blocks_read: int = 0
    pages_parsed: int = 0
    enriched: int = 0
    page_missing: int = 0
    not_in_wikitext: int = 0
    dead_link_tagged: int = 0
    archived: int = 0
    kinds: Counter = field(default_factory=Counter)
    sections: Counter = field(default_factory=Counter)
    cache: cache_mod.CacheStats = field(default_factory=lambda: cache_mod.CacheStats())


def _apply(
    conn: sqlite3.Connection,
    link_id: int,
    dump_run: str,
    context,
    status: str,
    stats: EnrichStats,
) -> None:
    """Write one link's context back. `external_links` is not append-only —
    only the observation tables are — so filling these columns is legitimate."""
    conn.execute(
        "UPDATE external_links SET section=?, section_level=?, anchor_text=?, "
        " link_kind=?, ref_name=?, template_name=?, context_excerpt=?, "
        " dead_link_tagged=?, archive_url=COALESCE(?, archive_url), "
        " archive_date=COALESCE(?, archive_date), enriched_at=?, "
        " enrich_dump_run=?, enrich_status=? WHERE id=?",
        (
            context.section if context else None,
            context.section_level if context else None,
            context.anchor_text if context else None,
            context.link_kind if context else None,
            context.ref_name if context else None,
            context.template_name if context else None,
            context.context_excerpt if context else None,
            int(bool(context.dead_link_tagged)) if context else 0,
            context.archive_url if context else None,
            context.archive_date if context else None,
            utcnow(),
            dump_run,
            status,
            link_id,
        ),
    )
    if status == EnrichStatus.DONE and context:
        stats.enriched += 1
        if context.link_kind:
            stats.kinds[context.link_kind] += 1
        if context.section:
            stats.sections[context.section] += 1
        if context.dead_link_tagged:
            stats.dead_link_tagged += 1
        if context.archive_url:
            stats.archived += 1
    elif status == EnrichStatus.PAGE_MISSING:
        stats.page_missing += 1
    elif status == EnrichStatus.URL_NOT_FOUND_IN_WIKITEXT:
        stats.not_in_wikitext += 1


def run(
    cfg: Config,
    log: RunLog,
    *,
    states: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    no_cache: bool = False,
) -> EnrichStats:
    """Execute the enrichment stage."""
    stats = EnrichStats()
    policy = load_policy(cfg.root)
    trigger_states = select_mod.parse_states(states, policy)

    with open_db(cfg.db_path) as conn:
        # --- the fast path -------------------------------------------------
        # One indexed count, before anything touches a dump file.
        stats.pending = select_mod.count_pending(conn, trigger_states, policy=policy)
        log.ok("trigger states", ", ".join(trigger_states))
        if stats.pending == 0:
            log.warn(
                "nothing to enrich",
                "no links in those states are awaiting context — "
                "neither the archive nor the index was opened",
            )
            return stats

        candidates = select_mod.select(conn, trigger_states, limit=limit, policy=policy)
        stats.candidates = len(candidates)
        blocks = select_mod.group_by_block(candidates)
        log.ok(
            "candidates",
            f"{len(candidates):,} link(s) across {len(blocks):,} compressed block(s)",
        )

        # --- what the cache already holds (v2.H) ---------------------------
        # Resolved before the archive is located, because if it covers every
        # candidate there is no archive to locate. That is the point of the
        # cache: an extraction-rule improvement becomes re-runnable with the
        # drive unplugged, the same way `--reclassify` is.
        use_cache = policy.enrich.cache_enabled and not no_cache
        cached: dict[tuple[str, int], object] = {}
        if use_cache:
            by_run: dict[str, set[int]] = {}
            for candidate in candidates:
                by_run.setdefault(candidate.dump_run, set()).add(candidate.page_id)
            for run_id, page_ids in by_run.items():
                for page_id, page in cache_mod.get_many(conn, page_ids, run_id).items():
                    cached[(run_id, page_id)] = page
            stats.cache.hits = len(cached)
            stats.cache.misses = len({
                (c.dump_run, c.page_id) for c in candidates
            }) - len(cached)

        # A block only needs reading if some candidate in it missed the cache.
        pending_blocks = [
            (offset, group)
            for offset, group in blocks
            if any((c.dump_run, c.page_id) not in cached for c in group)
        ]
        if use_cache and stats.cache.hits:
            log.ok(
                "cache",
                f"{stats.cache.hits:,} page(s) already stored · "
                f"{len(blocks) - len(pending_blocks):,} of {len(blocks):,} block(s) "
                "need no archive read",
            )

        # Cost is visible before it is paid. A dry run must not *require* the
        # archive: "you would need the dump, and it is not here" is the answer
        # to the question, not a reason to refuse to answer it — and asking
        # what a run would cost is exactly what an operator does before
        # deciding whether to go and plug the drive in.
        if dry_run:
            log.warn("dry run", "nothing was read or written")
            if not pending_blocks:
                log.progress("would open no archive — every page is cached")
                return stats
            try:
                archive = seek_mod.find_archive(cfg.dumps_dir)
                log.progress(f"would open {archive.name}")
            except DumpError as exc:
                log.warn("archive", f"would be needed, but {exc}")
            log.progress(f"would decompress {len(pending_blocks):,} block(s)")
            return stats

        archive = None
        if pending_blocks:
            archive = seek_mod.find_archive(cfg.dumps_dir)
            log.ok("archive", archive.name)
        else:
            log.ok(
                "archive",
                "not opened — every candidate page came from the cache",
            )

        conn.execute("BEGIN")
        for index, (offset, group) in enumerate(blocks, 1):
            pages: dict[int, object] = {}
            needs_read = any((c.dump_run, c.page_id) not in cached for c in group)
            if needs_read and archive is not None:
                try:
                    pages = seek_mod.pages_at(archive, offset)
                except Exception as exc:  # noqa: BLE001 — one bad block is not fatal
                    log.warn("block", f"offset {offset}: {exc}")
                    for candidate in group:
                        _apply(conn, candidate.link_id, "", None,
                               EnrichStatus.PAGE_MISSING, stats)
                    continue
                stats.blocks_read += 1
                stats.pages_parsed += len(pages)

            for candidate in group:
                page = cached.get((candidate.dump_run, candidate.page_id))
                from_cache = page is not None
                if page is None:
                    page = pages.get(candidate.page_id)
                if page is None:
                    # Deleted or moved between dump runs.
                    _apply(conn, candidate.link_id, "", None,
                           EnrichStatus.PAGE_MISSING, stats)
                    continue
                if use_cache and not from_cache:
                    cache_mod.put_many(
                        conn, {candidate.page_id: page}, candidate.dump_run,
                        stats=stats.cache,
                    )
                    cached[(candidate.dump_run, candidate.page_id)] = page
                context = extract(page.wikitext, candidate.url_raw)
                status = (
                    EnrichStatus.DONE
                    if context.found
                    else EnrichStatus.URL_NOT_FOUND_IN_WIKITEXT
                )
                _apply(conn, candidate.link_id, candidate.dump_run, context,
                       status, stats)

            if index % 5 == 0 or index == len(blocks):
                log.progress(
                    f"block {index:,}/{len(blocks):,} · "
                    f"{stats.enriched:,} enriched"
                )

        if use_cache:
            cache_mod.evict(conn, policy.enrich.cache_max_bytes, stats.cache)
        conn.execute("COMMIT")

        conn.execute(
            "INSERT OR REPLACE INTO crawl_runs "
            "(run_id, kind, started_at, ended_at, counts, outcome) VALUES (?,?,?,?,?,?)",
            (
                log.run_id,
                RunKind.ENRICH,
                log.started_at,
                utcnow(),
                json.dumps(
                    {
                        "candidates": stats.candidates,
                        "blocks_read": stats.blocks_read,
                        "enriched": stats.enriched,
                    }
                ),
                "failed" if log.failed else "ok",
            ),
        )

    log.ok(
        "enriched",
        f"{stats.enriched:,} link(s) from {stats.blocks_read:,} block(s) "
        f"({stats.pages_parsed:,} pages decompressed)",
    )
    if stats.not_in_wikitext:
        log.warn(
            "no literal occurrence",
            f"{stats.not_in_wikitext:,} — template-expanded links, an expected "
            "outcome rather than a failure",
        )
    if stats.page_missing:
        log.warn("page missing", f"{stats.page_missing:,} not present in their block")
    if stats.dead_link_tagged:
        log.ok(
            "{{dead link}}",
            f"{stats.dead_link_tagged:,} already flagged by Wikipedia editors",
        )
    if stats.archived:
        log.ok("archive replacement", f"{stats.archived:,} carry an archive-url")
    if stats.cache.stored:
        log.ok(
            "cached",
            f"{stats.cache.stored:,} page(s) stored for offline re-enrichment "
            f"({stats.cache.bytes_stored / 1024:,.0f} KB)",
        )
    if stats.cache.evicted:
        log.warn(
            "cache evicted",
            f"{stats.cache.evicted:,} least-recently-used page(s) over budget",
        )
    if stats.kinds:
        log.note("")
        log.note("link kinds:")
        for name, count in stats.kinds.most_common():
            log.progress(f"{name:<26} {count:>6,}")
    return stats

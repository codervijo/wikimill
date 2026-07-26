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
from ..logging import RunLog, utcnow
from ..storage import open_db
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
) -> EnrichStats:
    """Execute the enrichment stage."""
    stats = EnrichStats()
    trigger_states = select_mod.parse_states(states)

    with open_db(cfg.db_path) as conn:
        # --- the fast path -------------------------------------------------
        # One indexed count, before anything touches a dump file.
        stats.pending = select_mod.count_pending(conn, trigger_states)
        log.ok("trigger states", ", ".join(trigger_states))
        if stats.pending == 0:
            log.warn(
                "nothing to enrich",
                "no links in those states are awaiting context — "
                "neither the archive nor the index was opened",
            )
            return stats

        candidates = select_mod.select(conn, trigger_states, limit=limit)
        stats.candidates = len(candidates)
        blocks = select_mod.group_by_block(candidates)
        log.ok(
            "candidates",
            f"{len(candidates):,} link(s) across {len(blocks):,} compressed block(s)",
        )

        # Cost is visible before it is paid.
        if dry_run:
            archive = seek_mod.find_archive(cfg.dumps_dir)
            log.warn("dry run", "nothing was read or written")
            log.progress(f"would open {archive.name}")
            log.progress(
                f"would decompress {len(blocks):,} block(s) for "
                f"{len({c.page_id for c in candidates}):,} page(s)"
            )
            return stats

        archive = seek_mod.find_archive(cfg.dumps_dir)
        log.ok("archive", archive.name)

        conn.execute("BEGIN")
        for index, (offset, group) in enumerate(blocks, 1):
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
                page = pages.get(candidate.page_id)
                if page is None:
                    # Deleted or moved between dump runs.
                    _apply(conn, candidate.link_id, "", None,
                           EnrichStatus.PAGE_MISSING, stats)
                    continue
                context = extract(page.wikitext, candidate.url_raw)
                status = (
                    EnrichStatus.DONE
                    if context.found
                    else EnrichStatus.URL_NOT_FOUND_IN_WIKITEXT
                )
                dump_run = conn.execute(
                    "SELECT dump_run FROM external_links WHERE id=?",
                    (candidate.link_id,),
                ).fetchone()["dump_run"]
                _apply(conn, candidate.link_id, dump_run, context, status, stats)

            if index % 5 == 0 or index == len(blocks):
                log.progress(
                    f"block {index:,}/{len(blocks):,} · "
                    f"{stats.enriched:,} enriched"
                )
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
    if stats.kinds:
        log.note("")
        log.note("link kinds:")
        for name, count in stats.kinds.most_common():
            log.progress(f"{name:<26} {count:>6,}")
    return stats

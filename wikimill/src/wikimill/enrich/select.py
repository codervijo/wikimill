"""Choosing what deserves enrichment — and, far more often, what does not.

This module exists to answer one question cheaply: *is there anything worth
opening the 26.6 GB archive for?* On a healthy corpus the answer is usually no,
and answering it must cost a single indexed query — not a dump read.

Acceptance criterion 12 is written against this file: on a subset with zero dead
links, `enrich` must exit having opened **neither the archive nor the index**.
That is the whole cheapest-first design made testable, so the empty path is the
first thing built here and the first thing tested.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..constants import ENRICH_TRIGGER_STATES, EnrichStatus


@dataclass(frozen=True)
class Candidate:
    """One link occurrence worth spending a block decompression on."""

    link_id: int
    page_id: int
    ms_offset: int
    title: str
    url_raw: str
    url_state: str


def parse_states(states: str | None) -> list[str]:
    """Resolve the trigger set. Defaults to everything except healthy states.

    The default deliberately excludes `live` and same-domain `redirect`: paying
    to extract context for a working link is exactly the work this pipeline is
    ordered to avoid.
    """
    if not states:
        return sorted(ENRICH_TRIGGER_STATES)
    return [s.strip() for s in states.split(",") if s.strip()]


def count_pending(
    conn: sqlite3.Connection, states: list[str], dump_run: str | None = None
) -> int:
    """How many link occurrences are awaiting enrichment. One indexed query.

    Called before anything opens a dump file, so the common "nothing is dead"
    case costs a count and nothing else.
    """
    params: list = list(states)
    sql = [
        "SELECT COUNT(*) FROM external_links e",
        "JOIN urls u ON u.url_hash = e.url_hash",
        "WHERE u.state IN (" + ",".join("?" * len(states)) + ")",
        "AND (e.enrich_status = ? OR e.enrich_dump_run IS NOT e.dump_run)",
    ]
    params.append(EnrichStatus.PENDING)
    if dump_run:
        sql.append("AND e.dump_run = ?")
        params.append(dump_run)
    return int(conn.execute("\n".join(sql), params).fetchone()[0])


def select(
    conn: sqlite3.Connection,
    states: list[str],
    *,
    limit: int | None = None,
    dump_run: str | None = None,
) -> list[Candidate]:
    """Candidates, **ordered by byte offset**.

    Offset order is not cosmetic. Enrichment seeks into a multi-hundred-megabyte
    archive once per candidate page; sorting by offset lets the runner group
    pages that share a compressed block, so one seek and one decompress serves
    all ~100 of them. On an SSD that is a modest win; on the spinning external
    drive this project expects, it is the difference between minutes and hours.
    """
    params: list = list(states)
    sql = [
        "SELECT e.id, e.page_id, p.ms_offset, p.title, e.url_raw, u.state",
        "FROM external_links e",
        "JOIN urls u ON u.url_hash = e.url_hash",
        "JOIN wiki_pages p ON p.page_id = e.page_id AND p.dump_run = e.dump_run",
        "WHERE u.state IN (" + ",".join("?" * len(states)) + ")",
        "AND e.enrich_status = ?",
        "AND p.ms_offset IS NOT NULL",
    ]
    params.append(EnrichStatus.PENDING)
    if dump_run:
        sql.append("AND e.dump_run = ?")
        params.append(dump_run)
    sql.append("ORDER BY p.ms_offset, e.page_id, e.id")
    if limit is not None:
        sql.append("LIMIT ?")
        params.append(limit)
    return [
        Candidate(r["id"], r["page_id"], r["ms_offset"], r["title"], r["url_raw"], r["state"])
        for r in conn.execute("\n".join(sql), params)
    ]


def group_by_block(candidates: list[Candidate]) -> list[tuple[int, list[Candidate]]]:
    """Group candidates by the compressed block that holds their page.

    Each multistream block holds ~100 pages, so several candidates routinely
    share one. Grouping turns N seeks into one.
    """
    blocks: dict[int, list[Candidate]] = {}
    for candidate in candidates:
        blocks.setdefault(candidate.ms_offset, []).append(candidate)
    return sorted(blocks.items())

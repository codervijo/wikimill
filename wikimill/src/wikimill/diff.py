"""Stage 7 — cross-dump-run diff (v2.G).

**A link removed from Wikipedia is its own signal.** Editors take citations out
for reasons that correlate strongly with what this tool is looking for: the site
went dead, the content vanished, the domain got parked and started serving ads,
a bot replaced it with an archive. Wikipedia's editor base is, in effect, a
large and unusually careful dead-link detector that has already done the work —
and the SQL dumps record its output for free, with no requests to anyone.

It is *corroboration*, never a verdict. Links also get removed because a
paragraph was rewritten, a source was upgraded, or a spam sweep caught the whole
domain. So removal contributes points to a domain's score and appears in the
export as evidence; it never sets a state.

## Why only pages present in both runs are compared

This tool ingests slices (`--pages p1p41242`, `--limit`). A page absent from the
newer run may have been deleted, or may simply never have been ingested — and
from inside the database those are indistinguishable while meaning opposite
things. Comparing them anyway would manufacture thousands of "removed" signals
out of an operator's decision to ingest half a dump, which is the exact failure
this project treats as most expensive: a false positive the operator acts on.

So the comparison is scoped to the intersection of the two runs' pages, and
everything outside it is reported as *not comparable* — a coverage number, not
a transition. `page_deleted` is therefore absent by design, not by omission.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .logging import utcnow

REMOVED = "removed"
ADDED = "added"


@dataclass
class DiffStats:
    from_run: str = ""
    to_run: str = ""
    pages_compared: int = 0
    pages_not_comparable: int = 0
    removed: int = 0
    added: int = 0
    domains_touched: int = 0

    @property
    def comparable(self) -> bool:
        return self.pages_compared > 0


def list_runs(conn: sqlite3.Connection) -> list[str]:
    """Every ingested dump run, oldest first.

    Read from `wiki_pages`, not `external_links`: a run is defined by what was
    *indexed*, and ingest always writes the slice's pages. Keying on links
    instead would make a run whose links were all removed — precisely the run
    this stage cares about — invisible to the comparison.

    Dump runs are `YYYYMMDD`, so lexical order is chronological order.
    """
    return [
        r["dump_run"]
        for r in conn.execute(
            "SELECT DISTINCT dump_run FROM wiki_pages ORDER BY dump_run"
        )
    ]


def previous_run(conn: sqlite3.Connection, run: str) -> str | None:
    """The newest run older than `run`, or None if it is the first."""
    row = conn.execute(
        "SELECT MAX(dump_run) AS prev FROM wiki_pages WHERE dump_run < ?",
        (run,),
    ).fetchone()
    return row["prev"] if row and row["prev"] else None


def _page_overlap(conn: sqlite3.Connection, from_run: str, to_run: str) -> tuple[int, int]:
    """(pages in both runs, pages in the older run that the newer never saw)."""
    row = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM (
              SELECT page_id, lang FROM wiki_pages WHERE dump_run = ?
              INTERSECT
              SELECT page_id, lang FROM wiki_pages WHERE dump_run = ?
          )) AS both,
          (SELECT COUNT(*) FROM (
              SELECT page_id, lang FROM wiki_pages WHERE dump_run = ?
              EXCEPT
              SELECT page_id, lang FROM wiki_pages WHERE dump_run = ?
          )) AS only_old
        """,
        (from_run, to_run, from_run, to_run),
    ).fetchone()
    return row["both"] or 0, row["only_old"] or 0


# One shared shape for both directions: the transition is just which run is
# treated as the source. Writing it once keeps `added` and `removed` from
# drifting apart, which is easy to do and impossible to notice.
_TRANSITION_SQL = """
    INSERT OR IGNORE INTO link_diffs
        (url_hash, page_id, lang, from_run, to_run, transition, observed_at)
    SELECT src.url_hash, src.page_id, src.lang, ?, ?, ?, ?
    FROM external_links src
    -- Only pages the *other* run also covers; see the module docstring.
    JOIN wiki_pages pa ON pa.page_id = src.page_id AND pa.lang = src.lang
                      AND pa.dump_run = ?
    JOIN wiki_pages pb ON pb.page_id = src.page_id AND pb.lang = src.lang
                      AND pb.dump_run = ?
    WHERE src.dump_run = ?
      AND NOT EXISTS (
          SELECT 1 FROM external_links other
          WHERE other.dump_run = ?
            AND other.page_id = src.page_id
            AND other.lang = src.lang
            AND other.url_hash = src.url_hash
      )
"""


def compute(
    conn: sqlite3.Connection, from_run: str, to_run: str, *, now: str | None = None
) -> DiffStats:
    """Record what changed between two ingested runs. No network, no dump reads.

    Idempotent: `INSERT OR IGNORE` against the uniqueness constraint, so
    re-running over the same pair adds nothing and changes nothing.
    """
    stats = DiffStats(from_run=from_run, to_run=to_run)
    if from_run == to_run:
        return stats

    stamp = now or utcnow()
    stats.pages_compared, stats.pages_not_comparable = _page_overlap(
        conn, from_run, to_run
    )
    if not stats.comparable:
        return stats

    before = conn.total_changes
    # In the old run, absent from the new one → an editor took it out.
    conn.execute(
        _TRANSITION_SQL,
        (from_run, to_run, REMOVED, stamp, from_run, to_run, from_run, to_run),
    )
    stats.removed = conn.total_changes - before

    before = conn.total_changes
    # In the new run, absent from the old → a fresh citation.
    conn.execute(
        _TRANSITION_SQL,
        (from_run, to_run, ADDED, stamp, from_run, to_run, to_run, from_run),
    )
    stats.added = conn.total_changes - before

    stats.domains_touched = conn.execute(
        """
        SELECT COUNT(DISTINCT u.domain_id) AS n
        FROM link_diffs d JOIN urls u ON u.url_hash = d.url_hash
        WHERE d.from_run = ? AND d.to_run = ? AND u.domain_id IS NOT NULL
        """,
        (from_run, to_run),
    ).fetchone()["n"] or 0
    return stats


def removal_counts(conn: sqlite3.Connection, domain_id: int) -> int:
    """How many of this domain's links Wikipedia editors have dropped.

    Counted across every run pair, distinct per (url, page): a link removed once
    and compared twice is one removal, not two.
    """
    return conn.execute(
        """
        SELECT COUNT(*) AS n FROM (
            SELECT DISTINCT d.url_hash, d.page_id, d.lang
            FROM link_diffs d JOIN urls u ON u.url_hash = d.url_hash
            WHERE u.domain_id = ? AND d.transition = ?
        )
        """,
        (domain_id, REMOVED),
    ).fetchone()["n"] or 0


def summary(conn: sqlite3.Connection, from_run: str, to_run: str) -> dict:
    """Stored diff totals for one run pair, for `stats --diff`."""
    rows = conn.execute(
        "SELECT transition, COUNT(*) AS n FROM link_diffs "
        "WHERE from_run = ? AND to_run = ? GROUP BY transition",
        (from_run, to_run),
    ).fetchall()
    return {r["transition"]: r["n"] for r in rows}


def top_removed_domains(
    conn: sqlite3.Connection, from_run: str, to_run: str, limit: int = 10
) -> list[tuple[str, int]]:
    """Domains editors dropped most between two runs — where to look first."""
    return [
        (r["registrable_domain"], r["n"])
        for r in conn.execute(
            """
            SELECT dom.registrable_domain, COUNT(*) AS n
            FROM link_diffs d
            JOIN urls u ON u.url_hash = d.url_hash
            JOIN domains dom ON dom.domain_id = u.domain_id
            WHERE d.from_run = ? AND d.to_run = ? AND d.transition = ?
            GROUP BY dom.registrable_domain
            ORDER BY n DESC, dom.registrable_domain
            LIMIT ?
            """,
            (from_run, to_run, REMOVED, limit),
        )
    ]

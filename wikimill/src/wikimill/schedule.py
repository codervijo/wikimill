"""What the scheduler would pick, without running it (v2.E).

The selection rule in prd.md §12 is one line — `terminal = 0 AND next_check_at
<= now()`, ordered by value — but until now it was only observable by starting a
crawl and watching what came out. That makes the cheapest question the operator
has ("is there anything to do?") cost a run against real hosts.

So this module answers it from the database alone: no network, no fetch, no
side effects. `stats --due` is the surface.

It reports the *shape* of the queue rather than a single number on purpose. "120
due" says nothing actionable; "120 due, of which 3 are `for_sale`" says which
run is worth making. And the two horizons past now — a week, a month — are what
turn "nothing to do" into "nothing to do until Tuesday", which is the difference
between an idle check and a plan.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .logging import utcnow


@dataclass
class Bucket:
    """One queue's schedule. Counts are disjoint — every record lands in
    exactly one of never/due/soon/later/terminal, so they sum to the total."""

    never: int = 0
    due: int = 0
    soon: int = 0        # within SOON_DAYS
    later: int = 0
    terminal: int = 0
    due_by_state: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.never + self.due + self.soon + self.later + self.terminal

    @property
    def actionable(self) -> int:
        """What a `--limit`-free run would touch right now."""
        return self.never + self.due


SOON_DAYS = 7


def _bucket(conn: sqlite3.Connection, table: str, key: str, now: str) -> Bucket:
    """Tally one queue. `table`/`key` are caller-supplied, never user input."""
    b = Bucket()
    row = conn.execute(
        f"""
        SELECT
          SUM(CASE WHEN terminal = 1 THEN 1 ELSE 0 END)                      AS terminal,
          SUM(CASE WHEN terminal = 0 AND next_check_at IS NULL
                   THEN 1 ELSE 0 END)                                        AS never,
          SUM(CASE WHEN terminal = 0 AND next_check_at IS NOT NULL
                   AND next_check_at <= ? THEN 1 ELSE 0 END)                 AS due,
          SUM(CASE WHEN terminal = 0 AND next_check_at > ?
                   AND next_check_at <= datetime(?, ?) THEN 1 ELSE 0 END)    AS soon,
          SUM(CASE WHEN terminal = 0 AND next_check_at > datetime(?, ?)
                   THEN 1 ELSE 0 END)                                        AS later
        FROM {table}
        """,
        (now, now, now, f"+{SOON_DAYS} days", now, f"+{SOON_DAYS} days"),
    ).fetchone()
    b.terminal = row["terminal"] or 0
    b.never = row["never"] or 0
    b.due = row["due"] or 0
    b.soon = row["soon"] or 0
    b.later = row["later"] or 0

    # Which states are due matters more than how many: three due `for_sale`
    # records justify a run that 100,000 due `live` ones do not.
    b.due_by_state = {
        r["state"]: r["n"]
        for r in conn.execute(
            f"""
            SELECT state, COUNT(*) AS n FROM {table}
            WHERE terminal = 0 AND next_check_at IS NOT NULL AND next_check_at <= ?
            GROUP BY state ORDER BY n DESC
            """,
            (now,),
        )
    }
    return b


def snapshot(conn: sqlite3.Connection, now: str | None = None) -> dict[str, Bucket]:
    """Both queues, as of `now` (injectable so a test need not wait a day)."""
    stamp = now or utcnow()
    return {
        "urls": _bucket(conn, "urls", "url_hash", stamp),
        "domains": _bucket(conn, "domains", "domain_id", stamp),
    }

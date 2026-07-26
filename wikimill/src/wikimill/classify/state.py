"""The URL state machine: turning a verdict into a queue decision.

Two rules from prd.md §11 are easy to get backwards, so they are enforced here
rather than left to callers:

* **`unregistered` is not terminal — it is the most urgent recheck class.** It is
  the most volatile record in the database: anyone can register the domain
  tomorrow. "Permanently classified" must never be read as "unregistered".
* **`hard_404` does not kill the domain.** A 404 says the page is gone; the
  domain may be perfectly healthy. Only domain checks (v1.G) may set a domain
  state, and nothing here touches the `domains` table.
"""

from __future__ import annotations

import sqlite3

from ..constants import RECHECK_INTERVALS, UrlState
from ..logging import utcnow

# Consecutive identical `hard_404` verdicts before the URL stops being checked.
# More than one because a single 404 can be a deploy blip; not many more,
# because the page is almost certainly gone.
HARD_404_CONFIRMATIONS = 3

# Fallback when a classification has no configured cadence.
DEFAULT_RECHECK_SECS = 30 * 86_400

# Failure states that feed the per-URL consecutive-failure counter.
_FAILURE_STATES = frozenset(
    {
        UrlState.DNS_FAILURE,
        UrlState.TLS_FAILURE,
        UrlState.TEMPORARILY_UNAVAILABLE,
    }
)


def recheck_seconds(classification: str, *, repeats: int = 0) -> int:
    """How long until this URL is due again.

    `hard_404` doubles with each repeat (capped at 180 days) — a page that has
    been gone for a year does not need monthly confirmation. Everything else
    uses its configured cadence unchanged, because the value of a `parked` or
    `for_sale` observation is in its freshness.
    """
    base = RECHECK_INTERVALS.get(classification, DEFAULT_RECHECK_SECS)
    if classification == UrlState.HARD_404 and repeats > 0:
        return min(base * (2**repeats), 180 * 86_400)
    return base


def is_terminal(classification: str, consecutive: int) -> bool:
    """Only a repeatedly-confirmed `hard_404` stops being checked.

    Deliberately narrow. Anything the operator might act on stays in rotation —
    especially `unregistered`, which is checked *most* often, not least.
    """
    return (
        classification == UrlState.HARD_404
        and consecutive >= HARD_404_CONFIRMATIONS
    )


def consecutive_count(
    conn: sqlite3.Connection, url_hash: str, classification: str
) -> int:
    """How many times in a row this URL has drawn this verdict, most recent first."""
    rows = conn.execute(
        "SELECT classification FROM url_classifications WHERE url_hash=? "
        "ORDER BY id DESC LIMIT 20",
        (url_hash,),
    ).fetchall()
    count = 0
    for row in rows:
        if row["classification"] != classification:
            break
        count += 1
    return count


def record(
    conn: sqlite3.Connection,
    *,
    check_id: int,
    url_hash: str,
    verdict,
) -> None:
    """Append the verdict and advance the URL. Never updates `url_checks`."""
    import json

    now = utcnow()
    conn.execute(
        "INSERT OR IGNORE INTO url_classifications "
        "(check_id, url_hash, classified_at, classifier_version, classification, "
        " reasons, confidence) VALUES (?,?,?,?,?,?,?)",
        (
            check_id,
            url_hash,
            now,
            verdict.version,
            verdict.classification,
            json.dumps(verdict.reasons),
            verdict.confidence,
        ),
    )

    repeats = consecutive_count(conn, url_hash, verdict.classification)
    terminal = is_terminal(verdict.classification, repeats)
    seconds = recheck_seconds(verdict.classification, repeats=max(0, repeats - 1))
    failed = verdict.classification in _FAILURE_STATES

    conn.execute(
        "UPDATE urls SET state=?, terminal=?, next_check_at=datetime(?, ?), "
        "consecutive_failures=CASE WHEN ? THEN consecutive_failures + 1 ELSE 0 END "
        "WHERE url_hash=?",
        (
            verdict.classification,
            int(terminal),
            now,
            f"+{seconds} seconds",
            int(failed),
            url_hash,
        ),
    )

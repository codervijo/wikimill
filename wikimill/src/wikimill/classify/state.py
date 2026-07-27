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

from ..constants import (
    DEFAULT_RECHECK_SECS,
    HARD_404_BACKOFF_CAP_SECS,
    HARD_404_CONFIRMATIONS,
    RECHECK_INTERVALS,
    TRANSIENT_CAP_SECS,
    TRANSIENT_REQUEUE_SECS,
    UrlState,
)
from ..logging import utcnow
from ..policy import Policy


# Failure states that feed the per-URL consecutive-failure counter.
_FAILURE_STATES = frozenset(
    {
        UrlState.DNS_FAILURE,
        UrlState.TLS_FAILURE,
        UrlState.TEMPORARILY_UNAVAILABLE,
    }
)


def recheck_seconds(classification: str, *, repeats: int = 0, policy=None) -> int:
    """How long until this URL is due again.

    Two states escalate on repeat; everything else uses its configured cadence
    unchanged, because the value of a `parked` or `for_sale` observation is in
    its freshness.

    * `hard_404` doubles per repeat, capped — a page gone for a year does not
      need monthly confirmation.
    * `temporarily_unavailable` doubles from one hour, and once doubling would
      pass the cap it **stops escalating and re-queues at the weekly interval**
      (prd.md §12). Without that second step a host down for a week collects
      168 requests from us on the theory that it might come back any minute.
      That is a politeness failure, not just a scheduling one — the flat hourly
      retry is at its worst exactly when the far end is least able to serve it.
    """
    c = policy.classify if policy else None
    cadences = c.recheck_seconds if c else RECHECK_INTERVALS
    fallback = c.default_recheck_seconds if c else DEFAULT_RECHECK_SECS
    base = cadences.get(str(classification), fallback)

    if repeats <= 0:
        return base

    if classification == UrlState.HARD_404:
        cap = c.hard_404_backoff_cap_seconds if c else HARD_404_BACKOFF_CAP_SECS
        return min(base * (2**repeats), cap)

    if classification == UrlState.TEMPORARILY_UNAVAILABLE:
        cap = c.transient_cap_seconds if c else TRANSIENT_CAP_SECS
        escalated = base * (2**repeats)
        if escalated > cap:
            return c.transient_requeue_seconds if c else TRANSIENT_REQUEUE_SECS
        return escalated

    return base


def is_terminal(classification: str, consecutive: int, policy=None) -> bool:
    """Only a repeatedly-confirmed `hard_404` stops being checked.

    Deliberately narrow. Anything the operator might act on stays in rotation —
    especially `unregistered`, which is checked *most* often, not least.
    """
    return (
        classification == UrlState.HARD_404
        and consecutive >= (policy.classify.hard_404_confirmations if policy
                            else HARD_404_CONFIRMATIONS)
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
    policy=None,
) -> None:
    """Append the verdict and advance the URL. Never updates `url_checks`."""
    import json

    now = utcnow()
    # The *effective* version, not the bare constant: a verdict produced under a
    # tuned marker list is not the same verdict the shipped one would produce,
    # and `url_classifications` is unique on (check_id, classifier_version) —
    # so stamping the fingerprint is what lets a re-classify under new rules
    # append alongside the old call instead of silently being ignored as a
    # duplicate. Without this the whole fingerprinting scheme is decorative.
    version = (policy or Policy()).effective_classifier_version
    conn.execute(
        "INSERT OR IGNORE INTO url_classifications "
        "(check_id, url_hash, classified_at, classifier_version, classification, "
        " reasons, confidence) VALUES (?,?,?,?,?,?,?)",
        (
            check_id,
            url_hash,
            now,
            version,
            verdict.classification,
            json.dumps(verdict.reasons),
            verdict.confidence,
        ),
    )

    repeats = consecutive_count(conn, url_hash, verdict.classification)
    terminal = is_terminal(verdict.classification, repeats, policy)
    seconds = recheck_seconds(verdict.classification, repeats=max(0, repeats - 1), policy=policy)
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

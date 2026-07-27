"""Re-classify stored observations offline.

The payoff of keeping classification a pure function over stored evidence: when
the rules improve, every past observation can be re-judged **without refetching
a single URL**. No network, no load on anyone's server, and the old verdicts stay
on record so the effect of a rule change is visible rather than asserted.

Reached via `wikimill crawl --reclassify`, which adds no new verb — classify is
a sub-step of crawl in the stage contract (prd.md §8), not a stage of its own.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from ..config import Config
from ..logging import RunLog
from ..policy import load as load_policy
from ..storage import open_db
from . import state as state_mod
from .rules import Observation, classify


@dataclass
class ReclassifyStats:
    checks_seen: int = 0
    classified: int = 0
    already_current: int = 0
    changed: int = 0
    distribution: Counter = field(default_factory=Counter)
    changes: list[tuple[str, str, str]] = field(default_factory=list)


def latest_checks(conn: sqlite3.Connection, limit: int | None = None):
    """The most recent observation per URL, joined to its URL row.

    Only the latest: re-judging every historical check would multiply work for
    no gain, since the state machine only cares about the current verdict.
    """
    sql = """
        SELECT k.*, u.url_normalized
        FROM url_checks k
        JOIN urls u ON u.url_hash = k.url_hash
        WHERE k.id = (
            SELECT MAX(id) FROM url_checks k2 WHERE k2.url_hash = k.url_hash
        )
        ORDER BY k.id
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def run(
    cfg: Config,
    log: RunLog,
    *,
    limit: int | None = None,
    force: bool = False,
) -> ReclassifyStats:
    """Re-judge stored evidence. No network access whatsoever."""
    stats = ReclassifyStats()
    policy = load_policy(cfg.root)

    with open_db(cfg.db_path) as conn:
        rows = latest_checks(conn, limit)
        stats.checks_seen = len(rows)
        if not rows:
            log.warn("reclassify", "no observations stored yet — run `crawl` first")
            return stats

        log.ok(
            "reclassify",
            f"{len(rows):,} stored observation(s) · classifier "
            f"v{policy.effective_classifier_version} "
            "· no network",
        )

        conn.execute("BEGIN")
        for row in rows:
            existing = conn.execute(
                "SELECT classification, classifier_version FROM url_classifications "
                "WHERE check_id=? ORDER BY classifier_version DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            if (
                existing
                # Equality, not `>=`: policy versions are not ordered. Two
                # different marker lists at the same CLASSIFIER_VERSION are
                # different rules, and neither is "newer" than the other.
                and existing["classifier_version"] == policy.effective_classifier_version
                and not force
            ):
                stats.already_current += 1
                stats.distribution[existing["classification"]] += 1
                continue

            verdict = classify(Observation.from_row(row), policy)
            state_mod.record(
                conn, check_id=row["id"], url_hash=row["url_hash"], verdict=verdict,
                policy=policy,
            )
            stats.classified += 1
            stats.distribution[verdict.classification] += 1
            if existing and existing["classification"] != verdict.classification:
                stats.changed += 1
                if len(stats.changes) < 10:
                    stats.changes.append(
                        (
                            row["url_normalized"],
                            existing["classification"],
                            verdict.classification,
                        )
                    )
        conn.execute("COMMIT")

    if stats.classified:
        log.ok("classified", f"{stats.classified:,} observation(s)")
    if stats.already_current:
        log.warn(
            "skipped",
            f"{stats.already_current:,} already judged by classifier "
            f"v{policy.effective_classifier_version} (--force to redo)",
        )
    if stats.changed:
        log.warn("verdicts changed", f"{stats.changed:,} differ from the previous run")
        for url, was, now in stats.changes:
            log.progress(f"{was} → {now}   {url[:56]}")

    if stats.distribution:
        log.note("")
        log.note("classification distribution:")
        for name, count in stats.distribution.most_common():
            log.progress(f"{name:<26} {count:>7,}")
    return stats

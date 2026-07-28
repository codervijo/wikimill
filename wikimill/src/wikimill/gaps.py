"""Stage 9 — gaps: which dead citations can still be recovered? (v4.B)

Turns the pipeline around. Every earlier stage asks *which dead domains are
worth acquiring*; this one asks **which dead citations still have a copy
somewhere, and which are gone for good**. Same evidence, and the output is a
contribution back rather than an extraction.

## Why this needs no crawling

A citation pointing at an `unregistered` domain is dead by definition — the
domain does not exist, so neither does the page. The domain sweep already
established that for the whole corpus in under two hours, which means the
actionable population is available without touching the 25-day crawl. Measured
on the current corpus: **1,799 such citations across 1,301 distinct articles**.

As the crawl progresses, `hard_404` and `dns_failure` URLs join the same pool
automatically; nothing here has to change to pick them up.

## The funnel

```
citations on dead domains
  − URLs already carrying an archive_url        (free: Wikipedia told us)
  → ask the Wayback Availability API
        usable capture  → RECOVERABLE   an edit fixes the citation
        nothing         → LOST          irrecoverable
        no answer       → UNKNOWN       ask again later, never call it lost
```

The exclusion is the cheapest-first principle applied once more: 13.2% of all
citations already reference an archive, captured for free when normalization
unwrapped a `web.archive.org/…` wrapper at ingest. Asking the Internet Archive
about a URL Wikipedia has already archived spends someone else's capacity to
learn something we were told.

## Pacing, measured rather than assumed

The first live run asked at one request per second and drew **HTTP 429 on the
very first request** — all twenty came back refused. At fifteen seconds apart it
runs clean. The default therefore sits well past what demonstrably works, and it
is a floor rather than a target: nothing about this stage is time-critical, the
whole queue is only a few hundred URLs, and the far end is a nonprofit absorbing
the cost. A run that takes an afternoon is entirely acceptable.

Refusals also back off exponentially on top of that, and `CIRCUIT_THRESHOLD`
consecutive refusals stop the run outright. Continuing to ask while a service
says "slow down" is both rude and pointless — every answer would be an error —
and the unasked URLs simply stay queued.

## What this stage will not do

It finds gaps. It does not edit Wikipedia and does not submit anything to any
archive. Both are plausible next steps and both are *writes* against systems we
do not own — editing at scale needs WP:BOT approval, and the Wayback save
endpoint is a request to spend the Internet Archive's storage. Neither is a
side effect to acquire by accident.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from . import wayback as wayback_mod
from .config import Config
from .constants import DomainState, RunKind, UrlState
from .crawl.politeness import backoff_delay
from .logging import RunLog, utcnow
from .progress import Heartbeat, open_progress_db
from .storage import open_db

# URL-level verdicts that make a citation dead enough to be worth asking about.
# `parked` and `for_sale` are deliberately absent: the page is gone but the host
# answers, so the citation is broken rather than unreachable, and the archive
# question is a different one.
DEAD_URL_STATES: tuple[str, ...] = (
    UrlState.HARD_404,
    UrlState.DNS_FAILURE,
    UrlState.UNREGISTERED,
)

DEAD_DOMAIN_STATES: tuple[str, ...] = (DomainState.UNREGISTERED,)

RECOVERABLE, LOST, UNKNOWN = "recoverable", "lost", "unknown"

# Consecutive refusals before the run gives up. archive.org rate-limits this
# endpoint hard — the first live run drew HTTP 429 on request one — and
# continuing to ask 969 times while a service says "slow down" is both rude and
# pointless: every answer would be an error anyway. Stopping is the polite
# response and the useful one, because the URLs stay queued for a later run.
CIRCUIT_THRESHOLD = 5


@dataclass
class GapStats:
    selected: int = 0
    asked: int = 0
    recoverable: int = 0
    lost: int = 0
    unknown: int = 0
    archived_capture_unusable: int = 0
    citations_affected: int = 0
    articles_affected: int = 0
    waited_seconds: float = 0.0
    circuit_tripped: bool = False
    examples: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Target:
    url_hash: str
    url: str
    dump_run: str


def select_targets(
    conn: sqlite3.Connection, *, limit: int | None = None, force: bool = False,
    url_states: tuple[str, ...] = DEAD_URL_STATES,
    domain_states: tuple[str, ...] = DEAD_DOMAIN_STATES,
) -> list[Target]:
    """Dead URLs with no known archive, most-cited first.

    Ordered by citation count because a URL cited by forty articles is forty
    broken references, and if `--limit` cuts the run short those are the ones
    worth the requests.
    """
    params: list = [*url_states, *domain_states]
    sql = [
        "SELECT u.url_hash, u.url_normalized, MIN(e.dump_run) AS dump_run,",
        "       COUNT(*) AS cites",
        "FROM urls u",
        "JOIN external_links e ON e.url_hash = u.url_hash",
        "LEFT JOIN domains d ON d.domain_id = u.domain_id",
        "WHERE (u.state IN (" + ",".join("?" * len(url_states)) + ")",
        "   OR d.state IN (" + ",".join("?" * len(domain_states)) + "))",
        # Wikipedia already recorded an archive for this URL somewhere. Asking
        # the Internet Archive would spend their capacity to learn what we
        # already know.
        "AND NOT EXISTS (SELECT 1 FROM external_links a",
        "                WHERE a.url_hash = u.url_hash AND a.archive_url IS NOT NULL)",
    ]
    if not force:
        # An answered check is not re-asked. `lost` is worth revisiting one day
        # — someone may archive it — but that is a recheck cadence, not this.
        sql.append(
            "AND NOT EXISTS (SELECT 1 FROM archive_checks c "
            "                WHERE c.url_hash = u.url_hash AND c.error_kind IS NULL)"
        )
    sql.append("GROUP BY u.url_hash, u.url_normalized")
    sql.append("ORDER BY cites DESC, u.url_hash")
    if limit is not None:
        sql.append("LIMIT ?")
        params.append(limit)
    return [
        Target(r["url_hash"], r["url_normalized"], r["dump_run"] or "")
        for r in conn.execute("\n".join(sql), params)
    ]


def record(conn: sqlite3.Connection, target: Target,
           result: wayback_mod.Availability, endpoint: str) -> None:
    """Append one observation. A failed check is stored *as a failure*."""
    conn.execute(
        "INSERT INTO archive_checks (url_hash, checked_at, has_snapshot, "
        " snapshot_url, snapshot_timestamp, snapshot_status, requested_timestamp, "
        " api_endpoint, error_kind, latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            target.url_hash,
            utcnow(),
            # None, not 0, when we could not ask — see the schema comment.
            None if result.has_snapshot is None else int(result.has_snapshot),
            result.snapshot_url,
            result.snapshot_timestamp,
            result.snapshot_status,
            result.requested_timestamp,
            endpoint,
            result.error_kind,
            result.latency_ms,
        ),
    )


def latest(conn: sqlite3.Connection, url_hash: str) -> sqlite3.Row | None:
    """The most recent answered check for one URL."""
    return conn.execute(
        "SELECT * FROM archive_checks WHERE url_hash=? AND error_kind IS NULL "
        "ORDER BY id DESC LIMIT 1",
        (url_hash,),
    ).fetchone()


def verdict(row: sqlite3.Row | None) -> str:
    """recoverable / lost / unknown for one stored check."""
    if row is None or row["has_snapshot"] is None:
        return UNKNOWN
    if not row["has_snapshot"]:
        return LOST
    status = row["snapshot_status"]
    if status and status not in wayback_mod.USABLE_SNAPSHOT_STATUSES:
        # The archive faithfully preserved a not-found page. Nothing recovered.
        return LOST
    return RECOVERABLE


def _timestamp_for(dump_run: str) -> str | None:
    """The dump run is `YYYYMMDD`, which is exactly what the API wants."""
    return dump_run if dump_run and dump_run.isdigit() else None


def run(
    cfg: Config,
    log: RunLog,
    *,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    endpoint: str | None = None,
    delay: float = 15.0,
    client=None,
    sleep=time.sleep,
) -> GapStats:
    """Ask the archive about dead citations, serially and slowly."""
    from .crawl.fetcher import build_client

    stats = GapStats()
    target_endpoint = endpoint or wayback_mod.DEFAULT_ENDPOINT
    user_agent = wayback_mod.require_identity(cfg.user_agent)

    with open_db(cfg.db_path) as conn:
        targets = select_targets(conn, limit=limit, force=force)
        stats.selected = len(targets)
        if not targets:
            log.warn(
                "nothing to check",
                "no dead citation is missing a known archive — nothing was requested",
            )
            return stats

        stats.citations_affected, stats.articles_affected = _reach(
            conn, [t.url_hash for t in targets]
        )
        log.ok(
            "gaps",
            f"{len(targets):,} dead URL(s) with no known archive · "
            f"{stats.citations_affected:,} citation(s) in "
            f"{stats.articles_affected:,} article(s)",
        )
        log.ok("archive", f"{target_endpoint} · serial, {delay:.1f}s apart")

        if dry_run:
            log.warn("dry run", "nothing was requested or written")
            log.progress(f"would make {len(targets):,} request(s)")
            return stats

        owned = client is None
        if owned:
            client = build_client(
                headers={"User-Agent": user_agent, "Accept": "application/json"},
                follow_redirects=True,
            )
        beat_conn = open_progress_db(cfg.state_dir)
        beat = Heartbeat(beat_conn, log.run_id, "gaps", total=len(targets),
                         phase="starting")
        consecutive = 0
        try:
            for index, target in enumerate(targets, 1):
                if index > 1 and delay > 0:
                    sleep(delay)
                    stats.waited_seconds += delay

                beat.done = index - 1
                beat.advance(0, phase="asking the archive",
                             current_item=target.url[:120])

                result = wayback_mod.check_url(
                    client, target.url,
                    timestamp=_timestamp_for(target.dump_run),
                    endpoint=target_endpoint,
                )
                conn.execute("BEGIN")
                record(conn, target, result, target_endpoint)
                conn.execute("COMMIT")

                if not result.answered:
                    stats.unknown += 1
                    consecutive += 1
                    log.warn("no answer", f"{target.url[:56]}: {result.error_kind}")

                    # Their Retry-After wins; otherwise back off exponentially
                    # rather than returning at the same rate that was refused.
                    wait = max(result.retry_after,
                               backoff_delay(consecutive, base=delay or 1.0))
                    if wait:
                        log.progress(
                            f"backing off {wait:.0f}s "
                            f"({'Retry-After' if result.retry_after else 'exponential'})"
                        )
                        sleep(wait)
                        stats.waited_seconds += wait

                    if consecutive >= CIRCUIT_THRESHOLD:
                        stats.circuit_tripped = True
                        log.fail(
                            "giving up",
                            f"{consecutive} refusals in a row — the archive is "
                            f"rate-limiting us. {len(targets) - index:,} URL(s) stay "
                            "queued; re-run later or raise [gaps] delay_seconds.",
                        )
                        break
                    continue

                consecutive = 0
                stats.asked += 1
                if result.recoverable:
                    stats.recoverable += 1
                else:
                    stats.lost += 1
                    if result.has_snapshot:
                        # A capture exists but preserves a 404. Counted apart,
                        # because "archived" and "recovered" are not the same.
                        stats.archived_capture_unusable += 1
                    if len(stats.examples) < 10:
                        stats.examples.append((target.url, result.snapshot_status or "—"))

                if index % 25 == 0 or index == len(targets):
                    log.progress(
                        f"{index:,}/{len(targets):,} · "
                        f"{stats.recoverable:,} recoverable, {stats.lost:,} lost"
                    )
            beat.finish("ok")
        except BaseException as exc:
            beat.finish("failed", note=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            beat_conn.close()
            if owned:
                client.close()

        conn.execute(
            "INSERT OR REPLACE INTO crawl_runs "
            "(run_id, kind, started_at, ended_at, counts, outcome) VALUES (?,?,?,?,?,?)",
            (
                f"{log.run_id}-gaps",
                RunKind.EXPORT,
                log.started_at,
                utcnow(),
                repr({
                    "selected": stats.selected,
                    "recoverable": stats.recoverable,
                    "lost": stats.lost,
                    "unknown": stats.unknown,
                }),
                "failed" if log.failed else "ok",
            ),
        )

    _report(log, stats)
    return stats


def _reach(conn: sqlite3.Connection, hashes: list[str]) -> tuple[int, int]:
    """(citations, distinct articles) affected by these URLs — the human cost.

    Article IDs are accumulated into a set rather than summing a per-chunk
    `COUNT(DISTINCT)`: chunking is forced by SQLite's parameter limit, and an
    article citing two of these URLs from different chunks would otherwise be
    counted twice. Overstating how much of Wikipedia is affected is the wrong
    direction to be wrong in.
    """
    citations = 0
    pages: set[int] = set()
    for start in range(0, len(hashes), 500):
        chunk = hashes[start:start + 500]
        placeholders = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT page_id FROM external_links WHERE url_hash IN ({placeholders})",
            chunk,
        ):
            citations += 1
            pages.add(row["page_id"])
    return citations, len(pages)


def _report(log: RunLog, stats: GapStats) -> None:
    if stats.recoverable:
        log.ok(
            "recoverable",
            f"{stats.recoverable:,} citation(s) have a usable archived copy — "
            "an edit fixes each one",
        )
    if stats.lost:
        # The finding this stage exists to produce.
        log.warn(
            "lost",
            f"{stats.lost:,} citation(s) have no usable copy anywhere",
        )
    if stats.archived_capture_unusable:
        log.warn(
            "archived, not recovered",
            f"{stats.archived_capture_unusable:,} have a capture that preserves "
            "an error page — archived is not the same as recovered",
        )
    if stats.circuit_tripped:
        log.warn(
            "incomplete",
            "the run stopped early — nothing was recorded as lost on the "
            "strength of a refused request",
        )
    if stats.unknown:
        log.warn(
            "unknown",
            f"{stats.unknown:,} could not be checked — stored as errors, never "
            "as \"no copy exists\"",
        )
    for url, status in stats.examples[:5]:
        log.progress(f"lost (capture status {status})   {url[:58]}")

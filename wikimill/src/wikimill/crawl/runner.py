"""Stage 3 — crawl: walk due URLs and record what happened.

Two structural decisions carry this module:

1. **Work is partitioned by registrable domain**, and each partition is handled
   by exactly one worker. Per-domain concurrency of 1 is therefore impossible to
   violate — it is a property of the shape, not a lock that a later change could
   drop. Parallelism comes from crawling *many different* domains at once, which
   is the only kind that is polite anyway.
2. **One writer.** Workers only fetch; every database write happens on the main
   thread as results arrive. No SQLite threading pragmas, no write lock, and
   Ctrl-C can never interrupt a half-written batch.

Classification (stage 4, v1.F) runs inline here from the evidence each row just
stored — never from the live response object — so `crawl --reclassify` reaches
exactly the same verdict later, offline, with no refetching.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit

from .. import __version__
from ..classify import state as classify_state
from ..classify.rules import Observation, classify
from ..config import Config
from ..constants import RunKind, UrlState
from ..errors import CrawlError
from ..logging import RunLog, utcnow
from ..policy import load as load_policy
from ..progress import Heartbeat
from ..score import URL_DEATH_POINTS, priority_case
from ..storage import open_db
from . import robots as robots_mod
from .fetcher import FetchResult, build_client, fetch
from .politeness import Politeness, backoff_delay, should_retry

# Results per commit. Small enough that a crash costs little, large enough that
# committing is not the bottleneck.
CHECKPOINT_EVERY = 25


@dataclass
class CrawlStats:
    considered: int = 0
    fetched: int = 0
    blocked_by_robots: int = 0
    failed: int = 0
    retried: int = 0
    hosts_tripped: int = 0
    skipped_not_due: int = 0
    verdicts: Counter = field(default_factory=Counter)


@dataclass
class _Task:
    url_hash: str
    url: str
    domain: str


def select_due(
    conn: sqlite3.Connection, limit: int | None, force: bool, policy=None
) -> list[_Task]:
    """Pick non-terminal URLs whose recheck window has opened.

    Terminal records are excluded unless `--force`, which is the only way to
    revisit them (prd.md §12).

    Ordered by **candidate value, then oldest first** (v2.E). Never-checked URLs
    still lead — a URL with no observation at all is the cheapest information
    available — but among records that *have* been seen, a due `for_sale` is
    picked up before a due `live`.
    """
    weights = policy.scoring.url_death_points if policy else URL_DEATH_POINTS
    priority, priority_params = priority_case("u.state", weights)
    sql = [
        "SELECT u.url_hash, u.url_normalized, COALESCE(d.registrable_domain, '') AS dom",
        "FROM urls u LEFT JOIN domains d ON d.domain_id = u.domain_id",
    ]
    params: list = []
    where = []
    if not force:
        where.append("u.terminal = 0")
        where.append("(u.next_check_at IS NULL OR u.next_check_at <= ?)")
        params.append(utcnow())
    if where:
        sql.append("WHERE " + " AND ".join(where))
    sql.append(
        f"ORDER BY u.last_checked IS NOT NULL, {priority} DESC, "
        "u.last_checked, u.url_hash"
    )
    params.extend(priority_params)
    if limit is not None:
        sql.append("LIMIT ?")
        params.append(limit)
    rows = conn.execute("\n".join(sql), params).fetchall()
    return [_Task(r["url_hash"], r["url_normalized"], r["dom"]) for r in rows]


def _partition(tasks: list[_Task]) -> list[list[_Task]]:
    """Group by registrable domain — the unit of serialization."""
    groups: dict[str, list[_Task]] = defaultdict(list)
    for task in tasks:
        groups[task.domain or task.url].append(task)
    return list(groups.values())


def _record(
    conn: sqlite3.Connection,
    task: _Task,
    result: FetchResult | None,
    robots_decision: str,
    stats: CrawlStats,
    policy=None,
) -> None:
    """Append one `url_checks` row and advance the URL. Single-writer only."""
    now = utcnow()
    if result is None:  # blocked by robots — never fetched
        conn.execute(
            "INSERT INTO url_checks (url_hash, checked_at, robots_decision, "
            "crawler_version) VALUES (?,?,?,?)",
            (task.url_hash, now, robots_decision, __version__),
        )
        check_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "UPDATE urls SET last_checked=?, check_count=check_count+1 "
            "WHERE url_hash=?",
            (now, task.url_hash),
        )
        verdict = classify(
            Observation(url=task.url, robots_decision=robots_decision, fetched=False),
            policy,
        )
        classify_state.record(
            conn, check_id=check_id, url_hash=task.url_hash, verdict=verdict,
            policy=policy,
        )
        stats.blocked_by_robots += 1
        return

    conn.execute(
        "INSERT INTO url_checks (url_hash, checked_at, http_status, final_url, "
        " redirect_chain, redirect_count, cross_domain_redirect, content_type, "
        " content_length, page_title, body_sha256, evidence_blob, latency_ms, "
        " error_kind, error_detail, robots_decision, crawler_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            task.url_hash,
            now,
            result.http_status,
            result.final_url,
            json.dumps([h.as_dict() for h in result.redirects]) if result.redirects else None,
            result.redirect_count,
            int(_crossed_domain(task.url, result.final_url)),
            result.content_type,
            result.content_length,
            result.page_title,
            result.body_sha256,
            result.evidence_blob,
            result.latency_ms,
            result.error_kind,
            result.error_detail,
            robots_decision,
            __version__,
        ),
    )
    check_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "UPDATE urls SET last_checked=?, check_count=check_count+1 WHERE url_hash=?",
        (now, task.url_hash),
    )
    # Classified inline (stage 4 runs inside crawl), from the same evidence the
    # row just stored — so `crawl --reclassify` later reaches the same verdict.
    observation = Observation.from_result(result, task.url, robots_decision)
    observation = replace(
        observation, cross_domain_redirect=_crossed_domain(task.url, result.final_url)
    )
    verdict = classify(observation, policy)
    classify_state.record(
        conn, check_id=check_id, url_hash=task.url_hash, verdict=verdict, policy=policy
    )
    stats.verdicts[verdict.classification] += 1
    if result.ok:
        stats.fetched += 1
    else:
        stats.failed += 1


def _crossed_domain(original: str, final: str | None) -> bool:
    """Whether a redirect chain ended on a different registrable domain.

    An in-site move and a domain handover look identical at the HTTP level but
    mean very different things for expired-domain discovery.
    """
    if not final:
        return False
    from ..normalize import analyse

    a = analyse(urlsplit(original).hostname or "")
    b = analyse(urlsplit(final).hostname or "")
    return bool(a.registrable_domain and b.registrable_domain
                and a.registrable_domain != b.registrable_domain)


def _worker(
    group: list[_Task],
    user_agent: str,
    robots_store: dict,
    robots_lock: threading.Lock,
    politeness: Politeness,
    out: queue.Queue,
    stop: threading.Event,
) -> None:
    """Crawl one domain's URLs, sequentially, with pacing between them.

    **Every task emits exactly one result, including on failure.** A worker that
    dies without reporting would leave the main thread waiting forever for a
    result that is never coming — the failure mode that turns any worker bug
    into a silent hang instead of a visible error.

    This function touches no database. The robots store is an in-memory dict;
    the main thread loads and persists it.
    """
    with build_client() as client:
        for task in group:
            if stop.is_set():
                out.put((task, None, "skipped: run interrupted"))
                continue
            try:
                _crawl_one(task, client, user_agent, robots_store, robots_lock,
                           politeness, out, stop)
            except Exception as exc:  # noqa: BLE001 — must never escape unreported
                failure = FetchResult(url=task.url)
                failure.error_kind = f"worker_error:{type(exc).__name__}"
                failure.error_detail = str(exc)[:500]
                out.put((task, failure, "worker error"))


def _crawl_one(
    task: _Task,
    client,
    user_agent: str,
    robots_store: dict,
    robots_lock: threading.Lock,
    politeness: Politeness,
    out: queue.Queue,
    stop: threading.Event,
) -> None:
    parts = urlsplit(task.url)
    host = parts.hostname or ""
    origin = robots_mod.origin_of(parts.scheme, host, parts.port)

    # robots.txt is consulted before every fetch — no exceptions, no override.
    # The lock guards the shared dict, not a database.
    with robots_lock:
        cache = robots_mod.RobotsCache(
            robots_store, user_agent, _robots_body_fetch(client, user_agent)
        )
        verdict = cache.check(origin, task.url)

    state = politeness.for_host(host, verdict.crawl_delay)
    if state.tripped:
        out.put((task, None, f"skipped: {host} circuit open"))
        return
    if not verdict.allowed:
        out.put((task, None, verdict.reason))
        return

    attempt = 0
    while True:
        attempt += 1
        state.wait()
        result = fetch(client, task.url, user_agent=user_agent)
        if result.ok or not should_retry(attempt, result.transient):
            break
        delay = result.retry_after if result.retry_after is not None else backoff_delay(attempt)
        out.put(("retry", task, delay))
        if stop.wait(min(delay, 60.0)):
            break
    state.record(success=result.ok)
    out.put((task, result, verdict.reason))


def _robots_body_fetch(client, user_agent: str):
    """Return a callable that fetches a robots.txt body for RobotsCache."""

    def _fetch(robots_url: str) -> tuple[int | None, str]:
        # The full file, not the 8 KB evidence blob — a truncated robots.txt
        # silently drops its later rules.
        result = fetch(
            client,
            robots_url,
            user_agent=user_agent,
            max_body=robots_mod.MAX_ROBOTS_BYTES,
            max_evidence=robots_mod.MAX_ROBOTS_BYTES,
        )
        if result.error_kind is not None:
            return None, ""  # unreachable -> assume disallow (RFC 9309)
        return result.http_status, result.evidence_blob or ""

    return _fetch


def run(
    cfg: Config,
    log: RunLog,
    *,
    limit: int | None = None,
    concurrency: int | None = None,
    force: bool = False,
) -> CrawlStats:
    """Execute the crawl stage."""
    from concurrent.futures import ThreadPoolExecutor

    stats = CrawlStats()
    policy = load_policy(cfg.root)
    workers = max(1, concurrency or policy.crawl.concurrency)

    with open_db(cfg.db_path) as conn:
        tasks = select_due(conn, limit, force, policy)
        stats.considered = len(tasks)
        if not tasks:
            log.warn("queue", "nothing due — every URL is within its recheck window")
            return stats

        groups = _partition(tasks)
        log.ok(
            "queue",
            f"{len(tasks):,} URLs across {len(groups):,} domains "
            f"· {workers} workers · 1 request per domain at a time",
        )
        if force:
            log.warn("force", "ignoring recheck windows and terminal state")

        out: queue.Queue = queue.Queue()
        stop = threading.Event()
        robots_lock = threading.Lock()
        politeness = Politeness(default_delay=policy.crawl.delay_seconds)
        user_agent = cfg.user_agent
        # Loaded here, on the main thread, and handed to workers as a plain
        # dict. Workers must never touch SQLite (see _worker).
        robots_store = robots_mod.load_store(conn)

        pending = len(tasks)
        # Committed every CHECKPOINT_EVERY results rather than once at the end.
        # A single transaction around a long crawl would mean a crash loses the
        # whole run — and would hold a write lock for its entire duration, so a
        # concurrent `stats` or `inspect` write fails on busy_timeout. Re-crawling
        # is not cheap: it costs real requests to other people's servers.
        conn.execute("BEGIN")
        beat = Heartbeat(conn, log.run_id, "crawl", total=pending, phase="starting")
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(
                        _worker, group, user_agent, robots_store, robots_lock,
                        politeness, out, stop,
                    )
                    for group in groups
                ]
                done = 0
                while done < pending:
                    try:
                        item = out.get(timeout=5.0)
                    except queue.Empty:
                        # Nothing arrived in 5s. Say so rather than going quiet:
                        # a polite crawler waiting on a slow host and a wedged
                        # one look identical from outside unless we speak up.
                        beat.touch(phase="waiting on workers")
                        # Liveness check. If every worker has exited without
                        # producing the outstanding results, waiting longer would
                        # hang forever — surface it instead.
                        if all(f.done() for f in futures):
                            for future in futures:
                                if future.exception() is not None:
                                    raise CrawlError(
                                        "A crawl worker died: "
                                        f"{future.exception()!r}",
                                        remediation="This is a bug — please report it.",
                                    ) from future.exception()
                            log.warn(
                                "workers",
                                f"all workers exited with {pending - done} result(s) "
                                "outstanding",
                            )
                            break
                        continue
                    if item[0] == "retry":
                        _, task, delay = item
                        stats.retried += 1
                        log.progress(f"retry in {delay:.1f}s — {task.url[:58]}")
                        continue
                    task, result, reason = item
                    _record(conn, task, result, reason, stats, policy)
                    done += 1
                    beat.advance(0, phase="crawling", current_item=task.url[:120])
                    beat.done = done
                    if done % CHECKPOINT_EVERY == 0:
                        robots_mod.persist_store(conn, robots_store)
                        conn.execute("COMMIT")
                        conn.execute("BEGIN")
                    if done % 10 == 0 or done == pending:
                        log.progress(
                            f"checked {done:,}/{pending:,} "
                            f"({stats.fetched:,} ok, {stats.failed:,} failed)"
                        )
        except KeyboardInterrupt:
            stop.set()
            beat.finish("interrupted", note="Ctrl-C — checkpointed")
            robots_mod.persist_store(conn, robots_store)
            conn.execute("COMMIT")
            log.warn("interrupted", "checkpointed — re-run to resume")
            raise
        except BaseException as exc:
            # A crash must leave a row saying it crashed. A row that merely
            # stopped moving has to be diagnosed by its silence.
            beat.finish("failed", note=f"{type(exc).__name__}: {exc}")
            raise
        beat.finish("ok")
        robots_written = robots_mod.persist_store(conn, robots_store)
        conn.execute("COMMIT")

        if robots_written:
            log.ok("robots.txt", f"{robots_written} fetched and cached for 24h")
        stats.hosts_tripped = sum(1 for s in politeness.hosts.values() if s.tripped)
        conn.execute(
            "INSERT OR REPLACE INTO crawl_runs "
            "(run_id, kind, started_at, ended_at, counts, outcome) VALUES (?,?,?,?,?,?)",
            (log.run_id, RunKind.CRAWL, log.started_at, utcnow(),
             json.dumps({k: (dict(v) if isinstance(v, Counter) else v) for k, v in stats.__dict__.items()}), "failed" if log.failed else "ok"),
        )

    if stats.fetched:
        log.ok("fetched", f"{stats.fetched:,} responses recorded")
    if stats.failed:
        log.warn("failed", f"{stats.failed:,} (recorded as evidence, not discarded)")
    if stats.blocked_by_robots:
        log.warn(
            "robots", f"{stats.blocked_by_robots:,} disallowed — not fetched at all"
        )
    if stats.retried:
        log.warn("retries", f"{stats.retried:,} transient failures retried")
    if stats.hosts_tripped:
        log.warn(
            "circuit breaker",
            f"{stats.hosts_tripped} host(s) cooled after repeated failures",
        )
    if stats.verdicts:
        log.note("")
        log.note("verdicts:")
        for name, count in stats.verdicts.most_common():
            log.progress(f"{name:<26} {count:>6,}")
    return stats

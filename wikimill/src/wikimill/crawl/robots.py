"""robots.txt fetching, caching, and evaluation.

**Honoured unconditionally — there is no override flag** (prd.md §20). A
disallowed URL is classified `blocked_by_robots` and never fetched, not even
once "to check".

Status handling follows RFC 9309 §2.3.1, which is more specific than intuition:

* **2xx** — parse and apply.
* **3xx** — followed, capped (handled by the fetcher).
* **4xx** — "unavailable": the crawler MAY access any resource. A site with no
  robots.txt has not restricted anything.
* **5xx / network failure** — "unreachable": assume **complete disallow**. This
  is the counter-intuitive one, and the one that matters: a site whose server is
  struggling must not be hammered on the assumption that silence means consent.

Results are cached per origin with a TTL so a crawl of many URLs on one host
fetches robots.txt once.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.robotparser import RobotFileParser

# How long a fetched robots.txt is trusted. RFC 9309 suggests up to 24h.
CACHE_TTL = timedelta(hours=24)
# A robots.txt larger than this is truncated, per RFC 9309's 500 KiB guidance.
MAX_ROBOTS_BYTES = 512 * 1024


@dataclass(frozen=True)
class RobotsVerdict:
    allowed: bool
    crawl_delay: float | None
    reason: str
    from_cache: bool = False


def origin_of(scheme: str, host: str, port: int | None) -> str:
    return f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"


def _now() -> datetime:
    return datetime.now(UTC)


def evaluate(body: str, user_agent: str, url: str) -> RobotsVerdict:
    """Apply a robots.txt body to one URL."""
    parser = RobotFileParser()
    parser.parse(body.splitlines())
    allowed = parser.can_fetch(user_agent, url)
    try:
        delay = parser.crawl_delay(user_agent)
    except Exception:
        delay = None
    return RobotsVerdict(
        allowed=allowed,
        crawl_delay=float(delay) if delay is not None else None,
        reason="allowed by robots.txt" if allowed else "disallowed by robots.txt",
    )


def verdict_for_status(status: int | None, body: str, user_agent: str, url: str) -> RobotsVerdict:
    """Turn a robots.txt fetch outcome into a verdict, per RFC 9309 §2.3.1."""
    if status is None:
        # Unreachable — assume complete disallow. Silence is not consent.
        return RobotsVerdict(False, None, "robots.txt unreachable (assumed disallow)")
    if 200 <= status < 300:
        return evaluate(body, user_agent, url)
    if status in (429,) or status >= 500:
        return RobotsVerdict(
            False, None, f"robots.txt returned {status} (assumed disallow)"
        )
    if 400 <= status < 500:
        # Unavailable — nothing has been restricted.
        return RobotsVerdict(True, None, f"no robots.txt ({status})")
    return RobotsVerdict(True, None, f"robots.txt status {status}")


@dataclass
class RobotsEntry:
    status: int | None
    body: str
    crawl_delay: float | None
    fresh: bool = False  # newly fetched this run — the main thread must persist it


class RobotsCache:
    """Per-origin robots.txt cache over an in-memory mapping.

    Deliberately **not** backed by a database connection. This object is used
    from crawl worker threads, and a `sqlite3.Connection` is thread-bound —
    passing one in raises `ProgrammingError` in the worker, which a thread pool
    then swallows into a future, turning a clear bug into a silent hang.

    So the store is a plain dict: the main thread preloads it from
    `robots_cache`, workers read it and record newly fetched entries with
    `fresh=True`, and the main thread persists those. One writer, as designed.
    """

    def __init__(self, store: dict[str, RobotsEntry], user_agent: str, fetch) -> None:
        self.store = store
        self._user_agent = user_agent
        self._fetch = fetch  # (robots_url) -> (status | None, body)

    def check(self, origin: str, url: str) -> RobotsVerdict:
        """Decide whether `url` may be fetched, fetching robots.txt if needed."""
        entry = self.store.get(origin)
        if entry is not None:
            verdict = verdict_for_status(entry.status, entry.body, self._user_agent, url)
            return RobotsVerdict(
                verdict.allowed,
                verdict.crawl_delay if verdict.crawl_delay is not None else entry.crawl_delay,
                verdict.reason,
                from_cache=True,
            )
        status, body = self._fetch(f"{origin}/robots.txt")
        verdict = verdict_for_status(status, body, self._user_agent, url)
        self.store[origin] = RobotsEntry(
            status=status,
            body=body[:MAX_ROBOTS_BYTES],
            crawl_delay=verdict.crawl_delay,
            fresh=True,
        )
        return verdict


def load_store(conn: sqlite3.Connection) -> dict[str, RobotsEntry]:
    """Preload unexpired robots.txt entries. Main thread only."""
    store: dict[str, RobotsEntry] = {}
    for row in conn.execute(
        "SELECT origin, http_status, body, crawl_delay, expires_at FROM robots_cache"
    ):
        try:
            if datetime.fromisoformat(row["expires_at"]) <= _now():
                continue  # stale; let the worker refetch
        except (TypeError, ValueError):
            continue
        store[row["origin"]] = RobotsEntry(
            row["http_status"], row["body"] or "", row["crawl_delay"]
        )
    return store


def persist_store(conn: sqlite3.Connection, store: dict[str, RobotsEntry]) -> int:
    """Write back entries fetched during this run. Main thread only."""
    now = _now()
    written = 0
    for origin, entry in store.items():
        if not entry.fresh:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO robots_cache "
            "(origin, fetched_at, expires_at, http_status, body, crawl_delay) "
            "VALUES (?,?,?,?,?,?)",
            (
                origin,
                now.isoformat(timespec="seconds"),
                (now + CACHE_TTL).isoformat(timespec="seconds"),
                entry.status,
                entry.body,
                entry.crawl_delay,
            ),
        )
        entry.fresh = False
        written += 1
    return written

"""Heartbeat for long-running stages — is it working, or is it stuck?

A crawler that is being polite and a crawler that is wedged look identical from
outside: both produce no output for a long time. The only difference is whether
anything is still moving, and that is invisible unless the process says so.

So every long stage keeps one row saying what it is doing and when it last did
anything. Three questions become answerable **from another terminal, without
attaching a debugger and without waiting to see if output resumes**:

* *Is it alive?* — `updated_at` is moving.
* *How far along?* — `done` / `total`, with a rate and an estimate derived from
  real elapsed time rather than a guess made at the start.
* *What is it stuck on?* — `current_item` names the URL or domain being worked
  when the heartbeat stopped. That single field is the difference between "the
  crawler hung" and "the crawler is waiting on a DNS timeout for foo.example".

## It lives in its own database file, and that is the whole trick

The obvious implementation — write progress on the same connection as the work —
is broken, and subtly enough that it survived a working demo.

Long stages hold a write transaction open across a checkpoint interval (25 URLs
for the crawler) so an interrupted run resumes without re-fetching. In WAL mode
another process cannot see uncommitted rows, so progress written inside that
transaction only becomes visible **at checkpoint boundaries** — up to 41 seconds
at the measured crawl rate, and far longer when hosts time out. A healthy but
slow crawl would then cross the stall threshold and be reported as stuck. False
"it is stuck" alarms are exactly what teaches an operator to ignore the signal,
which is worse than not having it.

Nor can a second connection to the *same* database fix it: SQLite permits one
writer, so the heartbeat would block behind the work transaction it is trying to
describe, and — being best-effort — would silently write nothing at all.

So progress gets its own file, `state/progress.db`, with no lock contention with
the work database ever. Three things fall out of that, all of them wanted:

* Progress is visible **immediately**, on every write, to any process.
* Progress survives a rolled-back work transaction — correct, because it is an
  observation about the *process*, not about the data.
* For the other crawlers this is a pilot for, it drops in without touching the
  host project's schema at all.

## Other design notes

* **Upsert, not append.** This answers a question about *now*; one current row
  beats scanning ten thousand stale ones, and losing the file costs nothing the
  run logs do not already hold.
* **Throttled.** A heartbeat that writes on every item turns an I/O-bound loop
  into a database-bound one. Writes are rate-limited by wall clock, and the
  final write of a stage is forced so the last state is never lost to throttling.
* **Never fatal.** A stage must not die because its progress bookkeeping failed.
  Every write is best-effort; the crawl matters and the heartbeat does not.
* **Nothing here is wikimill-specific.** Stage names are strings, counters are
  integers, and the only dependency is a writable directory.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .logging import utcnow

PROGRESS_FILENAME = "progress.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_progress (
    run_id       TEXT    NOT NULL,
    stage        TEXT    NOT NULL,
    phase        TEXT,
    done         INTEGER NOT NULL DEFAULT 0,
    total        INTEGER,
    current_item TEXT,
    note         TEXT,
    started_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    finished_at  TEXT,
    outcome      TEXT,
    PRIMARY KEY (run_id, stage)
);
CREATE INDEX IF NOT EXISTS idx_run_progress_live
    ON run_progress(finished_at, updated_at);
"""


def open_progress_db(state_dir: Path | str) -> sqlite3.Connection:
    """Connect to the progress file, creating it if absent.

    `isolation_level=None` is the point: autocommit, so every heartbeat is
    visible to other processes the instant it is written. No migration
    framework — the file is disposable, so `CREATE TABLE IF NOT EXISTS` is the
    whole upgrade story, and a schema change is handled by deleting the file.
    """
    path = Path(state_dir) / PROGRESS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn

# How often a running stage may write. Frequent enough that a stall is obvious
# within seconds; rare enough to be free next to a network round-trip.
HEARTBEAT_SECONDS = 2.0

# No heartbeat for this long, with no `finished_at`, and the stage is not slow —
# it is stuck. Generous, because a single DNS timeout plus retries legitimately
# takes tens of seconds and crying wolf trains the operator to ignore it.
STALL_SECONDS = 90.0


@dataclass
class Heartbeat:
    """One stage's live state. Cheap to update, safe to lose."""

    conn: sqlite3.Connection
    run_id: str
    stage: str
    total: int | None = None
    done: int = 0
    phase: str | None = None
    current_item: str | None = None
    note: str | None = None
    _started: float = field(default_factory=time.monotonic)
    _last_write: float = 0.0

    def __post_init__(self) -> None:
        self._write(force=True)

    # -- the loop calls these -------------------------------------------------

    def advance(self, n: int = 1, *, current_item: str | None = None,
                phase: str | None = None, note: str | None = None) -> None:
        """Record progress. Cheap: throttled, and never raises."""
        self.done += n
        if current_item is not None:
            self.current_item = current_item
        if phase is not None:
            self.phase = phase
        if note is not None:
            self.note = note
        self._write()

    def touch(self, *, current_item: str | None = None,
              phase: str | None = None) -> None:
        """Say "still alive" without claiming progress.

        The important call, and the one people forget. A stage that only writes
        on completed work looks stalled while it waits on a slow host — which is
        exactly when the operator is staring at it wondering.
        """
        if current_item is not None:
            self.current_item = current_item
        if phase is not None:
            self.phase = phase
        self._write()

    def finish(self, outcome: str = "ok", note: str | None = None) -> None:
        """Mark the stage done, so a stale row is never mistaken for a stall."""
        if note is not None:
            self.note = note
        self.current_item = None
        self._write(force=True, finished=True, outcome=outcome)

    # -- context manager ------------------------------------------------------

    def __enter__(self) -> Heartbeat:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # A crash must leave a row saying it crashed, rather than a row that
        # simply stopped moving and has to be diagnosed by its silence.
        self.finish("failed" if exc_type else "ok",
                    note=f"{exc_type.__name__}: {exc}" if exc_type else None)
        return False

    # -- internals ------------------------------------------------------------

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    def _write(self, *, force: bool = False, finished: bool = False,
               outcome: str | None = None) -> None:
        now_mono = time.monotonic()
        if not force and (now_mono - self._last_write) < HEARTBEAT_SECONDS:
            return
        self._last_write = now_mono
        stamp = utcnow()
        try:
            self.conn.execute(
                "INSERT INTO run_progress (run_id, stage, phase, done, total, "
                " current_item, note, started_at, updated_at, finished_at, outcome) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id, stage) DO UPDATE SET "
                " phase=excluded.phase, done=excluded.done, total=excluded.total, "
                " current_item=excluded.current_item, note=excluded.note, "
                " updated_at=excluded.updated_at, "
                " finished_at=COALESCE(excluded.finished_at, run_progress.finished_at), "
                " outcome=COALESCE(excluded.outcome, run_progress.outcome)",
                (
                    self.run_id, self.stage, self.phase, self.done, self.total,
                    self.current_item, self.note, stamp, stamp,
                    stamp if finished else None, outcome,
                ),
            )
        except Exception:  # noqa: BLE001
            # Bookkeeping must never take down the work it is describing.
            pass


# --------------------------------------------------------------------------
# Reading it back — what `report` and `stats` consume
# --------------------------------------------------------------------------


@dataclass
class StageView:
    run_id: str
    stage: str
    phase: str | None
    done: int
    total: int | None
    current_item: str | None
    note: str | None
    started_at: str
    updated_at: str
    finished_at: str | None
    outcome: str | None
    age_seconds: float

    @property
    def running(self) -> bool:
        return self.finished_at is None

    @property
    def stalled(self) -> bool:
        """Alive-but-not-moving. The state this module exists to expose."""
        return self.running and self.age_seconds > STALL_SECONDS

    @property
    def percent(self) -> float | None:
        if not self.total:
            return None
        return min(100.0, 100.0 * self.done / self.total)

    @property
    def rate_per_second(self) -> float | None:
        """Throughput over the stage's own wall clock, not a configured guess."""
        span = _seconds_between(self.started_at, self.updated_at)
        if not span or self.done <= 0:
            return None
        return self.done / span

    @property
    def eta_seconds(self) -> float | None:
        rate = self.rate_per_second
        if not rate or not self.total:
            return None
        remaining = max(0, self.total - self.done)
        return remaining / rate

    @property
    def status(self) -> str:
        if self.stalled:
            return "stalled"
        if self.running:
            return "running"
        return self.outcome or "done"


def _seconds_between(start: str, end: str) -> float:
    from datetime import datetime

    try:
        return max(0.0, (datetime.fromisoformat(end)
                         - datetime.fromisoformat(start)).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def snapshot(conn: sqlite3.Connection, now: str | None = None,
             limit: int = 20) -> list[StageView]:
    """Recent stages, most recently active first."""
    stamp = now or utcnow()
    rows = conn.execute(
        "SELECT * FROM run_progress ORDER BY updated_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        StageView(
            run_id=r["run_id"], stage=r["stage"], phase=r["phase"],
            done=r["done"], total=r["total"], current_item=r["current_item"],
            note=r["note"], started_at=r["started_at"], updated_at=r["updated_at"],
            finished_at=r["finished_at"], outcome=r["outcome"],
            age_seconds=_seconds_between(r["updated_at"], stamp),
        )
        for r in rows
    ]


def live(conn: sqlite3.Connection, now: str | None = None) -> list[StageView]:
    """Stages that have not reported finishing — running or stalled."""
    return [v for v in snapshot(conn, now) if v.running]

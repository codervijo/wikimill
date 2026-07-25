"""Operator output and the structured run log.

Two surfaces, written at the same time:

1. **The terminal** — one `✓ / ↷ / ✗` line per step, printed as work happens
   (never batched into a final summary), flushed immediately so a long stage is
   never silent. Colour carries meaning: yellow ↷ = transient/skipped, retry
   helps; red ✗ = permanent, operator action needed.

2. **`state/logs/<run_id>.jsonl`** — one JSON object per event, greppable and
   surviving the terminal.

The marker set is closed on purpose. Every step ends in exactly one of the
three, including boring steps — the consistency is what lets the operator scan
a long log in seconds instead of reading it.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, TextIO

from .constants import Marker

_ANSI = {
    Marker.OK: "\033[32m",  # green
    Marker.WARN: "\033[33m",  # yellow — transient / skipped
    Marker.FAIL: "\033[31m",  # red — permanent, operator action needed
}
_DIM = "\033[2m"
_RESET = "\033[0m"


def _colour_enabled(stream: TextIO) -> bool:
    """Respect NO_COLOR and non-TTY output (pipes, CI, redirection to a file)."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return bool(getattr(stream, "isatty", lambda: False)())


def utcnow() -> str:
    """ISO-8601 UTC timestamp — the one time format used everywhere."""
    return datetime.now(UTC).isoformat(timespec="seconds")


class RunLog:
    """A single command invocation: terminal markers plus a JSONL event log.

    Used as a context manager so the log is always closed and the summary
    always printed, including on Ctrl-C:

        with RunLog(RunKind.PREFLIGHT, logs_dir) as log:
            log.ok("database", "migrated to v1")
    """

    def __init__(
        self,
        kind: str,
        logs_dir: Path | None = None,
        *,
        stream: TextIO | None = None,
        quiet: bool = False,
    ) -> None:
        self.kind = kind
        self.run_id = f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
        self.started_at = utcnow()
        self.quiet = quiet
        # Markers go to stderr so stdout stays clean for JSON and file output —
        # `wikimill export --format jsonl > file` must not capture progress noise.
        self._stream = stream if stream is not None else sys.stderr
        self._colour = _colour_enabled(self._stream)
        self.counts: dict[str, int] = {m.value: 0 for m in Marker}
        self._t0 = time.monotonic()
        self._fh: TextIO | None = None
        if logs_dir is not None:
            try:
                logs_dir.mkdir(parents=True, exist_ok=True)
                self._fh = (logs_dir / f"{self.run_id}.jsonl").open(
                    "a", encoding="utf-8"
                )
            except OSError:
                # A missing log directory must never abort a run. The terminal
                # surface is the one that matters; the file is a convenience.
                self._fh = None

    # -- internals ---------------------------------------------------------

    def _emit(self, marker: Marker, step: str, detail: str = "", **fields: Any) -> None:
        self.counts[marker.value] += 1
        if not self.quiet:
            if self._colour:
                mark = f"{_ANSI[marker]}{marker.value}{_RESET}"
                tail = f" {_DIM}{detail}{_RESET}" if detail else ""
            else:
                mark = marker.value
                tail = f" {detail}" if detail else ""
            print(f"{mark} {step}{tail}", file=self._stream, flush=True)
        self._write(
            {
                "ts": utcnow(),
                "run_id": self.run_id,
                "kind": self.kind,
                "marker": marker.value,
                "step": step,
                "detail": detail,
                **fields,
            }
        )

    def _write(self, obj: dict[str, Any]) -> None:
        if self._fh is None:
            return
        try:
            self._fh.write(json.dumps(obj, default=str) + "\n")
            self._fh.flush()
        except (OSError, ValueError):
            self._fh = None

    # -- the three markers -------------------------------------------------

    def ok(self, step: str, detail: str = "", **fields: Any) -> None:
        """✓ — succeeded, or was already in the desired state."""
        self._emit(Marker.OK, step, detail, **fields)

    def warn(self, step: str, detail: str = "", **fields: Any) -> None:
        """↷ — soft-skipped, transient, or a dry-run would-do. Retry may help."""
        self._emit(Marker.WARN, step, detail, **fields)

    def fail(self, step: str, detail: str = "", **fields: Any) -> None:
        """✗ — permanent. The operator must act before a retry can succeed."""
        self._emit(Marker.FAIL, step, detail, **fields)

    def note(self, message: str) -> None:
        """An unmarked line — headers and progress, not step outcomes."""
        if not self.quiet:
            print(message, file=self._stream, flush=True)
        self._write(
            {"ts": utcnow(), "run_id": self.run_id, "kind": self.kind, "note": message}
        )

    def progress(self, message: str) -> None:
        """A transient progress line, so long stages are never silent."""
        if not self.quiet:
            print(f"  {_DIM if self._colour else ''}{message}"
                  f"{_RESET if self._colour else ''}",
                  file=self._stream, flush=True)

    # -- lifecycle ---------------------------------------------------------

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._t0

    @property
    def failed(self) -> bool:
        return self.counts[Marker.FAIL.value] > 0

    def summary(self) -> str:
        c = self.counts
        return (
            f"{self.kind}: {c[Marker.OK.value]} ok, "
            f"{c[Marker.WARN.value]} skipped, {c[Marker.FAIL.value]} failed "
            f"in {self.elapsed:.1f}s"
        )

    def close(self) -> None:
        if not self.quiet:
            print(f"\n{self.summary()}", file=self._stream, flush=True)
        self._write(
            {
                "ts": utcnow(),
                "run_id": self.run_id,
                "kind": self.kind,
                "event": "run_end",
                "counts": self.counts,
                "elapsed_secs": round(self.elapsed, 3),
            }
        )
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> RunLog:
        self._write(
            {
                "ts": self.started_at,
                "run_id": self.run_id,
                "kind": self.kind,
                "event": "run_start",
            }
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

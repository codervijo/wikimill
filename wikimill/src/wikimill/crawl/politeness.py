"""Rate limiting, retry backoff, and the per-host circuit breaker.

Per-registrable-domain concurrency of 1 is a **guarantee to the sites we crawl,
not a tuning knob** — so it is structural here rather than configurable: the
runner partitions work by domain and gives each partition to one worker, which
makes concurrent requests to a single domain impossible by construction rather
than by a lock someone could later remove.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from ..constants import MAX_RETRIES, RETRY_BASE_SECS, RETRY_CAP_SECS

# Consecutive transient failures against one host before it is cooled for the
# rest of the run. One sick server must never stall a whole crawl.
CIRCUIT_THRESHOLD = 5


def backoff_delay(attempt: int, *, base: float = RETRY_BASE_SECS,
                  cap: float = RETRY_CAP_SECS, jitter=random.random) -> float:
    """Exponential backoff with full jitter.

    Full jitter (not just an exponential) because a synchronised retry storm is
    exactly what a struggling server does not need.
    """
    ceiling = min(cap, base * (2 ** max(0, attempt - 1)))
    return ceiling * jitter()


def should_retry(attempt: int, transient: bool, *, max_retries: int = MAX_RETRIES) -> bool:
    """Retry only transient failures, and only while attempts remain.

    A permanent failure is a *verdict*, not a fluke — retrying a 404 or a
    confirmed NXDOMAIN wastes the operator's time and the server's.
    """
    return transient and attempt < max_retries


@dataclass
class HostState:
    """Per-host pacing and health, held for the duration of one run."""

    delay: float
    last_request: float = 0.0
    consecutive_failures: int = 0
    tripped: bool = False

    def wait(self, *, sleep=time.sleep, now=time.monotonic) -> float:
        """Block until this host may be contacted again. Returns seconds waited."""
        if self.last_request == 0.0:
            self.last_request = now()
            return 0.0
        elapsed = now() - self.last_request
        remaining = self.delay - elapsed
        if remaining > 0:
            sleep(remaining)
        self.last_request = now()
        return max(0.0, remaining)

    def record(self, *, success: bool) -> None:
        if success:
            self.consecutive_failures = 0
            return
        self.consecutive_failures += 1
        if self.consecutive_failures >= CIRCUIT_THRESHOLD:
            self.tripped = True


@dataclass
class Politeness:
    """Tracks pacing across hosts for one crawl run."""

    default_delay: float
    hosts: dict[str, HostState] = field(default_factory=dict)

    def for_host(self, host: str, crawl_delay: float | None = None) -> HostState:
        state = self.hosts.get(host)
        if state is None:
            # A site's own Crawl-delay wins when it asks for more time; we never
            # use it to go faster than our own default.
            delay = max(self.default_delay, crawl_delay or 0.0)
            state = HostState(delay=delay)
            self.hosts[host] = state
        elif crawl_delay is not None and crawl_delay > state.delay:
            state.delay = crawl_delay
        return state

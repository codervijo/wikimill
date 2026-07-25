"""Stage 3 — the HTTP crawler.

Records evidence; never judges it. Classification is v1.F, and is a pure
function over the rows this stage writes.
"""

from .fetcher import FetchResult, build_client, fetch
from .guard import BlockedAddress, check_address, resolve_and_check
from .politeness import Politeness, backoff_delay, should_retry
from .robots import RobotsCache, RobotsVerdict, evaluate, verdict_for_status
from .runner import CrawlStats, run, select_due

__all__ = [
    "BlockedAddress",
    "CrawlStats",
    "FetchResult",
    "Politeness",
    "RobotsCache",
    "RobotsVerdict",
    "backoff_delay",
    "build_client",
    "check_address",
    "evaluate",
    "fetch",
    "resolve_and_check",
    "run",
    "select_due",
    "should_retry",
    "verdict_for_status",
]

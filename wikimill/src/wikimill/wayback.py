"""The Wayback Availability API — is there still a copy of this? (v4.B)

Second of the two stages that talk to somebody's servers rather than reading
their dumps, and the etiquette is the same as `wiki/usage.py`: the Internet
Archive runs this service for free, as a nonprofit, so a contact `User-Agent`,
paced serial requests, `Retry-After` obeyed, and no concurrency knob.

## The one distinction this module exists to protect

There are three answers, not two:

* **a snapshot exists** — the citation is recoverable; someone can fix it
* **no snapshot exists** — the citation is irrecoverable, and saying so is the
  whole point of the stage
* **we could not ask** — a timeout, a rate limit, a malformed response

The middle and the last look identical if you are careless, and collapsing them
is the expensive mistake here. Reporting "no copy anywhere" for a citation that
merely timed out tells the operator a source is permanently lost when it is
sitting in the archive. `UNKNOWN` is therefore a first-class result, and
`has_snapshot` is left NULL rather than 0 when we did not get an answer.

## Which moment we ask about

Not "now". The Availability API takes a `timestamp` and returns the *closest*
capture to it, so we ask for the dump run's date — the version of the page
Wikipedia was actually citing. A capture from after the site died, or after the
domain changed hands, is not the source the citation meant, and treating it as
one would quietly launder a dead reference into a live-looking link.

## A snapshot of a 404 is not a snapshot

The API reports the HTTP status the crawler saw. `available: true` with
`status: "404"` means the Internet Archive faithfully preserved a not-found
page. That is not a recovered citation, and the status is recorded so the
caller can refuse it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .errors import ConfigError

DEFAULT_ENDPOINT = "https://archive.org/wayback/available"

# HTTP statuses on the *snapshot* that mean the capture holds real content.
# A preserved 404 or 500 is a preserved absence.
USABLE_SNAPSHOT_STATUSES = frozenset({"200", "203", "226"})


@dataclass
class Availability:
    """What the archive said about one URL. `has_snapshot is None` means unknown."""

    url: str
    has_snapshot: bool | None = None
    snapshot_url: str | None = None
    snapshot_timestamp: str | None = None
    snapshot_status: str | None = None
    requested_timestamp: str | None = None
    error_kind: str | None = None
    latency_ms: int = 0
    retry_after: float = 0.0

    @property
    def answered(self) -> bool:
        return self.error_kind is None and self.has_snapshot is not None

    @property
    def recoverable(self) -> bool:
        """A usable copy exists. Deliberately stricter than `has_snapshot`:
        a faithfully-archived 404 recovers nothing."""
        return bool(
            self.has_snapshot
            and (self.snapshot_status is None
                 or self.snapshot_status in USABLE_SNAPSHOT_STATUSES)
        )


def build_params(url: str, timestamp: str | None = None) -> dict:
    params = {"url": url}
    if timestamp:
        params["timestamp"] = timestamp
    return params


def check_url(client, url: str, *, timestamp: str | None = None,
              endpoint: str = DEFAULT_ENDPOINT, now=time.monotonic) -> Availability:
    """Ask whether `url` has a capture near `timestamp`.

    `client` is injected so tests hand in a mock transport and this module never
    opens a socket in the suite.
    """
    result = Availability(url=url, requested_timestamp=timestamp)
    started = now()
    try:
        response = client.get(endpoint, params=build_params(url, timestamp))
    except Exception as exc:  # noqa: BLE001 — any transport failure is one kind
        result.error_kind = f"transport:{type(exc).__name__}"
        result.latency_ms = int((now() - started) * 1000)
        return result

    result.latency_ms = int((now() - started) * 1000)

    if response.status_code == 429 or response.status_code >= 500:
        result.retry_after = _retry_after(response)
        result.error_kind = f"http:{response.status_code}"
        return result
    if response.status_code != 200:
        result.error_kind = f"http:{response.status_code}"
        return result

    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        result.error_kind = "malformed_json"
        return result
    if not isinstance(payload, dict):
        result.error_kind = "malformed_json"
        return result

    closest = (payload.get("archived_snapshots") or {}).get("closest")
    if not closest:
        # A real, load-bearing answer: the archive has nothing. This is the
        # only place `has_snapshot=False` is set.
        result.has_snapshot = False
        return result

    if not closest.get("available", False):
        result.has_snapshot = False
        return result

    result.has_snapshot = True
    result.snapshot_url = closest.get("url")
    result.snapshot_timestamp = closest.get("timestamp")
    status = closest.get("status")
    result.snapshot_status = str(status) if status is not None else None
    return result


def _retry_after(response) -> float:
    raw = response.headers.get("retry-after") if hasattr(response, "headers") else None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def require_identity(user_agent: str | None) -> str:
    """Refuse to query the Internet Archive anonymously.

    They run this for free. A tool that will not say who it is, to a nonprofit
    absorbing the cost, does not get to make the request.
    """
    if not user_agent or "wikimill" not in user_agent:
        raise ConfigError(
            "Cannot query the Wayback Machine without a contact User-Agent.",
            remediation=(
                "Set WIKIMILL_CONTACT to an email address or URL in wikimill.env. "
                "The Internet Archive runs this API for free; identify yourself."
            ),
        )
    return user_agent

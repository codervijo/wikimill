"""`exturlusage` — asking the live wiki what it still links to (v2.F).

Every other stage in this tool reads dumps. This one talks to Wikimedia's
servers, and that difference sets all the rules below.

## Why ask at all

Our citation evidence is a snapshot. The export says "cited by 8 distinct
Wikipedia pages" on the authority of a dump that may be weeks old, and that
number is the single strongest claim in the file — it is why a candidate is
worth anything. If editors have since removed those citations, the export is
confidently wrong in the direction the operator acts on.

It also yields the v2.G removal signal *without waiting for a second dump*: the
gap between what the dump claimed and what the wiki reports now is exactly the
count of citations editors have dropped since.

## Etiquette, which is not optional here

Wikimedia runs this API for free and asks specific things of clients. All of it
is structural rather than configurable, for the same reason per-domain crawl
concurrency is:

* **A real User-Agent with contact information.** Already built — `cfg.user_agent`
  exists because of this policy (config.py), and this module refuses to send a
  request without one rather than falling back to a generic string.
* **`maxlag`.** The canonical good-citizen parameter: if replication lag exceeds
  it the API returns an error instead of adding load to a struggling cluster,
  and the client is expected to wait. Sent on every request, and the error is
  honoured rather than retried through.
* **`Retry-After` is obeyed** when the API sends it.
* **Requests are serial.** v2.I parallelised domain checks against registries
  because those are many independent operators with their own capacity. This is
  one operator's shared cluster, which asks bots to run series of requests
  sequentially — so there is deliberately no concurrency knob here, and the
  contrast is the point rather than an oversight.

## What it does not do

It never sets a domain state and never removes a candidate from the export. A
live count is one more piece of evidence, recorded with the dump count beside
it; the operator sees both numbers and decides.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..errors import ConfigError

DEFAULT_ENDPOINT = "https://en.wikipedia.org/w/api.php"

# Replication lag, in seconds, past which Wikimedia should refuse us rather than
# add load. 5 is the value their API etiquette documentation names for bots.
DEFAULT_MAXLAG = 5

# Articles only (namespace 0) — the same slice ingest keeps, so the live number
# is comparable with the dump number rather than counting talk pages too.
ARTICLE_NAMESPACE = 0

# `eulimit` for anonymous clients. 500 is the API's own ceiling.
PAGE_SIZE = 500

# Stop paginating after this many requests for one domain. A domain cited by
# tens of thousands of pages is not a candidate for acquisition, so paying for
# an exact count buys nothing — the result is marked `truncated` and treated as
# a floor everywhere downstream.
MAX_PAGES_PER_DOMAIN = 4


@dataclass
class UsageResult:
    """What the live wiki said about one domain."""

    domain: str
    live_page_count: int | None = None
    truncated: bool = False
    error_kind: str | None = None
    latency_ms: int = 0
    retry_after: float = 0.0
    titles: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error_kind is None and self.live_page_count is not None


def build_params(domain: str, continue_token: dict | None = None,
                 *, maxlag: int = DEFAULT_MAXLAG) -> dict:
    """Query parameters for one `exturlusage` page.

    `euquery` is matched against the wiki's own externallinks index, and a bare
    registrable domain matches that domain and its subdomains — which is the
    granularity a candidate is judged at.
    """
    params = {
        "action": "query",
        "list": "exturlusage",
        "euquery": domain,
        "eunamespace": str(ARTICLE_NAMESPACE),
        "eulimit": str(PAGE_SIZE),
        "euprop": "title",
        "format": "json",
        "formatversion": "2",
        "maxlag": str(maxlag),
    }
    if continue_token:
        params.update({k: str(v) for k, v in continue_token.items()})
    return params


def _error_kind(payload: dict) -> str | None:
    """Map an API-level error onto a recorded kind.

    The Action API answers with HTTP 200 and an `error` object, so a client that
    only checks status codes records "0 pages link here" for what was actually
    "we were rate-limited" — a false, high-confidence signal.
    """
    error = payload.get("error")
    if not error:
        return None
    code = str(error.get("code") or "unknown")
    return f"api:{code}"


def check_domain(client, domain: str, *, endpoint: str = DEFAULT_ENDPOINT,
                 maxlag: int = DEFAULT_MAXLAG, sleep=time.sleep,
                 now=time.monotonic) -> UsageResult:
    """How many live articles still link to `domain`.

    One domain, one serial pagination loop. `client` is injected so tests can
    hand in a mock transport and this module never opens a socket in the suite.
    """
    result = UsageResult(domain=domain)
    started = now()
    seen: set[str] = set()
    continue_token: dict | None = None

    for _ in range(MAX_PAGES_PER_DOMAIN):
        try:
            response = client.get(endpoint, params=build_params(
                domain, continue_token, maxlag=maxlag
            ))
        except Exception as exc:  # noqa: BLE001 — any transport failure is one kind
            result.error_kind = f"transport:{type(exc).__name__}"
            break

        if response.status_code == 429 or response.status_code >= 500:
            # Honour their backpressure rather than deciding our own.
            result.retry_after = _retry_after(response)
            result.error_kind = f"http:{response.status_code}"
            break
        if response.status_code != 200:
            result.error_kind = f"http:{response.status_code}"
            break

        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            result.error_kind = "malformed_json"
            break

        kind = _error_kind(payload)
        if kind:
            # `maxlag` is the expected one: the cluster is behind and has asked
            # us to come back later. It is not a failure of the query.
            result.retry_after = _retry_after(response)
            result.error_kind = kind
            break

        for entry in payload.get("query", {}).get("exturlusage", []) or []:
            title = entry.get("title")
            if title:
                seen.add(title)

        continue_token = payload.get("continue")
        if not continue_token:
            break
    else:
        # Ran out of allowed requests with a continuation still pending.
        result.truncated = bool(continue_token)

    result.latency_ms = int((now() - started) * 1000)
    if result.error_kind is None:
        result.live_page_count = len(seen)
        result.titles = sorted(seen)[:5]
    return result


def _retry_after(response) -> float:
    raw = response.headers.get("retry-after") if hasattr(response, "headers") else None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def require_identity(user_agent: str | None) -> str:
    """Refuse to contact Wikimedia anonymously.

    Their User-Agent policy asks for real contact information, and a tool that
    quietly degrades to a generic string when the operator has not configured
    one is exactly the client that policy exists to keep out.
    """
    if not user_agent or "wikimill" not in user_agent:
        raise ConfigError(
            "Cannot query the Wikimedia API without a contact User-Agent.",
            remediation=(
                "Set WIKIMILL_CONTACT to an email address or URL in wikimill.env. "
                "Wikimedia's User-Agent policy requires real contact information."
            ),
        )
    return user_agent

"""The HTTP fetcher: one URL in, one `url_checks` row's worth of evidence out.

Deliberately **not** a classifier. This module records what happened —
status, redirect chain, timing, a bounded evidence blob — and v1.F decides what
it means. Keeping the two apart is what lets an improved classifier re-judge
history offline, with no refetching and no extra load on anyone's server
(architecture.md §2).

Safety properties, all from prd.md §18 and enforced here rather than assumed:

* every redirect hop is SSRF-checked, not just the first;
* redirects are capped and loop-detected;
* the body is size-capped **while streaming**, so a hostile server cannot
  exhaust memory with an endless response;
* only `<title>` is extracted, by regex — no HTML parser, no JS, no execution.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..constants import (
    EVIDENCE_BLOB_BYTES,
    MAX_BODY_BYTES,
    MAX_REDIRECTS,
)
from .guard import BlockedAddress, resolve_and_check

_TITLE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_WS = re.compile(r"\s+")

CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 20.0
TOTAL_TIMEOUT = 45.0


@dataclass
class Hop:
    url: str
    status: int
    location: str | None
    addresses: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "url": self.url,
            "status": self.status,
            "location": self.location,
            "addresses": list(self.addresses),
        }


@dataclass
class FetchResult:
    """Everything one fetch attempt observed. No verdict — that is v1.F."""

    url: str
    final_url: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    content_length: int | None = None
    page_title: str | None = None
    body_sha256: str | None = None
    evidence_blob: str | None = None
    latency_ms: int | None = None
    redirects: list[Hop] = field(default_factory=list)
    error_kind: str | None = None
    error_detail: str | None = None
    transient: bool = False
    retry_after: float | None = None

    @property
    def redirect_count(self) -> int:
        return len(self.redirects)

    @property
    def ok(self) -> bool:
        return self.error_kind is None


def extract_title(body: bytes) -> str | None:
    """Pull `<title>` out with a regex.

    Deliberately not an HTML parser: this content is attacker-controlled, and
    parsing it properly would mean handing hostile markup to a much larger
    attack surface for a field we only use as a classification hint.
    """
    match = _TITLE.search(body)
    if not match:
        return None
    text = match.group(1).decode("utf-8", errors="replace")
    text = re.sub(r"<[^>]*>", "", text)
    return _WS.sub(" ", text).strip()[:512] or None


def _classify_transport_error(exc: Exception) -> tuple[str, bool]:
    """Map an exception to (error_kind, transient).

    Sub-kinds are preserved rather than lumped (prd.md §11): "the TLS cert
    expired" and "the host does not exist" lead to completely different
    conclusions about a domain, and collapsing them into "failed" throws away
    the signal this whole tool exists to find.
    """
    import httpx

    name = type(exc).__name__
    text = str(exc).lower()

    if isinstance(exc, BlockedAddress):
        return "blocked_address", False
    if isinstance(exc, LookupError):
        return "dns_nxdomain" if "not known" in text or "failure" in text else "dns_error", False

    if isinstance(exc, httpx.ConnectTimeout):
        return "connect_timeout", True
    if isinstance(exc, httpx.ReadTimeout):
        return "read_timeout", True
    if isinstance(exc, httpx.PoolTimeout):
        return "pool_timeout", True
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", True
    if isinstance(exc, httpx.TooManyRedirects):
        return "too_many_redirects", False
    if isinstance(exc, httpx.ConnectError):
        # TLS problems surface as ConnectError; the distinction is the point.
        if "certificate has expired" in text or "certificate_expired" in text:
            return "tls_cert_expired", False
        if "hostname mismatch" in text or "subject name" in text:
            return "tls_hostname_mismatch", False
        if "self signed" in text or "unable to get local issuer" in text:
            return "tls_chain_untrusted", False
        if "certificate" in text or "ssl" in text or "tls" in text:
            return "tls_error", False
        if "name or service not known" in text or "nodename nor servname" in text:
            return "dns_nxdomain", False
        if "connection refused" in text:
            return "connection_refused", True
        return "connect_error", True
    if isinstance(exc, httpx.ReadError):
        return "read_error", True
    if isinstance(exc, httpx.RemoteProtocolError):
        return "protocol_error", True
    if isinstance(exc, httpx.UnsupportedProtocol):
        return "unsupported_protocol", False
    if isinstance(exc, httpx.HTTPError):
        return f"http_error:{name}", True
    return f"unexpected:{name}", False


def _parse_retry_after(value: str | None) -> float | None:
    """`Retry-After` is honoured exactly when present (prd.md §13)."""
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        from datetime import UTC, datetime

        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - datetime.now(UTC)).total_seconds())
    except Exception:
        return None


def fetch(
    client,
    url: str,
    *,
    user_agent: str,
    resolver=None,
    max_redirects: int = MAX_REDIRECTS,
    max_body: int = MAX_BODY_BYTES,
    max_evidence: int = EVIDENCE_BLOB_BYTES,
) -> FetchResult:
    """Fetch one URL, following redirects manually so every hop is checked."""
    import httpx

    result = FetchResult(url=url)
    started = time.monotonic()
    current = url
    seen: set[str] = set()
    headers = {
        "User-Agent": user_agent,
        # Honest identification. We never spoof a browser (prd.md §17).
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }

    try:
        for _ in range(max_redirects + 1):
            if current in seen:
                result.error_kind = "redirect_loop"
                result.error_detail = f"loop back to {current}"
                break
            seen.add(current)

            parts = urlsplit(current)
            host = parts.hostname or ""
            if not host:
                result.error_kind = "no_host"
                break
            # Re-checked at EVERY hop: validating only the first is how
            # redirect-to-internal-address slips through.
            resolution = resolve_and_check(host, **({"resolver": resolver} if resolver else {}))

            with client.stream("GET", current, headers=headers) as response:
                status = response.status_code
                location = response.headers.get("location")

                if 300 <= status < 400 and location:
                    result.redirects.append(
                        Hop(current, status, location, resolution.addresses)
                    )
                    current = str(httpx.URL(current).join(location))
                    response.close()
                    continue

                result.http_status = status
                result.final_url = current
                result.content_type = response.headers.get("content-type")
                result.retry_after = _parse_retry_after(
                    response.headers.get("retry-after")
                )

                body = bytearray()
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) >= max_body:
                        # Capped while streaming: a hostless server cannot make
                        # us buffer an endless body.
                        break
                payload = bytes(body[:max_body])
                result.body_sha256 = hashlib.sha256(payload).hexdigest()
                result.page_title = extract_title(payload)
                # `max_evidence` is separate from `max_body` because robots.txt
                # needs the whole file: truncating it at the 8 KB evidence cap
                # would drop later rules and could let us fetch something the
                # site disallowed.
                result.evidence_blob = payload[:max_evidence].decode(
                    "utf-8", errors="replace"
                )
                # The bytes we actually observed, never the declared
                # Content-Length. A server's claim can be wrong or a lie, and
                # the classifier uses this to judge thin bodies — it has to
                # match what was hashed and stored.
                result.content_length = len(payload)
                break
        else:
            result.error_kind = "too_many_redirects"
            result.error_detail = f"exceeded {max_redirects} hops"
    except Exception as exc:  # noqa: BLE001 — every failure becomes evidence
        kind, transient = _classify_transport_error(exc)
        result.error_kind = kind
        result.error_detail = str(exc)[:500]
        result.transient = transient

    if result.error_kind in {"redirect_loop", "too_many_redirects", "no_host"}:
        result.transient = False
    result.latency_ms = int((time.monotonic() - started) * 1000)
    return result


def build_client(**kwargs):
    """A client with our timeouts and no automatic redirect following."""
    import httpx

    kwargs.setdefault(
        "timeout",
        httpx.Timeout(TOTAL_TIMEOUT, connect=CONNECT_TIMEOUT, read=READ_TIMEOUT),
    )
    kwargs.setdefault("follow_redirects", False)  # we follow manually, per hop
    kwargs.setdefault("max_redirects", 0)
    return httpx.Client(**kwargs)

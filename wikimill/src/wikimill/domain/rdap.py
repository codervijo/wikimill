"""RDAP lookups via the IANA bootstrap registry (RFC 9224).

RDAP is the standardized successor to port-43 WHOIS: documented, JSON, and with
terms a crawler can honour. wikimill never scrapes a registrar's web page and
never speaks WHOIS (prd.md §2, §17).

**The most important rule in this module is what it refuses to conclude.** A
rate-limited, erroring, or unreachable RDAP server means *we do not know* — it
is recorded as `unavailable`, never as `not_found`. Only an explicit 404 from
the authoritative registry counts as "no such domain", and even that is not
sufficient for `unregistered` on its own (see rules.py).

Coverage is genuinely incomplete, and that is not a bug to work around but a
fact to report. Measured against the live bootstrap (2026-07-25): 1,200 TLDs
across 590 service groups — but `.de`, `.es`, `.io` and `.ru` have **no RDAP
entry at all**. Domains under those TLDs can never be confirmed unregistered,
and are honestly recorded as `no_rdap_for_tld`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
BOOTSTRAP_FILENAME = "rdap-bootstrap.json"
BOOTSTRAP_TTL_SECS = 7 * 86_400
MAX_RDAP_BYTES = 512 * 1024

# EPP status codes that mean the registration is winding down. Their presence is
# the earliest reliable warning that a domain may become available.
EXPIRING_STATUSES = frozenset(
    {
        "pending delete",
        "pendingdelete",
        "redemption period",
        "redemptionperiod",
        "client hold",
        "clienthold",
        "server hold",
        "serverhold",
        "pending restore",
        "pendingrestore",
        "auto renew period",
    }
)


class RdapStatus(StrEnum):
    REGISTERED = "registered"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"          # we do not know — never "not found"
    NO_RDAP_FOR_TLD = "no_rdap_for_tld"  # honest coverage gap


@dataclass
class RdapResult:
    status: RdapStatus
    server: str | None = None
    registrar: str | None = None
    expiry: str | None = None
    statuses: list[str] = field(default_factory=list)
    raw: str | None = None
    error: str | None = None
    latency_ms: int | None = None

    @property
    def expiring(self) -> bool:
        """Whether an EPP status marks this registration as winding down."""
        return any(s.strip().lower() in EXPIRING_STATUSES for s in self.statuses)


class Bootstrap:
    """The IANA TLD → RDAP base-URL map, cached on disk.

    Cached because it changes rarely (published every few days) and refetching
    71 KB before every domain check would be rude for no benefit.
    """

    def __init__(self, services: list) -> None:
        self._by_tld: dict[str, str] = {}
        for entry in services:
            if len(entry) < 2 or not entry[1]:
                continue
            base = entry[1][0]
            for tld in entry[0]:
                self._by_tld[tld.lower()] = base

    def __len__(self) -> int:
        return len(self._by_tld)

    def base_url(self, domain: str) -> str | None:
        """Longest label-wise suffix match, per RFC 9224 — so `co.uk` wins over
        `uk` when the registry publishes both."""
        labels = domain.lower().strip(".").split(".")
        # Ascending i yields the LONGEST suffix first ("bbc.co.uk", "co.uk",
        # "uk"). Descending would match "uk" first and route every .co.uk
        # domain to the wrong registry — silently, with plausible answers.
        for i in range(len(labels)):
            candidate = ".".join(labels[i:])
            if candidate in self._by_tld:
                return self._by_tld[candidate]
        return None

    @classmethod
    def from_json(cls, payload: str) -> Bootstrap:
        return cls(json.loads(payload).get("services", []))


def load_bootstrap(state_dir: Path, fetch) -> Bootstrap | None:
    """Load the cached bootstrap, refetching only when stale or missing."""
    path = state_dir / BOOTSTRAP_FILENAME
    if path.is_file():
        age = time.time() - path.stat().st_mtime
        if age < BOOTSTRAP_TTL_SECS:
            try:
                return Bootstrap.from_json(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, KeyError):
                pass  # corrupt cache — refetch below

    status, body = fetch(BOOTSTRAP_URL)
    if status != 200 or not body:
        # Fall back to a stale cache rather than failing: an out-of-date TLD map
        # is far better than no domain checks at all.
        if path.is_file():
            try:
                return Bootstrap.from_json(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, KeyError):
                return None
        return None
    try:
        bootstrap = Bootstrap.from_json(body)
    except (json.JSONDecodeError, KeyError):
        return None
    try:
        path.write_text(body, encoding="utf-8")
    except OSError:
        pass  # a cache we cannot persist costs speed, never correctness
    return bootstrap


def _extract(payload: dict) -> tuple[str | None, str | None, list[str]]:
    """Pull registrar, expiry, and EPP statuses out of an RDAP domain object."""
    statuses = [str(s) for s in payload.get("status", []) if s]

    expiry = None
    for event in payload.get("events", []) or []:
        if event.get("eventAction") == "expiration":
            expiry = event.get("eventDate")
            break

    registrar = None
    for entity in payload.get("entities", []) or []:
        roles = entity.get("roles") or []
        if "registrar" not in roles:
            continue
        vcard = entity.get("vcardArray")
        if isinstance(vcard, list) and len(vcard) > 1:
            for item in vcard[1]:
                if isinstance(item, list) and len(item) >= 4 and item[0] == "fn":
                    registrar = str(item[3])
                    break
        if registrar is None and entity.get("handle"):
            registrar = str(entity["handle"])
        break

    return registrar, expiry, statuses


def query(domain: str, bootstrap: Bootstrap | None, fetch) -> RdapResult:
    """Look one domain up. Never raises; every failure becomes a status."""
    if bootstrap is None:
        return RdapResult(RdapStatus.UNAVAILABLE, error="no bootstrap registry")

    base = bootstrap.base_url(domain)
    if base is None:
        # Not a failure — a real, measurable coverage gap (.de, .es, .io, .ru …).
        return RdapResult(RdapStatus.NO_RDAP_FOR_TLD)

    url = f"{base.rstrip('/')}/domain/{domain}"
    started = time.monotonic()
    status, body = fetch(url)
    latency = int((time.monotonic() - started) * 1000)

    if status == 404:
        return RdapResult(
            RdapStatus.NOT_FOUND, server=base, latency_ms=latency, raw=None
        )
    if status is None or status != 200:
        # Rate-limited, erroring, or unreachable. We do not know — and guessing
        # here would fabricate an available domain.
        return RdapResult(
            RdapStatus.UNAVAILABLE,
            server=base,
            error=f"HTTP {status}" if status else "unreachable",
            latency_ms=latency,
        )

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return RdapResult(
            RdapStatus.UNAVAILABLE, server=base, error="malformed JSON",
            latency_ms=latency,
        )

    registrar, expiry, statuses = _extract(payload)
    return RdapResult(
        RdapStatus.REGISTERED,
        server=base,
        registrar=registrar,
        expiry=expiry,
        statuses=statuses,
        raw=body[:MAX_RDAP_BYTES],
        latency_ms=latency,
    )


def expires_within(expiry: str | None, days: int, *, now: datetime | None = None) -> bool:
    """Whether an RDAP expiry timestamp falls inside the warning window."""
    if not expiry:
        return False
    try:
        when = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    return 0 <= (when - reference).total_seconds() <= days * 86_400

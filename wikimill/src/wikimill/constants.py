"""Canonical enums, identifiers, and defaults.

These are contracts, not conveniences: values here are persisted to SQLite and
matched against, so renaming one is a data migration rather than a refactor.
Anything that appears in more than one module belongs here — three spellings of
the same concept across modules is a real bug-fix cost.

See docs/prd.md §11 (state vocabulary) and §9 (data model).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

# --------------------------------------------------------------------------
# Versions stamped onto persisted rows. Bump when the producing logic changes
# in a way that would alter its output — that is what makes offline
# re-classification and re-normalization detectable (prd.md §8).
# --------------------------------------------------------------------------

SCHEMA_VERSION: Final = 1
NORMALIZER_VERSION: Final = 1
CLASSIFIER_VERSION: Final = 1


class UrlState(StrEnum):
    """URL lifecycle (prd.md §11). `pending` → `in_progress` → a classification."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    # The eleven-state classification vocabulary.
    LIVE = "live"
    REDIRECT = "redirect"
    SOFT_404 = "soft_404"
    HARD_404 = "hard_404"
    DNS_FAILURE = "dns_failure"
    TLS_FAILURE = "tls_failure"
    PARKED = "parked"
    FOR_SALE = "for_sale"
    UNREGISTERED = "unregistered"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    UNCLASSIFIED = "unclassified"
    # Terminal, non-classification outcomes.
    BLOCKED_BY_ROBOTS = "blocked_by_robots"
    SKIPPED = "skipped"


class DomainState(StrEnum):
    """Domain lifecycle (prd.md §11). Only domain checks may set these."""

    UNKNOWN = "unknown"
    ACTIVE = "active"
    PARKED = "parked"
    FOR_SALE = "for_sale"
    EXPIRING = "expiring"
    UNREGISTERED = "unregistered"
    NO_RDAP_FOR_TLD = "no_rdap_for_tld"
    OUT_OF_SCOPE = "out_of_scope"


class EnrichStatus(StrEnum):
    """Per-link enrichment outcome (prd.md §11).

    `URL_NOT_FOUND_IN_WIKITEXT` is an expected, honest result — not an error.
    It means the link came from template expansion and has no literal wikitext
    occurrence, which is information worth recording rather than papering over.
    """

    PENDING = "pending"
    DONE = "done"
    PAGE_MISSING = "page_missing"
    URL_NOT_FOUND_IN_WIKITEXT = "url_not_found_in_wikitext"


class RunKind(StrEnum):
    """`crawl_runs.kind` — one per pipeline stage that has a command."""

    PREFLIGHT = "preflight"
    INGEST = "ingest"
    CRAWL = "crawl"
    CHECK = "check"
    ENRICH = "enrich"
    EXPORT = "export"


class Marker(StrEnum):
    """Operator-facing step markers. See docs/architecture.md § Output contract.

    OK   ✓ success, or skip-because-already-correct
    WARN ↷ soft-skip, transient, dry-run-would-do, warn-skipped  (retry helps)
    FAIL ✗ permanent — operator action needed                    (retry will not)
    """

    OK = "✓"
    WARN = "↷"
    FAIL = "✗"


# --------------------------------------------------------------------------
# Recheck cadences, in seconds (prd.md §12). Policy defaults chosen to reflect
# volatility and value — NOT measured figures. Expected to be tuned once v1.J
# produces real data.
# --------------------------------------------------------------------------

_DAY: Final = 86_400

RECHECK_INTERVALS: Final[dict[str, int]] = {
    UrlState.UNREGISTERED: 3 * _DAY,
    UrlState.FOR_SALE: 7 * _DAY,
    UrlState.PARKED: 7 * _DAY,
    UrlState.DNS_FAILURE: 7 * _DAY,
    UrlState.TLS_FAILURE: 14 * _DAY,
    UrlState.SOFT_404: 30 * _DAY,
    UrlState.HARD_404: 30 * _DAY,
    UrlState.LIVE: 90 * _DAY,
    UrlState.REDIRECT: 90 * _DAY,
    UrlState.TEMPORARILY_UNAVAILABLE: 3_600,
    UrlState.BLOCKED_BY_ROBOTS: 180 * _DAY,
}

# Domain states that make a link worth enriching regardless of its URL state.
#
# Domain-level discovery can outpace URL-level classification: a domain is
# confirmed `unregistered` by DNS + RDAP even when its URLs were never crawled
# (they stay `pending`). Triggering on URL state alone meant the strongest finds
# in the corpus exported with no Wikipedia context at all — the one thing that
# makes them actionable. Found by running the full pipeline, 2026-07-25.
DOMAIN_ENRICH_TRIGGER_STATES: Final[frozenset[str]] = frozenset(
    {"unregistered", "expiring", "for_sale", "parked"}
)

# Classifications that make a link worth spending enrichment on (prd.md §11).
# Deliberately excludes LIVE — that exclusion is the whole cheapest-first design.
ENRICH_TRIGGER_STATES: Final[frozenset[str]] = frozenset(
    {
        UrlState.UNREGISTERED,
        UrlState.FOR_SALE,
        UrlState.PARKED,
        UrlState.DNS_FAILURE,
        UrlState.TLS_FAILURE,
        UrlState.SOFT_404,
        UrlState.HARD_404,
    }
)

# --------------------------------------------------------------------------
# Environment variables. Split by the layer that reads them: LAUNCHER vars are
# read by bin/wikimill on the host (they decide how the container is built and
# what is mounted, so they cannot be read from inside it); APP vars are read
# here, in the container.
# --------------------------------------------------------------------------

ENV_FILE_NAME: Final = "wikimill.env"
ENV_EXAMPLE_NAME: Final = "wikimill.env.example"

LAUNCHER_ENV_VARS: Final[tuple[str, ...]] = (
    "DOCKER_CMD",
    "WIKIMILL_IMAGE",
    "WIKIMILL_REBUILD",
    "WIKIMILL_DRY_RUN",
    "WIKIMILL_DUMPS_DIR",
)

APP_ENV_VARS: Final[tuple[str, ...]] = (
    "WIKIMILL_CONTACT",
    "WIKIMILL_USER_AGENT",
    "WIKIMILL_DNS_RESOLVERS",
    "WIKIMILL_CONCURRENCY",
    "WIKIMILL_CRAWL_DELAY",
)

# Any variable whose name matches one of these is redacted everywhere wikimill
# prints configuration — preflight, --json, logs, and crawl_runs.args.
SECRET_NAME_PATTERNS: Final[tuple[str, ...]] = (
    "_KEY",
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
)
REDACTED: Final = "<redacted>"

# --------------------------------------------------------------------------
# Paths, relative to the repo root. state/ and outputs/ are host-mounted.
# --------------------------------------------------------------------------

DB_FILENAME: Final = "wikimill.db"
STATE_DIRNAME: Final = "state"
DUMPS_DIRNAME: Final = "dumps"
LOGS_DIRNAME: Final = "logs"
OUTPUTS_DIRNAME: Final = "outputs"
CHECKSUM_CACHE_FILENAME: Final = "dump-checksums.json"

# --------------------------------------------------------------------------
# Crawl politeness. PER_DOMAIN_CONCURRENCY is deliberately not configurable:
# it is a guarantee we make to the sites we crawl, not a tuning knob.
# --------------------------------------------------------------------------

PER_DOMAIN_CONCURRENCY: Final = 1
DEFAULT_CONCURRENCY: Final = 8
DEFAULT_CRAWL_DELAY_SECS: Final = 1.0
MAX_REDIRECTS: Final = 5
MAX_BODY_BYTES: Final = 2 * 1024 * 1024
EVIDENCE_BLOB_BYTES: Final = 8 * 1024
MAX_RETRIES: Final = 3
RETRY_BASE_SECS: Final = 2.0
RETRY_CAP_SECS: Final = 60.0

# Exit codes (prd.md §13).
EXIT_OK: Final = 0
EXIT_ERROR: Final = 1
EXIT_PREFLIGHT: Final = 2
EXIT_INTERRUPTED: Final = 130

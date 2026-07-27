"""Operator-tunable policy, loaded from `wikimill.toml`.

**The line between policy and code is the load-bearing decision here** (v2.A).

Tunable — *what the tool looks for*:
  scoring weights, candidate states, citation floors, enrichment triggers,
  recheck cadences, marker vocabularies, concurrency and pacing defaults.
  Every one of these is a judgement call made without evidence in v1, and the
  soak exists to challenge them. Until they are config, "tune it and re-run" is
  not something the operator can actually do.

**Not** tunable — *what protects other people, and what makes results auditable*:
  * `PER_DOMAIN_CONCURRENCY = 1` — a guarantee to every site we crawl, not a
    knob. Configurable politeness is politeness you will eventually turn off.
  * redirect, body-size and evidence caps — SSRF and resource-exhaustion
    defences (prd.md §18).
  * the two-resolver rule for `unregistered` — a false "available domain" is
    the most expensive error this tool can make; no config may weaken it.
  * robots.txt obedience — there is no override flag and there will not be one.
  * version stamps, exit codes, and the export licence header — provenance and
    attribution obligations, not preferences.

Precedence, highest first: **CLI flag > environment > `wikimill.toml` > built-in
default.** The env layer stays credentials-and-environment (`wikimill.env`);
policy lives in the toml. A secret does not belong in a checked-in config file.

Changing a value that affects classification changes `effective_classifier_version`
automatically (see `fingerprint`), so stored verdicts remain attributable to the
rules that produced them without anyone having to remember to bump a number.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from . import constants, markers, score
from .errors import ConfigError

POLICY_FILENAME = "wikimill.toml"
POLICY_EXAMPLE = "wikimill.toml.example"

# Sections whose values change how a URL or domain is judged. A change to any of
# these shifts `effective_classifier_version`, so verdicts stay auditable.
CLASSIFYING_SECTIONS = ("scoring", "classify", "markers", "enrich", "check")


@dataclass
class Scoring:
    """Weights for ranking candidates. Policy defaults, never measured."""

    state_points: dict[str, int] = field(
        default_factory=lambda: {str(k): v for k, v in score.STATE_POINTS.items()}
    )
    url_death_points: dict[str, int] = field(
        default_factory=lambda: {str(k): v for k, v in score.URL_DEATH_POINTS.items()}
    )
    kind_points: dict[str, int] = field(
        default_factory=lambda: dict(score.KIND_POINTS)
    )
    citation_points_per_page: int = score.CITATION_POINTS_PER_PAGE
    citation_points_cap: int = score.CITATION_POINTS_CAP
    dead_link_tagged_points: int = score.DEAD_LINK_TAGGED_POINTS
    private_suffix_penalty: int = score.PRIVATE_SUFFIX_PENALTY


@dataclass
class Export:
    candidate_states: list[str] = field(
        default_factory=lambda: [
            constants.DomainState.UNREGISTERED,
            constants.DomainState.EXPIRING,
            constants.DomainState.FOR_SALE,
            constants.DomainState.PARKED,
            constants.DomainState.NO_RDAP_FOR_TLD,
        ]
    )
    min_pages: int = 1


@dataclass
class Enrich:
    url_trigger_states: list[str] = field(
        default_factory=lambda: sorted(str(s) for s in constants.ENRICH_TRIGGER_STATES)
    )
    domain_trigger_states: list[str] = field(
        default_factory=lambda: sorted(constants.DOMAIN_ENRICH_TRIGGER_STATES)
    )


@dataclass
class Check:
    interesting_url_states: list[str] = field(
        default_factory=lambda: [str(s) for s in constants.INTERESTING_URL_STATES]
    )
    recheck_days: dict[str, int] = field(
        default_factory=lambda: {str(k): v for k, v in constants.DOMAIN_RECHECK_DAYS.items()}
    )
    expiry_watch_days: int = constants.EXPIRY_WATCH_DAYS
    rdap_concurrency_per_registry: int = constants.RDAP_CONCURRENCY_PER_REGISTRY


@dataclass
class Classify:
    recheck_seconds: dict[str, int] = field(
        default_factory=lambda: {str(k): v for k, v in constants.RECHECK_INTERVALS.items()}
    )
    hard_404_confirmations: int = constants.HARD_404_CONFIRMATIONS
    default_recheck_seconds: int = constants.DEFAULT_RECHECK_SECS
    thin_body_bytes: int = markers.THIN_BODY_BYTES


@dataclass
class Markers:
    """Vocabularies for content classification. Heuristics over hostile text —
    they drift as parking providers change templates, so they must be editable
    without a code change."""

    parking_providers: list[str] = field(
        default_factory=lambda: list(markers.PARKING_PROVIDERS)
    )
    parking_phrases: list[str] = field(
        default_factory=lambda: list(markers.PARKING_PHRASES)
    )
    parking_weak: list[str] = field(default_factory=lambda: list(markers.PARKING_WEAK))
    for_sale_phrases: list[str] = field(
        default_factory=lambda: list(markers.FOR_SALE_PHRASES)
    )
    soft_404_phrases: list[str] = field(
        default_factory=lambda: list(markers.SOFT_404_PHRASES)
    )
    soft_404_title_markers: list[str] = field(
        default_factory=lambda: list(markers.SOFT_404_TITLE_MARKERS)
    )


@dataclass
class Crawl:
    concurrency: int = constants.DEFAULT_CONCURRENCY
    delay_seconds: float = constants.DEFAULT_CRAWL_DELAY_SECS
    max_retries: int = constants.MAX_RETRIES
    retry_base_seconds: float = constants.RETRY_BASE_SECS
    retry_cap_seconds: float = constants.RETRY_CAP_SECS
    circuit_breaker_threshold: int = constants.CIRCUIT_THRESHOLD


@dataclass
class Policy:
    scoring: Scoring = field(default_factory=Scoring)
    export: Export = field(default_factory=Export)
    enrich: Enrich = field(default_factory=Enrich)
    check: Check = field(default_factory=Check)
    classify: Classify = field(default_factory=Classify)
    markers: Markers = field(default_factory=Markers)
    crawl: Crawl = field(default_factory=Crawl)
    source: str = "built-in defaults"

    # -- provenance ---------------------------------------------------------

    def fingerprint(self) -> str:
        """Short digest of every value that affects a verdict.

        Folded into `effective_classifier_version`, so editing a marker list or a
        weight makes stored verdicts distinguishable from ones judged under the
        old rules — automatically, rather than relying on someone remembering to
        bump a constant.
        """
        payload = {s: asdict(getattr(self, s)) for s in CLASSIFYING_SECTIONS}
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]

    @property
    def effective_classifier_version(self) -> str:
        return f"{constants.CLASSIFIER_VERSION}+{self.fingerprint()}"

    @property
    def is_default(self) -> bool:
        return self.fingerprint() == Policy().fingerprint()

    def describe(self) -> list[tuple[str, str, Any]]:
        """(section, key, value) for `config show`."""
        rows: list[tuple[str, str, Any]] = []
        for f in fields(self):
            if f.name == "source":
                continue
            section = getattr(self, f.name)
            for key, value in asdict(section).items():
                rows.append((f.name, key, value))
        return rows


# --------------------------------------------------------------------------
# Loading and validation
# --------------------------------------------------------------------------

_SECTIONS = {
    "scoring": Scoring, "export": Export, "enrich": Enrich,
    "check": Check, "classify": Classify, "markers": Markers, "crawl": Crawl,
}


def _validate_section(name: str, cls, raw: dict) -> Any:
    """Build one section, rejecting unknown keys and wrong types loudly.

    A silently-ignored typo in a config file is worse than a crash: the operator
    believes they changed a threshold and the tool carries on with the old one.
    """
    known = {f.name: f for f in fields(cls)}
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise ConfigError(
            f"Unknown key(s) in [{name}]: {', '.join(unknown)}.",
            remediation=(
                f"Valid keys for [{name}] are: {', '.join(sorted(known))}. "
                f"Run `wikimill config validate` after editing."
            ),
        )
    values = {}
    for key, value in raw.items():
        expected = known[key].type
        default = getattr(cls(), key)
        if isinstance(default, bool) and not isinstance(value, bool):
            raise ConfigError(f"[{name}].{key} must be true or false, got {value!r}.")
        if isinstance(default, (int, float)) and not isinstance(value, (int, float)):
            raise ConfigError(
                f"[{name}].{key} must be a number, got {type(value).__name__}.",
                remediation=f"Example: {key} = {default}",
            )
        if isinstance(default, list) and not isinstance(value, list):
            raise ConfigError(
                f"[{name}].{key} must be a list, got {type(value).__name__}.",
                remediation=f'Example: {key} = ["a", "b"]',
            )
        if isinstance(default, dict) and not isinstance(value, dict):
            raise ConfigError(
                f"[{name}].{key} must be a table, got {type(value).__name__}.",
                remediation=f'Example: [{name}.{key}]\\n  live = 0',
            )
        values[key] = value
    return cls(**values)


def load(root: Path, *, path: Path | None = None) -> Policy:
    """Load policy from `wikimill.toml`, falling back to built-in defaults.

    A missing file is not an error — the defaults are the shipped policy, and a
    fresh checkout must work without one.
    """
    target = path or (root / POLICY_FILENAME)
    if not target.is_file():
        return Policy()
    try:
        raw = tomllib.loads(target.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"{target.name} is not valid TOML: {exc}",
            remediation=f"Fix the syntax, then run `wikimill config validate`.",
        ) from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read {target}: {exc}") from exc

    unknown = sorted(set(raw) - set(_SECTIONS))
    if unknown:
        raise ConfigError(
            f"Unknown section(s): {', '.join('[' + u + ']' for u in unknown)}.",
            remediation=f"Valid sections: {', '.join('[' + s + ']' for s in sorted(_SECTIONS))}.",
        )

    built = {
        name: _validate_section(name, cls, raw.get(name, {}) or {})
        for name, cls in _SECTIONS.items()
    }
    return Policy(**built, source=str(target))

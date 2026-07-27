"""Marker vocabularies for content-based classification.

These are **heuristics over attacker-influenceable text**, and they drift as
parking providers change their templates. Two consequences shape this module:

* Every marker is deliberately specific. A loose pattern (a bare "404", the word
  "sale") fires on ordinary pages and would mark live sites dead — the most
  damaging error this tool can make, because the operator acts on it.
* Nothing here is a verdict on its own. `rules.py` requires corroboration and
  records which markers fired, so a wrong call can be traced to the rule that
  made it rather than argued about.

Because verdicts are versioned and stored separately from observations,
tightening a list here is a `CLASSIFIER_VERSION` bump plus a re-classify pass —
no refetching, and the old verdicts stay on record for comparison.
"""

from __future__ import annotations

from ..markers import (
    FOR_SALE_PHRASES,
    PARKING_PHRASES,
    PARKING_PROVIDERS,
    PARKING_WEAK,
    SOFT_404_PHRASES,
    SOFT_404_TITLE_MARKERS,
    THIN_BODY_BYTES,
)

__all__ = [
    "FOR_SALE_PHRASES",
    "PARKING_PHRASES",
    "PARKING_PROVIDERS",
    "PARKING_WEAK",
    "SOFT_404_PHRASES",
    "SOFT_404_TITLE_MARKERS",
    "THIN_BODY_BYTES",
    "find",
    "for_sale_signals",
    "parking_signals",
    "soft_404_signals",
]


def _haystack(*parts: str | None) -> str:
    return " ".join(p.lower() for p in parts if p)


def find(text: str, needles: tuple[str, ...]) -> list[str]:
    """Every needle present in `text`. Returned, not counted, so the caller can
    record *which* markers fired — a bare score is unauditable."""
    return [n for n in needles if n in text]


def parking_signals(title: str | None, body: str | None, policy=None) -> tuple[list[str], list[str]]:
    """(strong, weak) parking markers found."""
    m = policy.markers if policy is not None else None
    text = _haystack(title, body)
    providers = tuple(m.parking_providers) if m else PARKING_PROVIDERS
    phrases = tuple(m.parking_phrases) if m else PARKING_PHRASES
    weak_words = tuple(m.parking_weak) if m else PARKING_WEAK
    strong = find(text, providers) + find(text, phrases)
    weak = find(text, weak_words)
    return strong, weak


def for_sale_signals(title: str | None, body: str | None, policy=None) -> list[str]:
    phrases = tuple(policy.markers.for_sale_phrases) if policy else FOR_SALE_PHRASES
    return find(_haystack(title, body), phrases)


def soft_404_signals(title: str | None, body: str | None, policy=None) -> tuple[list[str], list[str]]:
    """(title markers, body phrases) suggesting a not-found page served as 200."""
    m = policy.markers if policy is not None else None
    title_hits = find((title or "").lower(),
                      tuple(m.soft_404_title_markers) if m else SOFT_404_TITLE_MARKERS)
    body_hits = find(_haystack(body), tuple(m.soft_404_phrases) if m else SOFT_404_PHRASES)
    return title_hits, body_hits

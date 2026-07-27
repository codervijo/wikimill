"""Candidate scoring — explainable, and never a black box.

The score exists to *order* a shortlist, not to make a decision. Every component
is recorded with its contribution, so `inspect` can show why a domain ranked
where it did and the operator can disagree with the weighting rather than with
an unexplained number.

**The weights below are policy defaults, not measured figures.** They encode a
view — that a domain cited by many distinct articles, in real citations, that is
actually acquireable, is worth looking at first. They are expected to be tuned
once v1.J produces a real corpus, and they are versioned so a change is
detectable rather than silent.

Two things deliberately do *not* happen here:

* **Nothing is excluded by score.** Scoring ranks; §10's filters exclude. A
  domain that scores zero still appears with its zero, because "we looked and it
  is uninteresting" is different from "we never looked".
* **`is_private_suffix` penalises rather than removes.** The PSL cannot tell
  `foo.blogspot.com` (never acquireable) from `wbc.poznan.pl` (possibly), so the
  uncertainty is priced in, not resolved by guessing.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from .constants import DomainState, UrlState

SCORER_VERSION = 1

# Acquireability: how close is this domain to being obtainable at all?
STATE_POINTS: dict[str, int] = {
    DomainState.UNREGISTERED: 50,   # available now — the thing we are hunting
    DomainState.EXPIRING: 35,       # a dated, actionable window
    DomainState.FOR_SALE: 30,       # obtainable, at a price
    DomainState.PARKED: 20,         # often a waypoint to expiry
    DomainState.NO_RDAP_FOR_TLD: 5, # unverifiable — a little, for the uncertainty
    DomainState.UNKNOWN: 3,
    DomainState.ACTIVE: 0,          # healthy: not a candidate, but still listed
}

# URL-level death is corroboration even when the domain still resolves.
URL_DEATH_POINTS: dict[str, int] = {
    UrlState.DNS_FAILURE: 6,
    UrlState.UNREGISTERED: 6,
    UrlState.FOR_SALE: 5,
    UrlState.PARKED: 4,
    UrlState.TLS_FAILURE: 3,
    UrlState.SOFT_404: 2,
    UrlState.HARD_404: 2,
}

CITATION_POINTS_PER_PAGE = 3
CITATION_POINTS_CAP = 30      # a domain cited by 50 articles is not 5× one cited by 10
KIND_POINTS = {"citation": 8, "external_links_section": 5, "further_reading": 4}
DEAD_LINK_TAGGED_POINTS = 5   # a Wikipedia editor already noticed
PRIVATE_SUFFIX_PENALTY = -15


@dataclass
class Component:
    name: str
    points: int
    detail: str


@dataclass
class Score:
    total: int = 0
    components: list[Component] = field(default_factory=list)
    version: int = SCORER_VERSION

    def add(self, name: str, points: int, detail: str) -> None:
        if points:
            self.components.append(Component(name, points, detail))
            self.total += points

    def as_json(self) -> str:
        return json.dumps(
            {
                "version": self.version,
                "total": self.total,
                "components": [
                    {"name": c.name, "points": c.points, "detail": c.detail}
                    for c in self.components
                ],
            }
        )


def score_domain(
    row: sqlite3.Row,
    url_states: dict[str, int],
    kinds: dict[str, int],
    policy=None,
) -> Score:
    """Score one domain from its own row plus its URL and link evidence.

    `policy` supplies the weights. Passed as an argument rather than read from a
    global so the function stays pure: same inputs, same score, and a test can
    hand it a policy without touching the filesystem.
    """
    w = policy.scoring if policy is not None else None
    state_points = w.state_points if w else STATE_POINTS
    death_points = w.url_death_points if w else URL_DEATH_POINTS
    kind_points = w.kind_points if w else KIND_POINTS
    per_page = w.citation_points_per_page if w else CITATION_POINTS_PER_PAGE
    cap = w.citation_points_cap if w else CITATION_POINTS_CAP
    tagged_points = w.dead_link_tagged_points if w else DEAD_LINK_TAGGED_POINTS
    penalty = w.private_suffix_penalty if w else PRIVATE_SUFFIX_PENALTY
    score = Score()

    state = row["state"] or DomainState.UNKNOWN
    score.add("acquireability", state_points.get(str(state), 0), f"domain is {state}")

    pages = row["wiki_page_count"] or 0
    if pages:
        score.add(
            "citations",
            min(pages * per_page, cap),
            f"cited by {pages} distinct Wikipedia page(s)",
        )

    for url_state, points in death_points.items():
        count = url_states.get(str(url_state), 0)
        if count:
            score.add("url evidence", points, f"{count} URL(s) {url_state}")
            break  # strongest signal only — these are not additive

    for kind, points in kind_points.items():
        count = kinds.get(kind, 0)
        if count:
            score.add("citation quality", points, f"appears in {kind}")
            break

    if kinds.get("__dead_link_tagged__"):
        score.add(
            "editor corroboration",
            tagged_points,
            "Wikipedia tagged {{dead link}}",
        )

    if row["is_private_suffix"]:
        score.add(
            "private suffix",
            penalty,
            f"under {row['public_suffix']} — may not be independently acquireable",
        )

    return score


def evidence_for(conn: sqlite3.Connection, domain_id: int) -> tuple[dict, dict]:
    """URL-state tally and link-kind tally for one domain."""
    url_states = {
        r["state"]: r["n"]
        for r in conn.execute(
            "SELECT state, COUNT(*) n FROM urls WHERE domain_id=? GROUP BY state",
            (domain_id,),
        )
    }
    kinds = {
        r["link_kind"]: r["n"]
        for r in conn.execute(
            "SELECT e.link_kind, COUNT(*) n FROM external_links e "
            "JOIN urls u ON u.url_hash = e.url_hash "
            "WHERE u.domain_id=? AND e.link_kind IS NOT NULL GROUP BY e.link_kind",
            (domain_id,),
        )
    }
    tagged = conn.execute(
        "SELECT COUNT(*) n FROM external_links e JOIN urls u ON u.url_hash = e.url_hash "
        "WHERE u.domain_id=? AND e.dead_link_tagged=1",
        (domain_id,),
    ).fetchone()["n"]
    if tagged:
        kinds["__dead_link_tagged__"] = tagged
    return url_states, kinds


def rescore_all(conn: sqlite3.Connection, policy=None) -> int:
    """Recompute every domain's score. Cheap, deterministic, and idempotent."""
    rows = conn.execute(
        "SELECT domain_id, state, wiki_page_count, is_private_suffix, public_suffix "
        "FROM domains WHERE registrable_domain != ''"
    ).fetchall()
    for row in rows:
        url_states, kinds = evidence_for(conn, row["domain_id"])
        score = score_domain(row, url_states, kinds, policy)
        conn.execute(
            "UPDATE domains SET candidate_score=?, score_explanation=? WHERE domain_id=?",
            (score.total, score.as_json(), row["domain_id"]),
        )
    return len(rows)

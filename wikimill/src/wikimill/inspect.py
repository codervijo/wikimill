"""`inspect` — everything known about one URL or domain.

The answer to "why does the export say that?". Every verdict in this system
carries its reasons and its history, and this is where they are shown: the full
check timeline, the classifier's reasoning, the RDAP record, and every Wikipedia
article that cites the thing — with the section and anchor text that explain
what it was cited *for*.

Reads only. It never re-checks, so it is safe to run against a database a crawl
is writing to.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from .normalize import analyse, normalize, url_hash


@dataclass
class Report:
    kind: str                      # "domain" | "url" | "unknown"
    identifier: str
    domain: dict | None = None
    urls: list[dict] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    domain_checks: list[dict] = field(default_factory=list)
    citations: list[dict] = field(default_factory=list)
    score: dict | None = None

    @property
    def found(self) -> bool:
        return self.kind != "unknown"


def _rows(cursor) -> list[dict]:
    return [dict(r) for r in cursor]


def resolve(conn: sqlite3.Connection, target: str) -> tuple[str, str]:
    """Decide whether the operator meant a URL or a domain, and canonicalize it.

    A bare `example.com` is a domain; anything with a path or scheme is treated
    as a URL first and falls back to its domain — so `inspect example.com/x`
    still says something useful rather than "not found".
    """
    looks_like_url = "://" in target or "/" in target
    if looks_like_url:
        normalized = normalize(target)
        if normalized.keep:
            hashed = url_hash(normalized.url)
            hit = conn.execute(
                "SELECT 1 FROM urls WHERE url_hash=?", (hashed,)
            ).fetchone()
            if hit:
                return "url", hashed
        host = analyse(normalized.host or target).registrable_domain
        if host:
            return "domain", host
        return "unknown", target

    registrable = analyse(target).registrable_domain or target
    hit = conn.execute(
        "SELECT 1 FROM domains WHERE registrable_domain=?", (registrable,)
    ).fetchone()
    return ("domain", registrable) if hit else ("unknown", target)


def gather(conn: sqlite3.Connection, target: str) -> Report:
    """Collect everything the database knows about `target`."""
    kind, identifier = resolve(conn, target)
    report = Report(kind=kind, identifier=identifier)
    if kind == "unknown":
        return report

    if kind == "domain":
        row = conn.execute(
            "SELECT * FROM domains WHERE registrable_domain=?", (identifier,)
        ).fetchone()
        if row is None:
            report.kind = "unknown"
            return report
        report.domain = dict(row)
        domain_id = row["domain_id"]
        if row["score_explanation"]:
            try:
                report.score = json.loads(row["score_explanation"])
            except json.JSONDecodeError:
                pass
        report.urls = _rows(
            conn.execute(
                "SELECT url_hash, url_normalized, state, terminal, last_checked, "
                "next_check_at, check_count, cite_count FROM urls WHERE domain_id=? "
                "ORDER BY url_normalized",
                (domain_id,),
            )
        )
        report.domain_checks = _rows(
            conn.execute(
                "SELECT c.*, k.state AS verdict, k.reasons, k.confidence "
                "FROM domain_checks c "
                "LEFT JOIN domain_classifications k ON k.check_id = c.id "
                "WHERE c.domain_id=? ORDER BY c.checked_at DESC LIMIT 20",
                (domain_id,),
            )
        )
        hashes = [u["url_hash"] for u in report.urls]
    else:
        row = conn.execute(
            "SELECT * FROM urls WHERE url_hash=?", (identifier,)
        ).fetchone()
        report.urls = [dict(row)] if row else []
        hashes = [identifier]
        if row and row["domain_id"]:
            domain_row = conn.execute(
                "SELECT * FROM domains WHERE domain_id=?", (row["domain_id"],)
            ).fetchone()
            report.domain = dict(domain_row) if domain_row else None

    if hashes:
        placeholders = ",".join("?" * len(hashes))
        report.checks = _rows(
            conn.execute(
                f"SELECT c.id, c.url_hash, c.checked_at, c.http_status, c.final_url, "
                f"c.redirect_count, c.cross_domain_redirect, c.page_title, "
                f"c.latency_ms, c.error_kind, c.robots_decision, "
                f"k.classification, k.reasons, k.confidence, k.classifier_version "
                f"FROM url_checks c "
                f"LEFT JOIN url_classifications k ON k.check_id = c.id "
                f"WHERE c.url_hash IN ({placeholders}) "
                f"ORDER BY c.checked_at DESC, c.id DESC LIMIT 40",
                hashes,
            )
        )
        report.citations = _rows(
            conn.execute(
                f"SELECT p.title, p.lang, e.section, e.anchor_text, e.link_kind, "
                f"e.ref_name, e.template_name, e.dead_link_tagged, e.archive_url, "
                f"e.enrich_status, e.url_raw "
                f"FROM external_links e "
                f"JOIN wiki_pages p ON p.page_id = e.page_id AND p.dump_run = e.dump_run "
                f"WHERE e.url_hash IN ({placeholders}) "
                f"ORDER BY (e.enrich_status='done') DESC, p.title LIMIT 40",
                hashes,
            )
        )
    return report

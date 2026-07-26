"""Stage 8 — export: a self-contained candidate file.

The deliverable. A row has to be **actionable without reopening the tool**, so
each carries not just a verdict but the evidence behind it: which Wikipedia
articles cited the domain, in what section, with what anchor text.

Two properties the rest of the design depends on:

* **Deterministic in its content.** Fixed column order and fixed row order (with
  a name tie-break, never insertion order), so the same filter over the same
  database yields identical *data* every time. The recorded `sha256` covers the
  data only — deliberately **not** the provenance header, which carries the
  generation timestamp and so differs on every run. Hashing the whole file would
  make the digest change when nothing about the findings had, which is precisely
  the question the digest exists to answer. Two exports a week apart therefore
  differ by exactly one header line plus whatever genuinely changed.
* **Attributable by construction.** Anchor text and context excerpts are
  fragments of CC BY-SA content, so every export carries the licence and each
  row carries its source page URL. Whatever the operator later does with the
  file, the attribution is already in it (prd.md §17).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .constants import DomainState
from .logging import utcnow

# States worth exporting by default: everything with a route to acquisition.
# `active` is excluded — a healthy domain is not a candidate — but is reachable
# with an explicit `--state`.
DEFAULT_STATES = (
    DomainState.UNREGISTERED,
    DomainState.EXPIRING,
    DomainState.FOR_SALE,
    DomainState.PARKED,
    DomainState.NO_RDAP_FOR_TLD,
)

COLUMNS = (
    "domain",
    "state",
    "score",
    "wiki_pages",
    "wiki_links",
    "last_checked",
    "registrar",
    "expiry",
    "public_suffix",
    "private_suffix",
    "example_article",
    "example_article_url",
    "example_section",
    "example_anchor",
    "example_link_kind",
    "dead_link_tagged",
    "archive_url",
    "score_explanation",
)

LICENCE_HEADER = (
    "# wikimill candidate export — generated {when}\n"
    "# filter: {filter}\n"
    "#\n"
    "# Anchor text, section names and article titles below are excerpts of\n"
    "# Wikipedia content, licensed CC BY-SA 4.0 (also GFDL).\n"
    "# https://creativecommons.org/licenses/by-sa/4.0/\n"
    "# Each row carries example_article_url for attribution. Redistribution\n"
    "# requires attribution and share-alike.\n"
)


@dataclass
class ExportStats:
    rows: int = 0
    path: str | None = None
    sha256: str | None = None
    states: tuple[str, ...] = ()


def article_url(title: str, lang: str = "en") -> str:
    """The canonical article URL — the attribution anchor for every row."""
    return f"https://{lang}.wikipedia.org/wiki/{title.replace(' ', '_')}"


def _representative_citation(conn: sqlite3.Connection, domain_id: int) -> dict:
    """One citation that best explains why this domain matters.

    Prefers an enriched row: a real citation with a section and anchor says far
    more than a bare URL. Falls back to any link, so a domain is never exported
    with no provenance at all.
    """
    row = conn.execute(
        """
        SELECT p.title, p.lang, e.section, e.anchor_text, e.link_kind,
               e.dead_link_tagged, e.archive_url
        FROM external_links e
        JOIN urls u ON u.url_hash = e.url_hash
        JOIN wiki_pages p ON p.page_id = e.page_id AND p.dump_run = e.dump_run
        WHERE u.domain_id = ?
        ORDER BY (e.enrich_status = 'done') DESC,
                 (e.link_kind = 'citation') DESC,
                 e.dead_link_tagged DESC,
                 p.title
        LIMIT 1
        """,
        (domain_id,),
    ).fetchone()
    if row is None:
        return {}
    return {
        "example_article": row["title"],
        "example_article_url": article_url(row["title"], row["lang"] or "en"),
        "example_section": row["section"] or "",
        "example_anchor": row["anchor_text"] or "",
        "example_link_kind": row["link_kind"] or "",
        "dead_link_tagged": "yes" if row["dead_link_tagged"] else "",
        "archive_url": row["archive_url"] or "",
    }


def collect(
    conn: sqlite3.Connection, states: list[str], min_pages: int
) -> list[dict]:
    """Gather candidate rows, in a fixed order so the output is deterministic."""
    rows = conn.execute(
        "SELECT domain_id, registrable_domain, state, candidate_score, "
        " score_explanation, wiki_page_count, wiki_link_count, last_checked, "
        " public_suffix, is_private_suffix "
        "FROM domains WHERE state IN (" + ",".join("?" * len(states)) + ") "
        "AND wiki_page_count >= ? AND registrable_domain != '' "
        # Deterministic tie-break on the domain name, never insertion order.
        "ORDER BY candidate_score DESC, wiki_page_count DESC, registrable_domain",
        (*states, min_pages),
    ).fetchall()

    out: list[dict] = []
    for row in rows:
        check = conn.execute(
            "SELECT registrar, registration_expiry FROM domain_checks "
            "WHERE domain_id=? ORDER BY id DESC LIMIT 1",
            (row["domain_id"],),
        ).fetchone()
        record = {
            "domain": row["registrable_domain"],
            "state": row["state"],
            "score": row["candidate_score"] if row["candidate_score"] is not None else 0,
            "wiki_pages": row["wiki_page_count"],
            "wiki_links": row["wiki_link_count"],
            "last_checked": row["last_checked"] or "",
            "registrar": (check["registrar"] if check else "") or "",
            "expiry": (check["registration_expiry"] if check else "") or "",
            "public_suffix": row["public_suffix"] or "",
            "private_suffix": "yes" if row["is_private_suffix"] else "",
            "example_article": "",
            "example_article_url": "",
            "example_section": "",
            "example_anchor": "",
            "example_link_kind": "",
            "dead_link_tagged": "",
            "archive_url": "",
            "score_explanation": row["score_explanation"] or "",
        }
        record.update(_representative_citation(conn, row["domain_id"]))
        out.append(record)
    return out


def render_csv(records: list[dict], *, filter_desc: str, when: str) -> str:
    """Provenance header + data. Only the data is hashed (see `write`)."""
    return LICENCE_HEADER.format(when=when, filter=filter_desc) + render_csv_body(
        records
    )


def render_csv_body(records: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(COLUMNS), lineterminator="\n")
    writer.writeheader()
    for record in records:
        writer.writerow({k: record.get(k, "") for k in COLUMNS})
    return buffer.getvalue()


def render_jsonl(records: list[dict], *, filter_desc: str, when: str) -> str:
    return _jsonl_meta(filter_desc, when) + render_jsonl_body(records)


def _jsonl_meta(filter_desc: str, when: str) -> str:
    return json.dumps(
        {
            "_meta": {
                "generated": when,
                "filter": filter_desc,
                "licence": "CC BY-SA 4.0",
                "licence_url": "https://creativecommons.org/licenses/by-sa/4.0/",
                "note": "Anchor text and section names are Wikipedia excerpts.",
            }
        },
        sort_keys=True,
    ) + "\n"


def render_jsonl_body(records: list[dict]) -> str:
    lines: list[str] = []
    for record in records:
        parsed = dict(record)
        if parsed.get("score_explanation"):
            try:
                parsed["score_explanation"] = json.loads(parsed["score_explanation"])
            except json.JSONDecodeError:
                pass
        lines.append(json.dumps(parsed, sort_keys=True))
    return "\n".join(lines) + "\n"


def write(
    conn: sqlite3.Connection,
    out_path: Path,
    *,
    states: list[str],
    min_pages: int,
    fmt: str,
    when: str | None = None,
) -> ExportStats:
    """Render and write the candidate file, recording its digest."""
    stamp = when or utcnow()
    filter_desc = f"states={','.join(states)} min_pages={min_pages}"
    records = collect(conn, states, min_pages)

    if fmt == "jsonl":
        header = _jsonl_meta(filter_desc, stamp)
        data = render_jsonl_body(records)
    else:
        header = LICENCE_HEADER.format(when=stamp, filter=filter_desc)
        data = render_csv_body(records)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(header + data, encoding="utf-8")

    # The digest covers the DATA only. Including the generated-at header would
    # make it change on every run even when the findings had not — exactly the
    # question the digest exists to answer.
    digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO exports (created_at, filter, row_count, path, sha256) "
        "VALUES (?,?,?,?,?)",
        (stamp, filter_desc, len(records), str(out_path), digest),
    )
    return ExportStats(
        rows=len(records), path=str(out_path), sha256=digest, states=tuple(states)
    )

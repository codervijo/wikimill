"""Stage 5 — domain checks: DNS + RDAP over domains worth asking about.

Selection is evidence-driven. Checking every domain would be wasteful and rude;
the interesting ones are those whose URL-level evidence already suggests trouble
(`dns_failure`, `tls_failure`, `hard_404`, `soft_404`, `parked`, `for_sale`), or
whose recheck window has opened. `--state` overrides, `--force` ignores windows.

Like the crawler, this stage keeps one writer on the main thread; unlike it,
work is paced per **RDAP registry** rather than per target host, because that is
the shared resource being consumed.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..config import Config
from ..constants import DomainState, RunKind, UrlState
from ..logging import RunLog, utcnow
from ..storage import open_db
from . import dns as dns_mod
from . import rdap as rdap_mod
from .rdap import expires_within
from .rules import EXPIRY_WATCH_DAYS, classify

# URL classifications that make a domain worth an authoritative check.
INTERESTING_URL_STATES = (
    UrlState.DNS_FAILURE,
    UrlState.TLS_FAILURE,
    UrlState.HARD_404,
    UrlState.SOFT_404,
    UrlState.PARKED,
    UrlState.FOR_SALE,
)

RECHECK_DAYS = {
    DomainState.UNREGISTERED: 3,
    DomainState.EXPIRING: 1,
    DomainState.FOR_SALE: 7,
    DomainState.PARKED: 7,
    DomainState.ACTIVE: 90,
    DomainState.NO_RDAP_FOR_TLD: 30,
    DomainState.UNKNOWN: 7,
}


@dataclass
class CheckStats:
    considered: int = 0
    checked: int = 0
    states: Counter = field(default_factory=Counter)
    rdap_gaps: Counter = field(default_factory=Counter)
    unregistered: list[str] = field(default_factory=list)


@dataclass
class _Target:
    domain_id: int
    domain: str


def select_domains(
    conn: sqlite3.Connection,
    *,
    limit: int | None,
    states: list[str] | None,
    force: bool,
) -> list[_Target]:
    """Pick domains worth an authoritative check."""
    params: list = []
    where = ["d.registrable_domain != ''", "d.terminal = 0"]

    if states:
        where.append(
            "d.state IN (" + ",".join("?" * len(states)) + ")"
        )
        params.extend(states)
    else:
        placeholders = ",".join("?" * len(INTERESTING_URL_STATES))
        where.append(
            f"""(
                d.last_checked IS NULL
                OR EXISTS (
                    SELECT 1 FROM urls u
                    WHERE u.domain_id = d.domain_id AND u.state IN ({placeholders})
                )
            )"""
        )
        params.extend(INTERESTING_URL_STATES)

    if not force:
        where.append("(d.next_check_at IS NULL OR d.next_check_at <= ?)")
        params.append(utcnow())

    sql = (
        "SELECT d.domain_id, d.registrable_domain FROM domains d WHERE "
        + " AND ".join(where)
        + " ORDER BY d.last_checked IS NOT NULL, d.wiki_page_count DESC, d.domain_id"
    )
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [_Target(r["domain_id"], r["registrable_domain"]) for r in conn.execute(sql, params)]


def url_state_tally(conn: sqlite3.Connection, domain_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT state, COUNT(*) n FROM urls WHERE domain_id=? GROUP BY state",
        (domain_id,),
    ).fetchall()
    return {r["state"]: r["n"] for r in rows}


def _record(
    conn: sqlite3.Connection,
    target: _Target,
    dns_result,
    rdap_result,
    verdict,
    stats: CheckStats,
) -> None:
    now = utcnow()
    conn.execute(
        "INSERT INTO domain_checks (domain_id, checked_at, dns_status, a_records, "
        " ns_records, resolvers_agreed, rdap_status, rdap_raw, registrar, "
        " registration_expiry, domain_statuses, latency_ms, error_kind) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            target.domain_id,
            now,
            str(dns_result.status),
            json.dumps(dns_result.a_records),
            json.dumps(dns_result.ns_records),
            int(dns_result.resolvers_agreed),
            str(rdap_result.status),
            rdap_result.raw,
            rdap_result.registrar,
            rdap_result.expiry,
            json.dumps(rdap_result.statuses),
            rdap_result.latency_ms,
            rdap_result.error,
        ),
    )
    check_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT OR IGNORE INTO domain_classifications "
        "(check_id, domain_id, classified_at, classifier_version, state, reasons, "
        " confidence) VALUES (?,?,?,?,?,?,?)",
        (
            check_id,
            target.domain_id,
            now,
            verdict.version,
            verdict.state,
            json.dumps(verdict.reasons),
            verdict.confidence,
        ),
    )
    days = RECHECK_DAYS.get(verdict.state, 30)
    # An approaching expiry no longer changes the *state* (it predicts almost
    # nothing — most domains simply renew), but it is worth watching closely, so
    # it shortens the recheck window. If the registration ever does lapse, the
    # domain enters redemption and the next check catches it.
    if verdict.state == DomainState.ACTIVE and expires_within(
        rdap_result.expiry, EXPIRY_WATCH_DAYS
    ):
        days = min(days, 3)
    conn.execute(
        "UPDATE domains SET state=?, last_checked=?, next_check_at=datetime(?, ?) "
        "WHERE domain_id=?",
        (verdict.state, now, now, f"+{days} days", target.domain_id),
    )
    stats.checked += 1
    stats.states[verdict.state] += 1
    if rdap_result.status == rdap_mod.RdapStatus.NO_RDAP_FOR_TLD:
        stats.rdap_gaps[target.domain.rsplit(".", 1)[-1]] += 1
    if verdict.state == DomainState.UNREGISTERED:
        stats.unregistered.append(target.domain)


def _http_fetch(client, user_agent: str):
    """A JSON-sized fetch for RDAP and the bootstrap registry.

    Reuses the crawler's fetcher so RDAP requests inherit the same SSRF checks,
    redirect caps and error classification — a different HTTP path here would be
    a second thing to keep correct.
    """
    from ..crawl.fetcher import fetch as http_fetch

    def _fetch(url: str) -> tuple[int | None, str]:
        result = http_fetch(
            client,
            url,
            user_agent=user_agent,
            max_body=rdap_mod.MAX_RDAP_BYTES,
            max_evidence=rdap_mod.MAX_RDAP_BYTES,
        )
        if result.error_kind is not None:
            return None, ""
        return result.http_status, result.evidence_blob or ""

    return _fetch


def run(
    cfg: Config,
    log: RunLog,
    *,
    limit: int | None = None,
    states: str | None = None,
    force: bool = False,
) -> CheckStats:
    """Execute the domain-check stage."""
    from ..crawl.fetcher import build_client

    stats = CheckStats()
    wanted = [s.strip() for s in states.split(",")] if states else None
    resolvers = cfg.dns_resolvers

    if len(resolvers) < 2:
        log.fail(
            "resolvers",
            f"only {len(resolvers)} configured — an NXDOMAIN needs corroboration",
        )
        log.progress(
            "→ Set WIKIMILL_DNS_RESOLVERS to at least two independent resolvers, "
            "e.g. 1.1.1.1,8.8.8.8"
        )
        return stats

    with open_db(cfg.db_path) as conn:
        targets = select_domains(conn, limit=limit, states=wanted, force=force)
        stats.considered = len(targets)
        if not targets:
            log.warn("queue", "no domains due — nothing to check")
            return stats

        log.ok("resolvers", f"{len(resolvers)} independent: {', '.join(resolvers)}")
        log.ok("queue", f"{len(targets):,} domain(s)")

        with build_client() as client:
            fetch = _http_fetch(client, cfg.user_agent)
            bootstrap = rdap_mod.load_bootstrap(cfg.state_dir, fetch)
            if bootstrap is None:
                log.warn("rdap", "bootstrap registry unavailable — DNS only this run")
            else:
                log.ok("rdap", f"IANA bootstrap: {len(bootstrap):,} TLDs")

            conn.execute("BEGIN")
            for index, target in enumerate(targets, 1):
                dns_result = dns_mod.lookup(target.domain, resolvers)
                rdap_result = rdap_mod.query(target.domain, bootstrap, fetch)
                verdict = classify(
                    dns_result,
                    rdap_result,
                    url_states=url_state_tally(conn, target.domain_id),
                )
                _record(conn, target, dns_result, rdap_result, verdict, stats)
                if index % 10 == 0 or index == len(targets):
                    log.progress(f"checked {index:,}/{len(targets):,}")
            conn.execute("COMMIT")

        conn.execute(
            "INSERT OR REPLACE INTO crawl_runs "
            "(run_id, kind, started_at, ended_at, counts, outcome) VALUES (?,?,?,?,?,?)",
            (
                log.run_id,
                RunKind.CHECK,
                log.started_at,
                utcnow(),
                json.dumps({"checked": stats.checked, "states": dict(stats.states)}),
                "failed" if log.failed else "ok",
            ),
        )

    log.ok("checked", f"{stats.checked:,} domain(s)")
    if stats.rdap_gaps:
        gaps = ", ".join(f".{t}:{n}" for t, n in stats.rdap_gaps.most_common(6))
        log.warn(
            "rdap coverage",
            f"{sum(stats.rdap_gaps.values())} domain(s) under TLDs with no RDAP "
            f"— cannot be confirmed unregistered ({gaps})",
        )
    if stats.states:
        log.note("")
        log.note("domain states:")
        for name, count in stats.states.most_common():
            log.progress(f"{name:<22} {count:>6,}")
    if stats.unregistered:
        log.note("")
        log.ok(
            "UNREGISTERED",
            f"{len(stats.unregistered)} confirmed (2+ resolvers NXDOMAIN + RDAP 404)",
        )
        for name in stats.unregistered[:10]:
            log.progress(name)
    return stats

"""Stage 8 — verify: does enwiki still link here? (v2.F)

Runs immediately before an export, on the candidates that export would emit.
Scoped that way on purpose: the question is only worth asking about domains the
operator is about to be handed, and asking it about all 135,591 would be tens of
hours of requests to Wikimedia for an answer nobody reads.

**`export` itself stays offline and deterministic.** This runs first, writes what
it learned to `wiki_usage_checks`, and then the export proper collects from the
database exactly as it always has. So the digest still covers a pure function of
stored rows; `--verify` adds a step before the export rather than putting a
network call inside one.

Serial by construction — see `wiki/usage.py` for why this is the one stage that
must not be parallelised.

## The two counts are not symmetric, and that makes the signal conservative

`dump_page_count` is how many pages **in the ingested slice** cite a domain.
`live_page_count` is how many articles **in all of enwiki** cite it now. Until a
full-enwiki ingest (v3.D) those are different populations, and the first is a
subset of the second. Measured on the real corpus: a 27,152-page slice against
a wiki of millions, where every verified domain came back with a far larger live
count.

The direction of that asymmetry is what makes it safe. Writing `slice_then` for
the dump count and `enwiki_now` for the live one:

    slice_then <= enwiki_then          (the slice is part of the wiki)

so if `slice_then > enwiki_now`, then `enwiki_then > enwiki_now` — a real net
removal, proven, not inferred. And the reported loss satisfies

    slice_then - enwiki_now <= enwiki_then - enwiki_now = the true loss

so the figure is a **lower bound**: it will miss removals, and it cannot invent
one. Under-reporting is the correct way for this to be wrong.

The converse carries no information at all. `live > dump` is simply the wiki
being bigger than the slice, which is the expected case and says nothing about
whether editors kept or dropped anything — so it is reported as coverage, never
as good news about the source.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from .config import Config
from .constants import RunKind
from .logging import RunLog, utcnow
from .storage import open_db
from .wiki import usage as usage_mod


@dataclass
class VerifyStats:
    selected: int = 0
    checked: int = 0
    failed: int = 0
    unchanged: int = 0
    reduced: int = 0
    # live > dump. On a partial ingest this is the expected case and carries no
    # information — named for what it measures rather than for what it would
    # mean on a full corpus.
    beyond_slice: int = 0
    vanished: int = 0
    truncated: int = 0
    citations_lost: int = 0
    waited_seconds: float = 0.0
    examples: list[tuple[str, int, int]] = field(default_factory=list)


def select_candidates(
    conn: sqlite3.Connection, states: list[str], min_pages: int,
    limit: int | None = None,
) -> list[tuple[int, str, int]]:
    """(domain_id, domain, dump_page_count) for what the export would emit.

    Highest citation counts first: those are the rows whose evidence carries the
    most weight in the export, so they are the ones worth spending a request on
    when `--limit` cuts the run short.
    """
    sql = (
        "SELECT domain_id, registrable_domain, wiki_page_count FROM domains "
        "WHERE state IN (" + ",".join("?" * len(states)) + ") "
        "AND wiki_page_count >= ? AND registrable_domain != '' "
        "ORDER BY wiki_page_count DESC, registrable_domain"
    )
    params: list = [*states, min_pages]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [
        (r["domain_id"], r["registrable_domain"], r["wiki_page_count"])
        for r in conn.execute(sql, params)
    ]


def record(
    conn: sqlite3.Connection,
    domain_id: int,
    dump_count: int,
    result: usage_mod.UsageResult,
    endpoint: str,
) -> None:
    """Append one observation. A failed check is recorded too — "we could not
    ask" must never later read as "the answer was zero"."""
    conn.execute(
        "INSERT INTO wiki_usage_checks (domain_id, checked_at, live_page_count, "
        " dump_page_count, truncated, api_endpoint, error_kind, latency_ms) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            domain_id,
            utcnow(),
            result.live_page_count,
            dump_count,
            int(result.truncated),
            endpoint,
            result.error_kind,
            result.latency_ms,
        ),
    )


def latest(conn: sqlite3.Connection, domain_id: int) -> sqlite3.Row | None:
    """The most recent successful check for one domain."""
    return conn.execute(
        "SELECT * FROM wiki_usage_checks WHERE domain_id=? AND error_kind IS NULL "
        "ORDER BY id DESC LIMIT 1",
        (domain_id,),
    ).fetchone()


def citations_lost(conn: sqlite3.Connection, domain_id: int) -> int:
    """Citations the dump claimed that the live wiki no longer has.

    Clamped at zero: editors adding citations since the dump is good news about
    the source, and must not read as negative evidence of death. A `truncated`
    count is a floor, so it can only ever understate the live side — meaning it
    could overstate the loss, and is therefore not used for this at all.
    """
    row = latest(conn, domain_id)
    if row is None or row["truncated"] or row["live_page_count"] is None:
        return 0
    return max(0, row["dump_page_count"] - row["live_page_count"])


def run(
    cfg: Config,
    log: RunLog,
    *,
    states: list[str],
    min_pages: int,
    limit: int | None = None,
    endpoint: str | None = None,
    delay: float = 1.0,
    client=None,
    sleep=time.sleep,
) -> VerifyStats:
    """Ask the live wiki about each candidate, serially."""
    from .crawl.fetcher import build_client

    stats = VerifyStats()
    target = endpoint or usage_mod.DEFAULT_ENDPOINT
    user_agent = usage_mod.require_identity(cfg.user_agent)

    with open_db(cfg.db_path) as conn:
        candidates = select_candidates(conn, states, min_pages, limit)
        stats.selected = len(candidates)
        if not candidates:
            log.warn("verify", "no candidates to verify — nothing was requested")
            return stats

        log.ok(
            "verify",
            f"{len(candidates):,} candidate(s) against {target} · serial, "
            f"maxlag={usage_mod.DEFAULT_MAXLAG}",
        )

        owned = client is None
        if owned:
            client = build_client(
                headers={"User-Agent": user_agent, "Accept": "application/json"},
                follow_redirects=True,
            )
        try:
            for index, (domain_id, domain, dump_count) in enumerate(candidates, 1):
                if index > 1 and delay > 0:
                    # One operator's shared cluster: pace ourselves rather than
                    # waiting to be told to.
                    sleep(delay)
                    stats.waited_seconds += delay

                result = usage_mod.check_domain(client, domain, endpoint=target)
                conn.execute("BEGIN")
                record(conn, domain_id, dump_count, result, target)
                conn.execute("COMMIT")

                if not result.ok:
                    stats.failed += 1
                    log.warn("verify", f"{domain}: {result.error_kind}")
                    if result.retry_after:
                        log.progress(f"honouring Retry-After: {result.retry_after:.0f}s")
                        sleep(result.retry_after)
                        stats.waited_seconds += result.retry_after
                    continue

                stats.checked += 1
                live = result.live_page_count or 0
                if result.truncated:
                    stats.truncated += 1
                elif live == 0 and dump_count > 0:
                    stats.vanished += 1
                    stats.citations_lost += dump_count
                    if len(stats.examples) < 10:
                        stats.examples.append((domain, dump_count, live))
                elif live < dump_count:
                    stats.reduced += 1
                    stats.citations_lost += dump_count - live
                    if len(stats.examples) < 10:
                        stats.examples.append((domain, dump_count, live))
                elif live > dump_count:
                    stats.beyond_slice += 1
                else:
                    stats.unchanged += 1

                if index % 25 == 0 or index == len(candidates):
                    log.progress(
                        f"{index:,}/{len(candidates):,} · "
                        f"{stats.reduced + stats.vanished:,} with fewer citations"
                    )
        finally:
            if owned:
                client.close()

        conn.execute(
            "INSERT OR REPLACE INTO crawl_runs "
            "(run_id, kind, started_at, ended_at, counts, outcome) VALUES (?,?,?,?,?,?)",
            (
                f"{log.run_id}-verify",
                RunKind.EXPORT,
                log.started_at,
                utcnow(),
                repr({
                    "selected": stats.selected,
                    "checked": stats.checked,
                    "failed": stats.failed,
                    "citations_lost": stats.citations_lost,
                }),
                "ok" if not log.failed else "failed",
            ),
        )

    _report(log, stats)
    return stats


def _report(log: RunLog, stats: VerifyStats) -> None:
    if stats.vanished:
        # The headline: the export was about to claim citations that no longer
        # exist, which is the strongest thing it says about a candidate.
        log.warn(
            "no longer cited",
            f"{stats.vanished:,} domain(s) enwiki no longer links to at all",
        )
    if stats.reduced:
        log.warn(
            "fewer citations",
            f"{stats.reduced:,} domain(s) lost some since the dump "
            f"({stats.citations_lost:,} citation(s) in total)",
        )
    if stats.unchanged:
        log.ok("confirmed", f"{stats.unchanged:,} domain(s) match the dump exactly")
    if stats.beyond_slice:
        # Deliberately not phrased as "gained citations". Until the ingest
        # covers all of enwiki, this only says the wiki is larger than the
        # slice — which it always is.
        log.note(
            f"↷ cited beyond slice   {stats.beyond_slice:,} domain(s) are cited by "
            "articles outside the ingested slice — expected, and not a signal"
        )
    if stats.truncated:
        log.warn(
            "count truncated",
            f"{stats.truncated:,} domain(s) are cited too widely to count exactly — "
            "recorded as a floor, and excluded from the loss figure",
        )
    if stats.failed:
        log.warn(
            "not answered",
            f"{stats.failed:,} domain(s) could not be checked — recorded as "
            "errors, never as zero",
        )
    for domain, was, now_count in stats.examples:
        log.progress(f"{was} → {now_count} citations   {domain}")

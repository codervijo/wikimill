"""Typer CLI — eight flat commands, no nested subcommands (prd.md §15).

Only `preflight` and `stats` are functional at v1.B. The other six exist with
their real signatures and raise a typed `NotImplementedYetError` naming the
phase that ships them. That is deliberate: the command surface is agreed, so
`--help` should tell the truth about the shape of the tool now, and a stub that
names its phase is far better than a missing command or a stack trace.
"""

from __future__ import annotations

import json as jsonlib
import sys
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from . import ingest as ingest_stage
from .classify import runner as classify_stage
from .crawl import runner as crawl_stage
from .domain import runner as domain_stage
from .enrich import runner as enrich_stage
from . import export as export_mod
from . import inspect as inspect_mod
from . import diff as diff_mod
from . import policy as policy_mod
from . import gaps as gaps_mod
from . import report as report_mod
from . import schedule as schedule_mod
from . import verify as verify_mod
from . import score as score_mod
from .config import load as load_config
from .constants import EXIT_INTERRUPTED, EXIT_OK, RunKind
from .errors import ConfigError, Interrupted, NotImplementedYetError, WikimillError
from .logging import RunLog
from .wiki import msindex
from .preflight import gate, run_checks, verify_dumps
from .preflight import preflight as run_preflight
from .storage import counts, open_db, user_version

# `no_args_is_help` is deliberately NOT used: Click implements it by exiting 2,
# and our exit-code contract reserves 2 for a preflight failure (prd.md §13).
# Showing help is a success, so the root callback does it and exits 0.
app = typer.Typer(
    name="wikimill",
    help="Crawl Wikipedia's external links; find the dead, parked, and expired.",
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"wikimill {__version__}")
        raise typer.Exit(EXIT_OK)


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True,
                     help="Show the version and exit."),
    ] = False,
) -> None:
    """wikimill — Wikipedia external-link crawler."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(EXIT_OK)


# --------------------------------------------------------------------------
# v1.B — implemented
# --------------------------------------------------------------------------


@app.command(name="preflight")
def preflight_cmd(
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit the report as JSON on stdout.")
    ] = False,
    verify_dumps_flag: Annotated[
        bool,
        typer.Option(
            "--verify-dumps",
            help="Re-hash every dump file. Slow — 32 GB of I/O, and slower on USB.",
        ),
    ] = False,
) -> None:
    """Check the environment, config, database, and dumps. Fails fast with fixes."""
    cfg = load_config()
    with RunLog(RunKind.PREFLIGHT, cfg.logs_dir, quiet=json_out) as log:
        ok = run_preflight(cfg, log, show_config=not json_out)
        if verify_dumps_flag:
            verify_dumps(cfg, log)
        if json_out:
            payload = {
                "version": __version__,
                "ok": ok,
                "config": [
                    {"name": n, "value": v, "source": s} for n, v, s in cfg.describe()
                ],
                "checks": [
                    {
                        "step": r.step,
                        "marker": r.marker.value,
                        "detail": r.detail,
                        "remediation": r.remediation,
                    }
                    for r in run_checks(cfg)
                ],
            }
            typer.echo(jsonlib.dumps(payload, indent=2))
        if not ok:
            raise typer.Exit(2)


@app.command()
def stats(
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit as JSON on stdout.")
    ] = False,
    due: Annotated[
        bool,
        typer.Option(
            "--due",
            help="Show the recheck schedule: what `crawl` and `check` would "
                 "pick up now, and what is waiting. Reads the DB only.",
        ),
    ] = False,
    diff_out: Annotated[
        bool,
        typer.Option(
            "--diff",
            help="Show what Wikipedia editors added and removed between the two "
                 "most recent ingested dump runs.",
        ),
    ] = False,
) -> None:
    """Row counts by table, queue depth, and recent runs."""
    cfg = load_config()
    with RunLog(RunKind.PREFLIGHT, cfg.logs_dir, quiet=True) as log:
        gate(cfg, log)
    with open_db(cfg.db_path) as conn:
        table_counts = counts(conn)
        version = user_version(conn)
        buckets = schedule_mod.snapshot(conn) if due else None
        diff_view = _diff_view(conn) if diff_out else None
    if json_out:
        payload: dict = {"schema_version": version, "counts": table_counts}
        if buckets:
            payload["schedule"] = {
                queue: {
                    "never_checked": b.never,
                    "due_now": b.due,
                    f"due_within_{schedule_mod.SOON_DAYS}d": b.soon,
                    "due_later": b.later,
                    "terminal": b.terminal,
                    "actionable": b.actionable,
                    "due_by_state": b.due_by_state,
                }
                for queue, b in buckets.items()
            }
        if diff_view is not None:
            payload["diff"] = diff_view
        typer.echo(jsonlib.dumps(payload, indent=2))
        return
    typer.echo(f"schema v{version}  ·  {cfg.db_path}")
    width = max((len(k) for k in table_counts), default=0)
    for table, count in table_counts.items():
        typer.echo(f"  {table:<{width}}  {count:>12,}")
    if not any(table_counts.values()):
        typer.echo("\nEmpty — run `wikimill ingest` once v1.C ships.")
    if buckets:
        _print_schedule(buckets)
    if diff_view is not None:
        _print_diff(diff_view)


def _diff_view(conn) -> dict:
    """Stored transitions between the two most recent ingested runs."""
    runs = diff_mod.list_runs(conn)
    view: dict = {"runs": runs}
    if len(runs) < 2:
        return view
    from_run, to_run = runs[-2], runs[-1]
    view["from_run"], view["to_run"] = from_run, to_run
    view["transitions"] = diff_mod.summary(conn, from_run, to_run)
    view["top_removed_domains"] = [
        {"domain": d, "removed": n}
        for d, n in diff_mod.top_removed_domains(conn, from_run, to_run)
    ]
    return view


def _print_diff(view: dict) -> None:
    runs = view.get("runs", [])
    typer.echo("\ncross-dump-run diff")
    typer.echo(f"  ingested runs              {', '.join(runs) if runs else '(none)'}")
    if len(runs) < 2:
        # Not a failure. One run is the normal state until a second dump lands.
        typer.echo("  → needs two ingested dump runs; nothing to compare yet")
        return
    typer.echo(f"  comparing                  {view['from_run']} → {view['to_run']}")
    transitions = view.get("transitions", {})
    for name in (diff_mod.REMOVED, diff_mod.ADDED):
        typer.echo(f"  {name:<26} {transitions.get(name, 0):>12,}")
    top = view.get("top_removed_domains") or []
    if top:
        typer.echo("  most-dropped domains:")
        for entry in top:
            typer.echo(f"    {entry['domain']:<24} {entry['removed']:>12,}")


def _print_schedule(buckets) -> None:
    """The §12 selection rule, made observable without running it."""
    soon = schedule_mod.SOON_DAYS
    for queue, b in buckets.items():
        typer.echo(f"\n{queue} — recheck schedule")
        for label, value in (
            ("never checked", b.never),
            ("due now", b.due),
            (f"due within {soon}d", b.soon),
            ("due later", b.later),
            ("terminal (needs --force)", b.terminal),
        ):
            typer.echo(f"  {label:<26} {value:>12,}")
        typer.echo(f"  {'→ a run would touch':<26} {b.actionable:>12,}")
        if b.due_by_state:
            typer.echo("  due now, by state:")
            for state, n in b.due_by_state.items():
                typer.echo(f"    {state:<24} {n:>12,}")


# --------------------------------------------------------------------------
# Later phases — real signatures, honest stubs
# --------------------------------------------------------------------------


@app.command()
def ingest(
    dump: Annotated[
        str | None,
        typer.Option("--dump", help="Path to the externallinks SQL dump. "
                                    "Auto-discovered in the dumps dir if omitted."),
    ] = None,
    pages: Annotated[
        str | None,
        typer.Option("--pages", help="Page-ID slice to ingest, e.g. p1p41242."),
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Stop after N links.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report what would be ingested.")
    ] = False,
    include_namespaces: Annotated[
        bool,
        typer.Option(
            "--include-namespaces",
            help="Also ingest Wikipedia:/Portal:/Help:/Draft: pages "
                 "(default: articles only).",
        ),
    ] = False,
) -> None:
    """Seed the link table from the externallinks SQL dump (no wikitext read)."""
    cfg = load_config()
    with RunLog(RunKind.INGEST, cfg.logs_dir) as log:
        gate(cfg, log)
        ingest_stage.run(
            cfg,
            log,
            dump=dump,
            pages=pages,
            limit=limit,
            dry_run=dry_run,
            include_namespaces=include_namespaces,
        )


config_app = typer.Typer(help="Inspect and validate the policy config.")
app.add_typer(config_app, name="config")


@config_app.command("show")
def config_show(
    json_out: Annotated[bool, typer.Option("--json", help="Emit as JSON.")] = False,
    changed: Annotated[
        bool, typer.Option("--changed", help="Only values differing from defaults.")
    ] = False,
) -> None:
    """Show the effective policy and where each value came from."""
    cfg = load_config()
    pol = policy_mod.load(cfg.root)
    base = policy_mod.Policy()
    rows = pol.describe()
    baseline = {(s, k): v for s, k, v in base.describe()}
    if changed:
        rows = [(s, k, v) for s, k, v in rows if baseline[(s, k)] != v]

    if json_out:
        typer.echo(jsonlib.dumps({
            "source": pol.source,
            "effective_classifier_version": pol.effective_classifier_version,
            "is_default": pol.is_default,
            "values": [{"section": s, "key": k, "value": v,
                        "default": baseline[(s, k)] == v} for s, k, v in rows],
        }, indent=2, default=str))
        return

    typer.echo(f"source: {pol.source}")
    typer.echo(f"classifier version: {pol.effective_classifier_version}"
               + ("  (defaults)" if pol.is_default else "  (customised)"))
    if not rows:
        typer.echo("\nnothing differs from the built-in defaults.")
        return
    typer.echo("")
    section = None
    for sec, key, value in rows:
        if sec != section:
            typer.echo(f"[{sec}]")
            section = sec
        mark = " " if baseline[(sec, key)] == value else "*"
        shown = value if not isinstance(value, (list, dict)) else (
            f"{len(value)} entries" if len(str(value)) > 60 else value)
        typer.echo(f" {mark} {key:<32} {shown}")
    if not changed:
        typer.echo("\n* = differs from the built-in default")


@config_app.command("validate")
def config_validate() -> None:
    """Check `wikimill.toml` parses and every key is known."""
    cfg = load_config()
    target = cfg.root / policy_mod.POLICY_FILENAME
    with RunLog(RunKind.PREFLIGHT, cfg.logs_dir) as log:
        if not target.is_file():
            log.warn("config", f"no {policy_mod.POLICY_FILENAME} — using built-in defaults")
            log.progress(f"→ cp {policy_mod.POLICY_EXAMPLE} {policy_mod.POLICY_FILENAME} to start tuning")
            return
        pol = policy_mod.load(cfg.root)   # raises ConfigError with a remediation
        base = policy_mod.Policy()
        diff = sum(1 for s, k, v in pol.describe()
                   if {(a, b): c for a, b, c in base.describe()}[(s, k)] != v)
        log.ok("config", f"{target.name} is valid")
        log.ok("policy", f"{diff} value(s) differ from defaults")
        log.ok("classifier version", pol.effective_classifier_version)


@app.command()
def namespaces(
    pages: Annotated[
        str | None, typer.Option("--pages", help="Restrict to a page-ID slice.")
    ] = None,
    sample: Annotated[
        int, typer.Option("--sample", help="How many index entries to sample.")
    ] = 200_000,
    json_out: Annotated[bool, typer.Option("--json", help="Emit as JSON.")] = False,
) -> None:
    """Measure whether the multistream index is a clean article-namespace filter.

    `externallinks` has no namespace column, so ingest intersects `el_from` with
    the index's page IDs. That this filters to articles is a hypothesis; this
    command is the evidence for it (acceptance criterion 4).
    """
    cfg = load_config()
    with RunLog(RunKind.INGEST, cfg.logs_dir, quiet=json_out) as log:
        gate(cfg, log)
        files = msindex.find_index_files(cfg.dumps_dir)
        page_range = msindex.parse_page_range(pages) if pages else None
        report = msindex.namespace_report(
            msindex.iter_range(files, page_range), sample=sample
        )
    if json_out:
        typer.echo(jsonlib.dumps(report, indent=2))
        return
    typer.echo(f"sampled {report['sampled']:,} index entries")
    typer.echo(f"article fraction: {report['article_fraction']:.4%}")
    if report["by_namespace"]:
        typer.echo("non-article namespaces found:")
        for ns, count in report["by_namespace"].items():
            typer.echo(f"  {ns:<20} {count:>8,}   e.g. {report['examples'][ns]}")
    else:
        typer.echo("no non-article namespace prefixes found in the sample")


@app.command()
def crawl(
    limit: Annotated[int | None, typer.Option("--limit", help="Max URLs.")] = None,
    concurrency: Annotated[
        int | None, typer.Option("--concurrency", help="Global concurrency.")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Re-check terminal/not-yet-due records.")
    ] = False,
    reclassify: Annotated[
        bool,
        typer.Option(
            "--reclassify",
            help="Re-judge stored evidence with the current classifier. "
                 "Makes no network requests at all.",
        ),
    ] = False,
) -> None:
    """Crawl pending and due URLs, honouring robots.txt and per-host politeness."""
    cfg = load_config()
    with RunLog(RunKind.CRAWL, cfg.logs_dir) as log:
        gate(cfg, log)
        if reclassify:
            classify_stage.run(cfg, log, limit=limit, force=force)
            return
        crawl_stage.run(cfg, log, limit=limit, concurrency=concurrency, force=force)


@app.command()
def check(
    limit: Annotated[int | None, typer.Option("--limit", help="Max domains.")] = None,
    state: Annotated[
        str | None, typer.Option("--state", help="Comma-separated states to check.")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Ignore recheck windows.")] = False,
    concurrency: Annotated[
        int | None,
        typer.Option("--concurrency", help="Parallel workers. RDAP stays bounded "
                                           "per registry regardless."),
    ] = None,
) -> None:
    """Run DNS + RDAP against due domains; the only place `unregistered` is set."""
    cfg = load_config()
    with RunLog(RunKind.CHECK, cfg.logs_dir) as log:
        gate(cfg, log)
        domain_stage.run(cfg, log, limit=limit, states=state, force=force,
                         concurrency=concurrency)


@app.command()
def enrich(
    state: Annotated[
        str | None, typer.Option("--state", help="Comma-separated states to enrich.")
    ] = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Max pages.")] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Report pages and blocks that would be read."),
    ] = False,
    no_cache: Annotated[
        bool,
        typer.Option(
            "--no-cache",
            help="Ignore the stored page cache and read every block from the "
                 "archive. Use to re-derive context from the dump itself.",
        ),
    ] = False,
) -> None:
    """Back-fill section, anchor text, and citation context for interesting links only."""
    cfg = load_config()
    with RunLog(RunKind.ENRICH, cfg.logs_dir) as log:
        gate(cfg, log)
        enrich_stage.run(cfg, log, states=state, limit=limit, dry_run=dry_run,
                         no_cache=no_cache)


@app.command()
def inspect(
    target: Annotated[str, typer.Argument(help="A URL or a registrable domain.")],
    json_out: Annotated[bool, typer.Option("--json", help="Emit as JSON.")] = False,
) -> None:
    """Everything known about one URL or domain, including full check history."""
    cfg = load_config()
    with RunLog(RunKind.PREFLIGHT, cfg.logs_dir, quiet=True) as log:
        gate(cfg, log)
    with open_db(cfg.db_path) as conn:
        report = inspect_mod.gather(conn, target)

    if json_out:
        typer.echo(jsonlib.dumps(report.__dict__, indent=2, default=str))
        return
    if not report.found:
        typer.echo(f"{target}: not in the database.")
        typer.echo("  Nothing has been ingested for it, or it was filtered at normalization.")
        raise typer.Exit(1)

    d = report.domain
    if d:
        typer.echo(f"{d['registrable_domain']}  ·  {d['state']}  ·  score {d['candidate_score'] or 0}")
        typer.echo(f"  cited by {d['wiki_page_count']} page(s) via {d['wiki_link_count']} link(s)"
                   f"  ·  {d['url_count']} url(s)")
        if d["last_checked"]:
            typer.echo(f"  last checked {d['last_checked']}  ·  next {d['next_check_at']}")
        if d["is_private_suffix"]:
            typer.echo(f"  under private suffix {d['public_suffix']} — may not be independently acquireable")

    if report.score and report.score.get("components"):
        typer.echo("\nscore")
        for c in report.score["components"]:
            typer.echo(f"  {c['points']:+4d}  {c['name']:<22} {c['detail']}")

    if report.domain_checks:
        typer.echo("\ndomain checks")
        for c in report.domain_checks[:5]:
            typer.echo(f"  {c['checked_at']}  dns={c['dns_status']} rdap={c['rdap_status']}"
                       f" agreed={bool(c['resolvers_agreed'])}  -> {c.get('verdict')}")
            if c.get("registrar"):
                typer.echo(f"      registrar={c['registrar']}  expiry={c['registration_expiry']}")

    if report.urls:
        typer.echo("\nurls")
        for u in report.urls[:12]:
            typer.echo(f"  [{u['state']:<22}] {u['url_normalized'][:78]}")

    if report.checks:
        typer.echo("\ncheck history")
        for c in report.checks[:10]:
            status = c["http_status"] if c["http_status"] is not None else (c["error_kind"] or "-")
            typer.echo(f"  {c['checked_at']}  {str(status):>14}  -> {c['classification'] or '-'}")
            if c["reasons"]:
                try:
                    why = ", ".join(jsonlib.loads(c["reasons"]))
                    typer.echo(f"      why: {why[:88]}")
                except (ValueError, TypeError):
                    pass

    if report.citations:
        typer.echo("\ncited by")
        for c in report.citations[:10]:
            line = f"  {c['title']}"
            if c["section"]:
                line += f"  §{c['section']}"
            if c["link_kind"]:
                line += f"  [{c['link_kind']}]"
            typer.echo(line)
            if c["anchor_text"]:
                typer.echo(f"      anchor: {c['anchor_text'][:76]!r}")
            if c["dead_link_tagged"]:
                typer.echo("      Wikipedia has tagged this {{dead link}}")


@app.command(name="export")
def export_cmd(
    state: Annotated[
        str | None, typer.Option("--state", help="Comma-separated states to export.")
    ] = None,
    min_pages: Annotated[
        int | None,
        typer.Option("--min-pages", help="Minimum citing Wikipedia pages. "
                                         "Defaults to [export].min_pages."),
    ] = None,
    fmt: Annotated[
        str, typer.Option("--format", help="csv or jsonl.")
    ] = "csv",
    out: Annotated[str | None, typer.Option("--out", help="Output path.")] = None,
    verify_flag: Annotated[
        bool,
        typer.Option(
            "--verify",
            help="Ask the live Wikipedia API whether it still links to each "
                 "candidate before exporting. Makes network requests, serially.",
        ),
    ] = False,
    verify_limit: Annotated[
        int | None,
        typer.Option("--verify-limit", help="Cap how many candidates are verified."),
    ] = None,
) -> None:
    """Write a self-contained candidate file with full Wikipedia evidence."""
    cfg = load_config()
    if fmt not in ("csv", "jsonl"):
        raise ConfigError(
            f"Unknown --format {fmt!r}.", remediation="Use --format csv or --format jsonl."
        )
    pol = policy_mod.load(cfg.root)
    # CLI flag > config > built-in default (policy.py).
    states = (
        [s.strip() for s in state.split(",") if s.strip()]
        if state
        else [str(x) for x in pol.export.candidate_states]
    )
    floor = min_pages if min_pages is not None else pol.export.min_pages
    with RunLog(RunKind.EXPORT, cfg.logs_dir) as log:
        gate(cfg, log)
        # The verification pass runs BEFORE the export, never inside it: the
        # export stays a deterministic, offline function of stored rows, and
        # its digest keeps meaning what it meant (prd.md §11).
        if verify_flag:
            verify_mod.run(
                cfg, log,
                states=states,
                min_pages=floor,
                limit=verify_limit if verify_limit is not None else pol.verify.limit,
                endpoint=pol.verify.endpoint,
                delay=pol.verify.delay_seconds,
            )
        with open_db(cfg.db_path) as conn:
            conn.execute("BEGIN")
            scored = score_mod.rescore_all(conn, pol)
            conn.execute("COMMIT")
            log.ok("scored", f"{scored:,} domain(s) · scorer v{score_mod.SCORER_VERSION} "
                             f"· policy {pol.effective_classifier_version}")

            path = Path(out) if out else cfg.outputs_dir / f"candidates.{fmt}"
            conn.execute("BEGIN")
            stats = export_mod.write(
                conn, path, states=states, min_pages=floor, fmt=fmt
            )
            conn.execute("COMMIT")

        log.ok("states", ", ".join(states))
        if stats.rows:
            log.ok("exported", f"{stats.rows:,} candidate(s) -> {stats.path}")
            log.progress(f"sha256 {stats.sha256[:32]}…")
        else:
            log.warn(
                "exported",
                f"0 candidates matched — nothing is in {', '.join(states)} "
                f"with >= {floor} citing page(s)",
            )


@app.command()
def gaps(
    limit: Annotated[
        int | None, typer.Option("--limit", help="Max URLs to ask about.")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Re-ask about URLs already answered.")
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Report what would be asked, ask nothing."),
    ] = False,
) -> None:
    """Ask the Wayback Machine which dead citations still have a usable copy."""
    cfg = load_config()
    pol = policy_mod.load(cfg.root)
    with RunLog(RunKind.EXPORT, cfg.logs_dir) as log:
        gate(cfg, log)
        gaps_mod.run(
            cfg, log,
            limit=limit if limit is not None else pol.gaps.limit,
            force=force,
            dry_run=dry_run,
            endpoint=pol.gaps.endpoint,
            delay=pol.gaps.delay_seconds,
        )


@app.command()
def report(
    out: Annotated[
        str | None, typer.Option("--out", help="Output path. Defaults to "
                                               "outputs/report.html.")
    ] = None,
    state: Annotated[
        str | None, typer.Option("--state", help="Comma-separated states to show.")
    ] = None,
    min_pages: Annotated[
        int | None, typer.Option("--min-pages", help="Minimum citing pages.")
    ] = None,
    limit: Annotated[
        int, typer.Option("--limit", help="Max candidate rows embedded in the page.")
    ] = report_mod.MAX_ROWS,
    watch: Annotated[
        float,
        typer.Option("--watch", help="Regenerate every N seconds until "
                                     "interrupted. The page reloads itself while "
                                     "a stage is running."),
    ] = 0.0,
) -> None:
    """Write a self-contained HTML page: candidates found, and what is running now."""
    import time as _time

    cfg = load_config()
    pol = policy_mod.load(cfg.root)
    states = (
        [s.strip() for s in state.split(",") if s.strip()] if state
        else [str(x) for x in pol.export.candidate_states]
    )
    floor = min_pages if min_pages is not None else pol.export.min_pages
    path = Path(out) if out else cfg.outputs_dir / "report.html"

    with RunLog(RunKind.EXPORT, cfg.logs_dir, quiet=watch > 0) as log:
        gate(cfg, log)
        interval = max(1, int(watch)) if watch else 0

        def once() -> report_mod.ReportData:
            with open_db(cfg.db_path) as conn:
                data = report_mod.collect(conn, cfg, states, floor, limit)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Written whole then moved, so a browser mid-refresh never reads a
            # half-written page — the failure mode `--watch` would otherwise hit
            # constantly.
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(report_mod.render(data, interval), encoding="utf-8")
            tmp.replace(path)
            return data

        if not watch:
            data = once()
            log.ok("report", f"{data.total_candidates:,} candidate(s) -> {path}")
            if data.stalled:
                log.warn(
                    "stalled",
                    f"{len(data.stalled)} stage(s) have stopped reporting progress",
                )
            elif data.live:
                log.ok("live", f"{len(data.live)} stage(s) running")
            log.progress(f"open it with: xdg-open {path}")
            return

        # Watch mode: the operator has a browser open on the file and wants to
        # see a long run move. Print one line per regeneration so the terminal
        # shows liveness too, not only the page.
        print(f"\033[32m✓\033[0m watching — {path} every {interval}s, Ctrl-C to stop",
              flush=True)
        try:
            while True:
                data = once()
                stamp = data.generated_at[11:19]
                if data.stalled:
                    names = ", ".join(s.stage for s in data.stalled)
                    print(f"\033[31m✗\033[0m {stamp} stalled: {names}", flush=True)
                elif data.live:
                    for s in data.live:
                        pct = f" {s.percent:.1f}%" if s.percent is not None else ""
                        print(f"\033[33m↷\033[0m {stamp} {s.stage}{pct} "
                              f"{s.done:,}"
                              f"{'/' + format(s.total, ',') if s.total else ''}"
                              f"  {(s.current_item or '')[:60]}", flush=True)
                else:
                    print(f"\033[32m✓\033[0m {stamp} idle · "
                          f"{data.total_candidates:,} candidates", flush=True)
                _time.sleep(interval)
        except KeyboardInterrupt:
            print(f"\033[32m✓\033[0m stopped — {path} holds the last render",
                  flush=True)


def main() -> None:
    """Console-script entry point. Maps typed errors onto the exit-code contract."""
    try:
        app()
    except WikimillError as exc:
        print(f"\033[31m✗\033[0m {exc}", file=sys.stderr, flush=True)
        raise SystemExit(exc.exit_code) from exc
    except KeyboardInterrupt:
        err = Interrupted()
        print(f"\n\033[33m↷\033[0m {err}", file=sys.stderr, flush=True)
        raise SystemExit(EXIT_INTERRUPTED) from None


if __name__ == "__main__":
    main()

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
from typing import Annotated

import typer

from . import __version__
from . import ingest as ingest_stage
from .classify import runner as classify_stage
from .crawl import runner as crawl_stage
from .domain import runner as domain_stage
from .config import load as load_config
from .constants import EXIT_INTERRUPTED, EXIT_OK, RunKind
from .errors import Interrupted, NotImplementedYetError, WikimillError
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
) -> None:
    """Row counts by table, queue depth, and recent runs."""
    cfg = load_config()
    with RunLog(RunKind.PREFLIGHT, cfg.logs_dir, quiet=True) as log:
        gate(cfg, log)
    with open_db(cfg.db_path) as conn:
        table_counts = counts(conn)
        version = user_version(conn)
    if json_out:
        typer.echo(
            jsonlib.dumps(
                {"schema_version": version, "counts": table_counts}, indent=2
            )
        )
        return
    typer.echo(f"schema v{version}  ·  {cfg.db_path}")
    width = max((len(k) for k in table_counts), default=0)
    for table, count in table_counts.items():
        typer.echo(f"  {table:<{width}}  {count:>12,}")
    if not any(table_counts.values()):
        typer.echo("\nEmpty — run `wikimill ingest` once v1.C ships.")


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
) -> None:
    """Run DNS + RDAP against due domains; the only place `unregistered` is set."""
    cfg = load_config()
    with RunLog(RunKind.CHECK, cfg.logs_dir) as log:
        gate(cfg, log)
        domain_stage.run(cfg, log, limit=limit, states=state, force=force)


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
) -> None:
    """Back-fill section, anchor text, and citation context for interesting links only."""
    raise NotImplementedYetError("enrich", "v1.H")


@app.command()
def inspect(
    target: Annotated[str, typer.Argument(help="A URL or a registrable domain.")],
    json_out: Annotated[bool, typer.Option("--json", help="Emit as JSON.")] = False,
) -> None:
    """Everything known about one URL or domain, including full check history."""
    raise NotImplementedYetError("inspect", "v1.I")


@app.command(name="export")
def export_cmd(
    state: Annotated[
        str | None, typer.Option("--state", help="Comma-separated states to export.")
    ] = None,
    min_pages: Annotated[
        int, typer.Option("--min-pages", help="Minimum citing Wikipedia pages.")
    ] = 1,
    fmt: Annotated[
        str, typer.Option("--format", help="csv or jsonl.")
    ] = "csv",
    out: Annotated[str | None, typer.Option("--out", help="Output path.")] = None,
) -> None:
    """Write a self-contained candidate file with full Wikipedia evidence."""
    raise NotImplementedYetError("export", "v1.I")


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

# wikimill

Local-first crawler that uses **Wikipedia as a trusted seed source** to find external
websites that have died, been abandoned, or become acquireable.

A link that survived Wikipedia's citation review is pre-vetted — someone judged that
site authoritative enough to cite in an encyclopedia. When such a domain goes dark it
is both a broken-citation problem and a high-quality acquisition candidate. wikimill
finds both, preserves the Wikipedia evidence, and exports a candidate file.

`docs/prd.md` is the canonical spec; `docs/architecture.md` is the HOW.

## Status

**Complete — v1, v2 and v3 shipped.** All eight pipeline stages, eleven commands,
607 hermetic tests. Soaked against the full 4.9 GB `externallinks` dump.

| | |
|---|---|
| Corpus | 27,152 Wikipedia pages → 1,433,427 links → 1,326,045 URLs → 135,591 domains |
| Found | **1,392 `unregistered` + 310 `expiring`**; 2,967 rows across all candidate states |
| Tuning | every threshold, weight and marker list is editable in `wikimill.toml` |
| Output | filterable offline HTML page, plus CSV/JSONL export |

**Known limits, stated rather than buried.** The corpus is one 27,152-page slice of
one dump run, so cross-run and live-wiki signals are conservative lower bounds rather
than full measurements. The HTTP crawl has run over ~0.04% of the URL queue — every
candidate above came from DNS + RDAP, which swept the whole corpus in 1.9 h, while a
full crawl is a ~25-day job bounded by politeness. `for_sale` and `parked` are
therefore near-zero because only the crawl can produce them. See `docs/soak-report.md`.

## Run (in Docker — crawlers always run in Docker)

```sh
cp wikimill.env.example wikimill.env    # then set WIKIMILL_CONTACT
./bin/wikimill preflight                # doctor: config, database, dumps
./bin/wikimill ingest                   # 1-2: dump -> links, normalized
./bin/wikimill crawl                    # 3-4: fetch + classify what is due
./bin/wikimill check                    # 5: DNS + RDAP; the only source of `unregistered`
./bin/wikimill enrich                   # 6: Wikipedia context, for candidates only
./bin/wikimill export                   # 7: candidates.csv, with full evidence
./bin/wikimill report                   # 8: outputs/report.html — filterable, offline
```

Watching a long run — the page reloads itself while a stage is live:

```sh
./bin/wikimill report --watch 10        # regenerate every 10s; shows stalls
./bin/wikimill stats --due              # what crawl/check would pick up now
./bin/wikimill stats --diff             # what editors added/removed between dumps
```

Also available: `namespaces`, `inspect`, `config show|validate`, and
`./bin/wikimill shell` for an interactive container.

Optional — run from any directory (always uses the latest source, no reinstall):

```sh
./bin/install
```

Dev/test path via the central builder (container `wikimill1`):

```sh
make buildsh && make deps && make test
```

## Configuration

All configuration is environment variables in `wikimill.env` (gitignored; copy from
`wikimill.env.example`). Precedence is **process environment > `wikimill.env` > default**,
so a one-off override needs no file edit:

```sh
WIKIMILL_DUMPS_DIR=/mnt/external/dumps ./bin/wikimill preflight
WIKIMILL_DRY_RUN=1 ./bin/wikimill preflight    # print the docker cmd, run nothing
```

`WIKIMILL_CONTACT` is **required** — the crawler presents a public identity, and
Wikimedia's User-Agent policy requires real contact information.

The three Wikimedia dumps total roughly 32 GB, so `WIKIMILL_DUMPS_DIR` may point at an
external SSD or HDD. The **database must stay on local disk** — SQLite WAL needs POSIX
locking and a durable `fsync`, and external/exFAT media risks corruption rather than
just errors.

## The pipeline

Ordered cheapest-first: the expensive step runs only on what turns out to be interesting.

```
externallinks SQL dump ─▶ normalize ─▶ crawl ─▶ classify
                                                   │
                        all live? ──▶ done, no further work
                                                   │
                            dead / parked / unregistered subset
                                                   │
                       DNS + RDAP ─▶ enrich (anchor text, section,
                                              citation context) ─▶ export
```

If a slice contains no dead links, the extra work is never done.

## Layout

```
bin/wikimill        host launcher (the only host-side code; bash, no Python)
src/wikimill/
  cli.py            Typer app — 8 flat commands
  config.py         env loading, precedence, secret redaction
  preflight.py      the mandatory gate
  storage/          SQLite schema + forward-only migrations
docs/prd.md         canonical spec
docs/architecture.md  mechanisms, schemas, invariants
state/              DB, logs, dumps (gitignored)
outputs/            exports (gitignored)
```

Read-only against every external source. Honours `robots.txt` unconditionally.

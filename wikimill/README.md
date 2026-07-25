# wikimill

Local-first crawler that uses **Wikipedia as a trusted seed source** to find external
websites that have died, been abandoned, or become acquireable.

A link that survived Wikipedia's citation review is pre-vetted — someone judged that
site authoritative enough to cite in an encyclopedia. When such a domain goes dark it
is both a broken-citation problem and a high-quality acquisition candidate. wikimill
finds both, preserves the Wikipedia evidence, and exports a candidate file.

`docs/prd.md` is the canonical spec; `docs/architecture.md` is the HOW.

## Status

**v1.B shipped** — scaffold, config, storage, preflight, launcher. `preflight` and
`stats` work today; the pipeline arrives from v1.C. See `docs/prd.md` §7 for the roadmap.

## Run (in Docker — crawlers always run in Docker)

```sh
cp wikimill.env.example wikimill.env    # then set WIKIMILL_CONTACT
./bin/wikimill preflight                # doctor: config, database, dumps
./bin/wikimill stats                    # row counts by table
./bin/wikimill shell                    # interactive container (debug)
```

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

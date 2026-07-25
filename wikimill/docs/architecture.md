# Architecture — wikimill

How this project is built. Mechanisms, schemas, modules, and integrations. The "HOW" companion to `docs/prd.md`'s "WHY / WHAT".

Status: **v1.B–v1.E shipped** (scaffold, config, storage, preflight, launcher, SQL ingest, normalization, crawler). Everything below marked *(vN.X)* is planned, not built.

## 1. Project layout

```
wikimill/
├── bin/wikimill               # host launcher — builds + `docker run --rm`s the CLI
├── bin/install                # PATH shim installer (~/.local/bin/wikimill)
├── Makefile / Makefile.local  # central-builder dev path (container: wikimill1)
├── Dockerfile / .dockerignore # bakes deps; source bind-mounted at runtime
├── pyproject.toml             # uv-managed, single lockfile
├── wikimill.env.example       # every variable documented — no secrets
├── main.py                    # root entry so the builder's `make run` works
├── src/wikimill/
│   ├── cli.py                 # Typer app — the 9 commands
│   ├── config.py              # env loading, precedence, redaction
│   ├── constants.py           # canonical enums/versions/defaults
│   ├── errors.py              # typed errors + exit-code contract
│   ├── logging.py             # ✓ ✗ ↷ markers + JSONL run log
│   ├── preflight.py           # the mandatory gate
│   ├── storage/
│   │   ├── db.py              # connection, WAL, migration runner
│   │   └── schema.py          # forward-only migrations
│   ├── ingest.py              # v1.C: stage 1 orchestration
│   ├── wiki/                  # v1.C: dump readers + URL reconstruction
│   │   ├── dump_sql.py        #   streaming MySQL INSERT-tuple scanner
│   │   ├── eldomain.py        #   el_to_domain_index -> real URL
│   │   └── msindex.py         #   multistream index (offset:page_id:title)
│   ├── normalize/             # v1.D: stage 2, runs inline inside ingest
│   │   ├── url.py             #   RFC 3986 + the §10 policy layer
│   │   ├── domain.py          #   PSL / registrable domain (tldextract)
│   │   └── archive.py         #   unwrap wayback / archive.today
│   ├── crawl/                 # v1.E: stage 3, the HTTP crawler
│   │   ├── guard.py           #   SSRF / address-space guards
│   │   ├── robots.py          #   robots.txt cache + RFC 9309 semantics
│   │   ├── politeness.py      #   backoff, per-host pacing, circuit breaker
│   │   ├── fetcher.py         #   one URL -> one url_checks row of evidence
│   │   └── runner.py          #   domain-partitioned workers, single writer
│   ├── classify/              # (v1.F) http.py · parked.py · soft404.py · state.py
│   ├── domain/                # (v1.G) dns.py · rdap.py
│   ├── enrich/                # (v1.H) select.py · seek.py · wikitext.py
│   └── export.py              # (v1.I) scoring + candidate file
├── tests/                     # 290 tests, hermetic (no network, no Docker)
├── state/                     # host-mounted, gitignored: DB, logs, dumps
└── outputs/                   # host-mounted, gitignored: exports
```

## 2. The pipeline

Eight stages, ordered **cheapest-first**. Every stage communicates only through SQLite, so any stage can be re-run without re-running its predecessor. Full contract in `prd.md` §8.

```
externallinks SQL dump ─▶ normalize ─▶ URL queue ─▶ HTTP crawl ─▶ classify
                                                                     │
                                       ┌── all live? ──▶ done. no further work.
                                       ▼
                          dead / parked / for-sale / unregistered subset
                                       │
                     domain checks (DNS + RDAP) ─▶ candidate set
                                       │
        ◀── ENRICH: seek those pages in the XML multistream dump ──▶
            section · anchor text · citation context
                                       │
                              score ─▶ export (candidate file)
```

**Why this order.** Link *context* is the expensive part and is only needed for links that turn out to be interesting. The SQL dump (~4.9 GB) yields the whole link set with no wikitext parsing; the XML dump (~26.6 GB) is touched only for the candidate subset, and only via random access. If a slice has no dead links, the expensive half never runs.

**Two structural rules hold it together:**

- **Enrichment consumes classification; it never precedes it.** Every stage before it works on URLs and page IDs alone.
- **Classification is a pure function over a stored check row.** Each `url_checks` row keeps bounded evidence plus the `classifier_version` that judged it, so an improved classifier re-classifies **offline** — no refetching, no extra load on third-party sites.

## 3. Random access into the article dump

The mechanism that makes lazy enrichment cheap.

`pages-articles-multistream.xml.bz2` is a concatenation of independently-decompressable bz2 streams, each holding ~100 pages. Its companion index (`…-multistream-index.txt.bz2`, ~283 MB) is `offset:page_id:page_title` per line.

So: look up the offset, seek, decompress **one small block**, parse one page. `wiki_pages.ms_offset` stores that offset at ingest time.

Two consequences:

- The index also supplies `page_id → title`, so **`page.sql.gz` (2.4 GB) is not needed at all**.
- `enrich` **sorts candidates by `ms_offset` and batches by block**, so one seek and one decompress serves every candidate sharing a stream. On an SSD this is a minor win; on a spinning external HDD — an expected deployment — it is the difference between minutes and hours.

## 4. Reading the externallinks dump

Everything here was validated against a real 4 MB slice of
`enwiki-20260701-externallinks.sql.gz` (170,426 rows, **99.98% reconstructed**),
not inferred. Four properties of the real file drive the implementation:

- **IP hosts are not reversed.** A `V4.`/`V6.` marker precedes them and the
  octets are in normal order: `http://V4.66.102.9.104.` is `66.102.9.104`.
  Reversing it would corrupt every IP host.
- **The port follows the trailing dot**: `http://uk.co.linearb.:8080` →
  `linearb.co.uk:8080`. It must be split *before* labels are reversed.
- **Statements are ~1 MB lines** of thousands of tuples, and values carry
  backslash-escaped quotes. Splitting on `'` corrupts data and a naive regex
  backtracks, so `dump_sql.py` is a hand-written character scanner.
- **Opaque schemes exist**: `mailto:`/`news:` are written `scheme:` with no
  `//`. They parse into an opaque, non-crawlable result and are *counted* —
  treating them as malformed would hide real parse failures behind noise.

**The dump is parsed, never executed** — it is a MySQL dump from a publicly
editable source, so feeding it to a database engine would execute
attacker-influenceable SQL.

**Namespace filtering — measured, not assumed.** `externallinks` has no
namespace column, so ingest intersects `el_from` with the index's page IDs.
Measured on the real `20260701` index (slice `p1p41242`): **99.27% articles** —
the article dump also carries `Wikipedia:`/`Portal:`/`Help:`/`Draft:` pages. So
intersection is a good proxy but not a clean filter. Ingest therefore defaults
to articles-only via known `Namespace:` prefixes (`--include-namespaces` opts
out), and `wikimill namespaces` reports the measurement. Only *known* prefixes
count: "Star Trek: First Contact" is an article. `page.sql.gz` remains unneeded.

## 5. Normalization

Stage 2, running inline inside `ingest` so `url_hash` is the normalized hash
from the moment a row exists. (A later rewrite pass would have to mutate a
UNIQUE key and merge the collisions it created.)

**The governing bias: a false merge is worse than a missed one**, because it
silently attributes one site's liveness to another. So path case, trailing
slashes, query order, encoded `%2F`, and every ambiguous parameter (`ref`, `id`,
`source`) are left alone; only unambiguous tracking identifiers are stripped.

- **Archives are unwrapped first** (`web.archive.org`, `archive.today` family),
  so nothing downstream can normalize a wrapper by mistake. The origin URL is
  queued, the wrapper kept as `archive_url`. Opaque archives (ghostarchive,
  webcitation) embed no origin and are recorded as-is rather than guessed at.
- **The PSL is never fetched at runtime** (`suffix_list_urls=()`). Ingest stays
  deterministic and a purely local stage makes no surprise network call;
  refreshing the list is a dependency bump, visible in review.
- **`NORMALIZER_VERSION` is stamped on every `urls` row.** Changing any rule
  changes every hash, and the version is what makes that detectable rather than
  a silent fork of identity.

**`is_private_suffix` is a fact, not a verdict.** It reports that a host sits
under a PSL private-section suffix. That section holds user-content platforms
(`blogspot.com`, `github.io`) whose subdomains are never acquireable *and*
regional/institutional registries (`poznan.pl`, `org.ru`, `ras.ru` — all seen in
real enwiki data) where one may well be. The PSL cannot separate them, so the
flag travels to scoring and the export instead of excluding. Hard exclusion is
reserved for the unambiguous: bare IPs, Wikimedia hosts, identifier resolvers.
An earlier version called this `is_user_content_suffix` and did exclude — which
would have silently dropped real finds (migration 2 renames it).

## 6. Storage

SQLite, single file, WAL, at `state/wikimill.db`. Ten tables (schema in `storage/schema.py`, documented in `prd.md` §9).

**Load-bearing invariants:**

- **`url_checks` and `domain_checks` are append-only.** No `UPDATE` may ever touch them — history is the product, and "live in July, NXDOMAIN in October" must stay a queryable fact.
- **The database never lives on removable or non-POSIX media.** WAL relies on POSIX locking and durable `fsync`; exFAT/NTFS/USB-detach break both, and the failure mode is a corrupted database, not an error message. Dumps may relocate (`WIKIMILL_DUMPS_DIR`); the DB may not. `preflight` warns when they share a mount.
- **Migrations are forward-only** and applied in a single transaction, so a failure leaves the previous version intact rather than a half-migrated database. A database from a *newer* build is refused rather than silently downgraded.
- **`urls.normalizer_version`** records which ruleset produced each `url_hash`. Changing a normalization rule changes the hash; without this column that would silently fork identity across the table.

## 7. Configuration

All configuration is environment variables, sourced from a mounted `wikimill.env`. Precedence: **process environment > `wikimill.env` > built-in default.**

Variables split across two layers, and the launcher handles both:

| Layer | Read by | Variables |
|---|---|---|
| **Launcher** (host bash, pre-Docker) | `bin/wikimill` sources the env file itself | `DOCKER_CMD` · `WIKIMILL_IMAGE` · `WIKIMILL_REBUILD` · `WIKIMILL_DRY_RUN` · `WIKIMILL_DUMPS_DIR` |
| **Application** (in-container) | `config.py`, via `--env-file` + `-e` passthrough | `WIKIMILL_CONTACT` · `WIKIMILL_USER_AGENT` · `WIKIMILL_DNS_RESOLVERS` · `WIKIMILL_CONCURRENCY` · `WIKIMILL_CRAWL_DELAY` · *future keys* |

`WIKIMILL_DUMPS_DIR` **must** be read on the host: it decides what gets bind-mounted, so it cannot be read from inside the container it configures. It is also deliberately *blanked* inside the container — the host path is meaningless there, since the mount always lands at the default location.

The launcher forwards every other host `WIKIMILL_*` variable with `-e` *after* `--env-file`, which is what preserves the precedence rule. (Omitting this was a real bug caught in v1.B soak: inline overrides silently stopped at the host.)

**Secrets.** None are needed at v1, but the whole path is built: gitignored `wikimill.env`, committed `.example` with no real values, and redaction of any variable matching `*_KEY|*_TOKEN|*_SECRET|*_PASSWORD` across `preflight`, `--json`, logs, and `crawl_runs.args`. Retrofitting this around a key that has already been committed once is far more expensive.

## 8. Preflight

A registry of small check functions, each returning a `CheckResult(marker, step, detail, remediation)`. Runs before every state-touching command and aborts on ✗ before any work, network request, or dump read.

Current checks: docker context · env file · **crawler identity** · state dir · database+migration · DB-not-on-dumps-mount · dumps presence · dump checksums. Later phases append theirs (robots reachability v1.E, RDAP v1.G) without touching the runner.

Two decisions worth knowing:

- **Identity is blocking (✗).** We do not touch anyone's server anonymously, and Wikimedia's User-Agent policy requires real contact info. A `(+CONTACT)` placeholder left unsubstituted counts as unset — shipping that string to a real server would be worse than failing here.
- **Missing dumps are ↷, not ✗.** v1.B must be runnable with no 32 GB on disk; each missing file names the phase that first needs it.

Checksums are cached as `(path, size, mtime, sha256)` and re-hashed only when size or mtime changes — verifying 32 GB over USB on every command would otherwise dominate runtime. `--verify-dumps` forces a full re-hash.

## 9. Output contract

- **Every step ends in exactly one marker**, including boring ones — the consistency is what makes a long log scannable. `✓` succeeded or already-correct · `↷` skipped/transient (retry helps) · `✗` permanent (operator must act).
- **Markers go to stderr**, so stdout stays clean for `--json` and file output.
- Colour is disabled for non-TTY and under `NO_COLOR`.
- **Progress prints as work happens**, flushed — a long stage is never silent.
- **`state/logs/<run_id>.jsonl`** carries one JSON object per event. A log directory that cannot be written degrades silently; it must never abort a run.
- **Every anticipated error names its fix.** `WikimillError` carries `remediation` and an exit code; a raw traceback reaching the operator is a bug.

**Exit codes:** `0` clean · `1` operator-actionable · `2` preflight failure · `130` interrupted.

Note: Typer's `no_args_is_help` is deliberately unused — Click implements it by exiting **2**, which would collide with the preflight-failure code. The root callback prints help and exits 0 instead.

## 10. Runtime

Two paths, one Dockerfile. Crawlers run inside Docker, never on the host; `bin/wikimill` and `bin/install` are the only host-side code, they are bash, and they execute no Python.

```sh
# user path
./bin/wikimill preflight
./bin/wikimill stats
./bin/wikimill shell                          # interactive container
WIKIMILL_DRY_RUN=1 ./bin/wikimill preflight   # print the docker cmd, run nothing
./bin/install                                 # optional PATH shim

# dev path (central builder, container wikimill1)
make buildsh && make deps && make test
```

Deps are baked into the image; **source is bind-mounted**, so code edits need no rebuild — and the PATH shim always runs the latest source with no reinstall. The launcher resolves its own real path (`readlink -f`) before deriving the repo root, which is what makes the symlink work.

**Two different dry-runs, deliberately distinguished.** `WIKIMILL_DRY_RUN` is *launcher-level*: print the docker invocation and every mount, start nothing — which is also what makes the launcher testable with no Docker present. `ingest --dry-run` / `enrich --dry-run` are *command-level*: the container starts and reports what work it would do.

## 11. Testing

290 tests, all hermetic — no network, no Docker, no real dumps. `pytest` runs inside the container (`make test`).

- `test_config.py` — precedence, identity, redaction, typed accessors
- `test_storage.py` — migrations, idempotency, WAL, append-only shape, uniqueness
- `test_preflight.py` — per-check markers, the gate, "every ✗ names a fix"
- `test_cli.py` — command surface, exit codes, stubs naming their phase
- `test_logging.py` — markers, JSONL, stderr/stdout split, colour suppression
- `test_crawl.py` — SSRF guards, robots.txt, fetcher, politeness (MockTransport + fake resolver)
- `test_normalize.py` — canonicalization, archive unwrapping, PSL, filtering
- `test_eldomain.py` / `test_dump_sql.py` / `test_ingest.py` — v1.C parsers + stage
- `test_launcher.py` — drives the bash launcher via `WIKIMILL_DRY_RUN` and the installer via `DRY_RUN`/`BIN_DIR`

**Suite-green is not feature-proven.** These verify code correctness. The acceptance criteria that matter (`prd.md` §19) need a real dump and a real crawl.

## 12. Tracked refactors

Logged as they arise, per house convention.

- **SSRF resolve-then-connect TOCTOU (v1.E).** `crawl/guard.py` resolves each hop
  and refuses blocked ranges, but httpx then resolves again independently, so a
  hostile DNS server could answer public to us and private to it. Closing this
  needs a transport that pins the connection to the address we validated.
  Deferred because wikimill sends no credentials and reads nothing into a trust
  boundary, so the residual exposure is a request being made rather than data
  disclosed — but it is a real gap, not a solved problem.
- **Provisional recheck cadence (v1.E).** Until v1.F classifies, a crawled URL is
  marked `unclassified` and rechecked on a flat 7-day cadence. The real, per-state
  cadences in prd.md §12 land with the classifier.

# Architecture — wikimill

How this project is built. Mechanisms, schemas, modules, and integrations. The "HOW" companion to `docs/prd.md`'s "WHY / WHAT".

Status: **v1 complete (v1.A–v1.J).** All eight pipeline stages and all nine CLI commands implemented, soaked against the full 4.9 GB dump — see `docs/soak-report.md`. v2 next. Everything below marked *(vN.X)* is planned, not built.

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
│   ├── cli.py                 # Typer app — 10 commands + `config`
│   ├── config.py              # env loading, precedence, redaction
│   ├── constants.py           # canonical enums/versions/defaults
│   ├── markers.py             # v2.C: marker word lists — a leaf, see below
│   ├── policy.py              # v2.B/C: wikimill.toml — the tuning surface
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
│   ├── classify/              # v1.F: stage 4, a pure function over evidence
│   │   ├── signals.py         #   marker matching (vocabularies in markers.py)
│   │   ├── rules.py           #   Observation -> Verdict
│   │   ├── state.py           #   state machine, cadences, terminality
│   │   └── runner.py          #   offline re-classification (no network)
│   ├── domain/                # v1.G: stage 5, DNS + RDAP
│   │   ├── dns.py             #   multi-resolver; NXDOMAIN needs corroboration
│   │   ├── rdap.py            #   IANA bootstrap (RFC 9224) + registry query
│   │   ├── rules.py           #   the unregistered gate
│   │   └── runner.py          #   selection, pacing, single writer
│   ├── enrich/                # v1.H: stage 6, the deferred expensive half
│   │   ├── select.py          #   what deserves it — and the empty fast path
│   │   ├── cache.py          #   v2.H: wikitext kept, so re-enrich is offline
│   │   ├── seek.py            #   offset -> one bz2 block -> pages
│   │   ├── wikitext.py        #   section, anchor, ref/cite context
│   │   └── runner.py          #   block batching, single writer
│   ├── diff.py                # v2.G: cross-dump-run link transitions
│   ├── verify.py              # v2.F: does the live wiki still link here?
│   ├── schedule.py            # v2.E: what's due, answered from the DB alone
│   ├── progress.py            # v3.B: heartbeat — alive? how far? stuck on what?
│   ├── report.py              # v3.B: the self-contained HTML page
│   ├── score.py               # v1.I: explainable ranking (never exclusion)
│   ├── inspect.py             # v1.I: everything known about one thing
│   └── export.py              # v1.I: deterministic, attributable candidate file
├── tests/                     # 606 tests, hermetic (no network, no Docker)
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
- **Classification is a pure function over a stored check row.** Each `url_checks` row keeps bounded evidence; the verdict lives in `url_classifications` with the `classifier_version` that produced it. So an improved classifier re-judges history **offline** — no refetching, no extra load on third-party sites — and the old verdicts stay on record for comparison (§6).

## 3. Random access into the article dump

The mechanism that makes lazy enrichment cheap.

`pages-articles-multistream.xml.bz2` is a concatenation of independently-decompressable bz2 streams, each holding ~100 pages. Its companion index (`…-multistream-index.txt.bz2`, ~283 MB) is `offset:page_id:page_title` per line.

So: look up the offset, seek, decompress **one small block**, parse one page. `wiki_pages.ms_offset` stores that offset at ingest time.

Two consequences:

- The index also supplies `page_id → title`, so **`page.sql.gz` (2.4 GB) is not needed at all**.
- `enrich` **sorts candidates by `ms_offset` and batches by block**, so one seek and one decompress serves every candidate sharing a stream. On an SSD this is a minor win; on a spinning external HDD — an expected deployment — it is the difference between minutes and hours.
- Measured on the real 298 MB `p1p41242` part: 5 candidates across 5 blocks →
  **500 pages decompressed in 1.4 s**, confirming ~100 pages per block.
- `BZ2Decompressor`, not `bz2.open`: it stops at its stream's end instead of
  running on into the following blocks, which is what makes a concatenated
  archive addressable at all.

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

## 6. Classification

Stage 4. A **pure function**: `Observation -> Verdict`, with no network, no
database and no clock. The observation is reconstructed either from a fresh
fetch or from a `url_checks` row read back months later, which is what makes
`crawl --reclassify` possible — on the real corpus it re-judged every stored
observation in 0.0s with zero requests.

**Verdicts live apart from observations.** `url_checks` is immutable (§20 forbids
UPDATE-ing it), so a verdict column there could never be revised. Verdicts go to
append-only `url_classifications`, keyed by `(check_id, classifier_version)`.
Re-classification appends, making a rule change auditable: you can see which
verdicts flipped and when.

**Rules are ordered by confidence, not convenience:** transport facts first (a
DNS failure is not a matter of opinion), then status codes, then content
heuristics — the only guesswork in the system. Every verdict records which
markers fired plus a confidence, so a wrong call is traceable rather than
arguable.

**`unregistered` is unreachable from this module by construction.** It needs two
resolvers agreeing plus RDAP (v1.G). A false "available domain" is the most
expensive error this tool can make, so no code path leads there from a fetch,
and a test asserts it across every HTTP shape.

**Marker restraint is the hard part.** Bare `"error"` was a soft-404 title marker
until its own test caught it matching *"Standard Error in Statistics"* — marking
live pages dead, the mistake an operator would act on. A marker now only earns
its place if it is implausible in a genuine page title; weak parking words never
fire alone; and an article *about* domain parking is not parked.

Two §11 rules enforced in `state.py` rather than left to callers: **`unregistered`
is never terminal** (it is the *most* urgent recheck — anyone can register the
domain tomorrow), and **`hard_404` does not kill the domain** (only domain checks
may set a domain state).

## 7. Domain checks

Stage 5, and the **only** place `unregistered` can be set.

**The gate is deliberately hard to pass:** ≥2 independent resolvers must return
NXDOMAIN *and* the authoritative registry must return an explicit 404. Either
alone is insufficient, and any disagreement errs toward "registered" — a missed
candidate costs nothing, a fabricated one costs real money. An RDAP server that
is rate-limited or unreachable yields `unavailable`, **never** `not_found`.

**RDAP coverage is reported, not worked around.** The IANA bootstrap (RFC 9224,
cached 7 days, longest-label match) maps 1,200 TLDs across 590 service groups —
but `.de`, `.es`, `.io`, `.ru` and `.edu` publish none. A domain there that
NXDOMAINs is `no_rdap_for_tld`: DNS says gone, nothing authoritative can confirm
it, and claiming availability would be a guess dressed as a fact.

Parking is **lifted from URL verdicts** rather than re-derived — the crawler
already saw the page — and needs a majority, so one parked URL cannot condemn a
healthy domain. `expiring` outranks it: a registration winding down is more
actionable than a parking page.

Verdicts live in append-only `domain_classifications`, mirroring
`url_classifications` (§6) for the same reason. WHOIS is never used and
registrar pages are never scraped.

## 8. Storage

SQLite, single file, WAL, at `state/wikimill.db`. Fifteen tables (schema in `storage/schema.py`, documented in `prd.md` §9).

**Load-bearing invariants:**

- **`url_checks` and `domain_checks` are append-only.** No `UPDATE` may ever touch them — history is the product, and "live in July, NXDOMAIN in October" must stay a queryable fact.
- **The database never lives on removable or non-POSIX media.** WAL relies on POSIX locking and durable `fsync`; exFAT/NTFS/USB-detach break both, and the failure mode is a corrupted database, not an error message. Dumps may relocate (`WIKIMILL_DUMPS_DIR`); the DB may not. `preflight` warns when they share a mount.
- **Migrations are forward-only** and applied in a single transaction, so a failure leaves the previous version intact rather than a half-migrated database. A database from a *newer* build is refused rather than silently downgraded.
- **`urls.normalizer_version`** records which ruleset produced each `url_hash`. Changing a normalization rule changes the hash; without this column that would silently fork identity across the table.

## 9. Configuration

All configuration is environment variables, sourced from a mounted `wikimill.env`. Precedence: **process environment > `wikimill.env` > built-in default.**

Variables split across two layers, and the launcher handles both:

| Layer | Read by | Variables |
|---|---|---|
| **Launcher** (host bash, pre-Docker) | `bin/wikimill` sources the env file itself | `DOCKER_CMD` · `WIKIMILL_IMAGE` · `WIKIMILL_REBUILD` · `WIKIMILL_DRY_RUN` · `WIKIMILL_DUMPS_DIR` |
| **Application** (in-container) | `config.py`, via `--env-file` + `-e` passthrough | `WIKIMILL_CONTACT` · `WIKIMILL_USER_AGENT` · `WIKIMILL_DNS_RESOLVERS` · `WIKIMILL_CONCURRENCY` · `WIKIMILL_CRAWL_DELAY` · *future keys* |

`WIKIMILL_DUMPS_DIR` **must** be read on the host: it decides what gets bind-mounted, so it cannot be read from inside the container it configures. It is also deliberately *blanked* inside the container — the host path is meaningless there, since the mount always lands at the default location.

The launcher forwards every other host `WIKIMILL_*` variable with `-e` *after* `--env-file`, which is what preserves the precedence rule. (Omitting this was a real bug caught in v1.B soak: inline overrides silently stopped at the host.)

**Secrets.** None are needed at v1, but the whole path is built: gitignored `wikimill.env`, committed `.example` with no real values, and redaction of any variable matching `*_KEY|*_TOKEN|*_SECRET|*_PASSWORD` across `preflight`, `--json`, logs, and `crawl_runs.args`. Retrofitting this around a key that has already been committed once is far more expensive.

### 3a. The enrichment page cache (v2.H)

`page_cache` keeps the wikitext of pages enrichment has already read. Its value is less about speed — a block decompresses in about a quarter-second — than about symmetry with classification: `crawl --reclassify` re-judges stored observations with no network, and extraction now has the same property. An improved `wikitext.py` rule re-applies to every past candidate with **no archive, no seek and no decompression**, and a fully-cached batch never opens the archive at all.

**Keyed on `(dump_run, page_id, lang)`, never on `ms_offset`.** Offset X in one run's archive is a different block from offset X in another's, and article text changes between runs — an offset-keyed cache would serve one revision's wikitext for a link recorded against another. That is what `check_dump_runs_agree` refuses at ingest; here it would be silent.

Derived and disposable: every row regenerates from the archive, so eviction is plain LRU against a byte budget and clearing costs time rather than information. Redirect stubs are never stored. `--no-cache` forces a read from the dump; `[enrich] cache_enabled` and `cache_max_bytes` are the knobs.

### 8b. Liveness — the heartbeat (v3.B)

**Commissioned as a pilot for the operator's other crawlers, so the mechanism is deliberately project-agnostic:** stage names are strings, counters are integers, and the only dependency is a SQLite connection and one table.

The problem it solves is that *a crawler being polite and a crawler being wedged produce identical output — nothing.* Politeness means one request per registrable domain plus a per-host delay, so long silences are normal and carry no information. The only thing separating "working" from "hung" is whether the process says so.

`run_progress` holds one upserted row per `(run_id, stage)`: `done`/`total`, the item being worked right now, `updated_at`, and `finished_at`. From that, three questions are answerable **from another terminal**:

| Question | Answered by |
|---|---|
| Is it alive? | `updated_at` is moving |
| How far along? | `done`/`total`, with rate and ETA from real elapsed time, not a guess made at the start |
| What is it stuck on? | `current_item` — the URL or domain being worked when the heartbeat stopped |

That third field is the one that matters in practice. It is the difference between "the crawler hung" and "the crawler is waiting on a DNS timeout for foo.example".

**This is the one table in the schema that is deliberately not append-only.** Everything else keeps history because history is the product. This answers a question about *now*; one current row beats scanning ten thousand stale ones, and it is derived state that costs nothing to lose.

Four rules keep it honest:

- **Throttled** — writing on every item turns an I/O-bound loop into a database-bound one. Rate-limited by wall clock, with the final write forced so the last state is never lost.
- **Never fatal** — every write is best-effort. The crawl matters; its bookkeeping does not. A test drops the table mid-run and asserts the run survives.
- **A crash leaves a row saying it crashed**, not a row that went quiet and must be diagnosed by silence.
- **A finished stage is never stalled, however old** — otherwise every completed run eventually turns red and the operator learns to ignore the signal.

### 8c. The report page (v3.B)

`wikimill report` writes one self-contained HTML file to `outputs/`. It carries every candidate with its evidence, filterable client-side (text, state toggles, sortable columns), plus the corpus funnel, the state distribution, and the live stage view above.

Hard constraints, all deliberate: **no network of any kind** — no CDN, webfont, analytics or external image, so it opens with the wifi off and still works when today's CDN is gone; everything inline, so there are no sidecar assets to lose; the data is deterministic so two reports diff meaningfully; and the CC BY-SA notice plus per-row article links travel with it, because anchor text and section names are Wikipedia excerpts here exactly as in the CSV (§17). Measured: 2,967 candidates, 1.8 MB, external references limited to hyperlinks to Wikipedia and the licence.

`--watch N` regenerates on an interval and prints a line per cycle, so the terminal and the browser both show movement. The file is written whole then moved into place, so a browser mid-refresh never reads a half-written page. The page auto-refreshes **only while a stage is running** — one that reloads forever fights the person trying to read it.

Its visual language is this project's own `✓ ✗ ↷` markers, which the operator already reads fluently from the terminal, carrying the same meanings as in `logging.py` so nothing new has to be learned.

### 8a. Cross-dump-run diff (v2.G)

`link_diffs` records what changed between two ingested runs: `removed` and `added`, keyed on `(url_hash, page_id, lang, from_run, to_run, transition)` so recomputing a pair is a no-op. Append-only, like every other observation table.

**A removal is corroboration, never a verdict.** Editors drop citations for reasons that correlate strongly with a dead site — but also when a paragraph is rewritten or a source upgraded. So a removal adds points to a domain's score and appears in the export as `wiki_removed`; nothing here writes a URL or domain state.

**Only pages present in both runs are compared, and this is the important part.** wikimill ingests slices, so a page missing from the newer run may have been deleted or may never have been ingested — indistinguishable from inside the database, and opposite in meaning. Comparing them anyway would fabricate one confident false positive per link on every un-ingested page. The comparison is therefore scoped to the intersection, and the remainder is reported as a *not comparable* count. There is deliberately no `page_deleted` transition; the schema comment says so, so nobody adds one later thinking it was an oversight.

The diff runs at the end of `ingest` rather than behind its own verb: the moment a second run lands is when the comparison becomes possible and when the operator wants it, and it costs two indexed queries on top of work already done. `stats --diff` displays stored results.

### 9a. Policy — `wikimill.toml` (v2.B/v2.C)

The env layer above is credentials-and-environment. **Policy** — what the tool looks for and how it ranks it — lives in a separate `wikimill.toml`, because a secret does not belong in a checked-in config file. Full precedence: **CLI flag > environment > `wikimill.toml` > built-in default.**

`policy.py` holds seven typed sections (`scoring`, `export`, `enrich`, `check`, `classify`, `markers`, `crawl`) whose dataclass defaults *are* the shipped policy — so a fresh checkout with no toml at all behaves identically to one with the `.example` copied verbatim. An unknown key is a `ConfigError` naming the valid keys, never a silent no-op.

**How policy reaches the code.** Runners call `load(cfg.root)` once and pass the result down as an argument; the pure functions (`score_domain`, `classify`, `recheck_seconds`, the signal matchers) take `policy=None` and fall back to their module constants. Passing rather than importing keeps them pure — same inputs, same output — and lets a test hand one in without touching the filesystem.

**Why `markers.py` is a top-level leaf.** The marker vocabularies are read by two places: `classify/signals.py` matches with them, and `policy.py` uses them as its built-in defaults. They can live in neither. Importing `wikimill.classify` runs its `__init__`, which reaches `classify/runner.py`, which loads policy — a cycle. Putting the *data* one hop below both consumers breaks it without hiding an import inside a function. The domain-check defaults (`INTERESTING_URL_STATES`, `DOMAIN_RECHECK_DAYS`, `EXPIRY_WATCH_DAYS`, `RDAP_CONCURRENCY_PER_REGISTRY`, `HARD_404_CONFIRMATIONS`, `CIRCUIT_THRESHOLD`) moved into `constants.py` for the same reason.

**Automatic version stamping.** `effective_classifier_version` is `CLASSIFIER_VERSION` plus a 12-char digest of every section that affects a verdict — `1+4458ff725a4c`. Editing a weight or a marker list shifts it without anyone remembering to bump a constant; editing `[export] min_pages` or crawl pacing deliberately does not, or unrelated runs would look incomparable.

That string is **stored on every verdict row**, not just printed. It has to be: `url_classifications` is unique on `(check_id, classifier_version)`, so a re-classify under edited rules would otherwise be swallowed as a duplicate of the original call. For the same reason the reclassify staleness check is **equality, not `>=`** — two marker lists at the same `CLASSIFIER_VERSION` are different rules, and neither is newer than the other.

**Not configurable, by design.** The §18 safety invariants stay code: per-domain concurrency of 1, redirect/body/evidence caps, the two-resolver rule for `unregistered`, robots.txt obedience, and the export licence header. Configurable politeness is politeness someone eventually turns off. A test asserts none of them has acquired a key.

## 10. Preflight

A registry of small check functions, each returning a `CheckResult(marker, step, detail, remediation)`. Runs before every state-touching command and aborts on ✗ before any work, network request, or dump read.

Current checks: docker context · env file · **crawler identity** · state dir · database+migration · DB-not-on-dumps-mount · dumps presence · dump checksums. Later phases append theirs (robots reachability v1.E, RDAP v1.G) without touching the runner.

Two decisions worth knowing:

- **Identity is blocking (✗).** We do not touch anyone's server anonymously, and Wikimedia's User-Agent policy requires real contact info. A `(+CONTACT)` placeholder left unsubstituted counts as unset — shipping that string to a real server would be worse than failing here.
- **Missing dumps are ↷, not ✗.** v1.B must be runnable with no 32 GB on disk; each missing file names the phase that first needs it.

Checksums are cached as `(path, size, mtime, sha256)` and re-hashed only when size or mtime changes — verifying 32 GB over USB on every command would otherwise dominate runtime. `--verify-dumps` forces a full re-hash.

## 11. Output contract

- **Every step ends in exactly one marker**, including boring ones — the consistency is what makes a long log scannable. `✓` succeeded or already-correct · `↷` skipped/transient (retry helps) · `✗` permanent (operator must act).
- **Markers go to stderr**, so stdout stays clean for `--json` and file output.
- Colour is disabled for non-TTY and under `NO_COLOR`.
- **Progress prints as work happens**, flushed — a long stage is never silent.
- **`state/logs/<run_id>.jsonl`** carries one JSON object per event. A log directory that cannot be written degrades silently; it must never abort a run.
- **Every anticipated error names its fix.** `WikimillError` carries `remediation` and an exit code; a raw traceback reaching the operator is a bug.

**Exit codes:** `0` clean · `1` operator-actionable · `2` preflight failure · `130` interrupted.

Note: Typer's `no_args_is_help` is deliberately unused — Click implements it by exiting **2**, which would collide with the preflight-failure code. The root callback prints help and exits 0 instead.

## 12. Runtime

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

## 13. Testing

606 tests, all hermetic — no network, no Docker, no real dumps. `pytest` runs inside the container (`make test`).

- `test_config.py` — precedence, identity, redaction, typed accessors
- `test_storage.py` — migrations, idempotency, WAL, append-only shape, uniqueness
- `test_preflight.py` — per-check markers, the gate, "every ✗ names a fix"
- `test_cli.py` — command surface, exit codes, stubs naming their phase
- `test_logging.py` — markers, JSONL, stderr/stdout split, colour suppression
- `test_export.py` — scoring, determinism, attribution, inspect
- `test_enrich.py` — the empty fast path (criterion 12), block seeking, wikitext
- `test_domain.py` — resolver reconciliation, RDAP bootstrap, the unregistered gate
- `test_classify.py` — the eleven states, marker restraint, state machine
- `test_crawl.py` — SSRF guards, robots.txt, fetcher, politeness (MockTransport + fake resolver)
- `test_normalize.py` — canonicalization, archive unwrapping, PSL, filtering
- `test_eldomain.py` / `test_dump_sql.py` / `test_ingest.py` — v1.C parsers + stage
- `test_launcher.py` — drives the bash launcher via `WIKIMILL_DRY_RUN` and the installer via `DRY_RUN`/`BIN_DIR`

**Suite-green is not feature-proven.** These verify code correctness. The acceptance criteria that matter (`prd.md` §19) need a real dump and a real crawl.

## 14. Tracked refactors

Logged as they arise, per house convention.

- **SSRF resolve-then-connect TOCTOU (v1.E).** `crawl/guard.py` resolves each hop
  and refuses blocked ranges, but httpx then resolves again independently, so a
  hostile DNS server could answer public to us and private to it. Closing this
  needs a transport that pins the connection to the address we validated.
  Deferred because wikimill sends no credentials and reads nothing into a trust
  boundary, so the residual exposure is a request being made rather than data
  disclosed — but it is a real gap, not a solved problem.
- **Parking-signature drift (v1.F).** `markers.py` is heuristics over
  attacker-influenceable text, and parking providers change their templates.
  Since v2.C the lists are `[markers]` in `wikimill.toml`, so tightening them is
  an operator edit plus `crawl --reclassify` — no code change, no rebuild, no
  refetching, and the version stamp shifts on its own. What remains tracked is
  that nothing *prompts* that review: the lists still need checking against real
  misses, and the tool cannot yet tell the operator when one has drifted.

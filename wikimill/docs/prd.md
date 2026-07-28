# PRD — wikimill

The canonical source of truth for purpose, scope, phases, and conformance. Code that contradicts this doc is drift, not feature.

> **Status: COMPLETE 2026-07-27.** Approved 2026-07-25; `v1.A` (planning) is this document.
> **v1** (`v1.A`–`v1.J`), **v2** (`v2.A`–`v2.I`) and **v3** (`v3.A`–`v3.B`) all shipped —
> eight pipeline stages, eleven commands, 607 hermetic tests. Four planned v3 phases were
> **dropped rather than delivered** once the operator stated the real requirement; that is
> recorded in the v3 section as an outcome, not hidden as a gap.
>
> Measurements and the operator's verdict on the finds are in `docs/soak-report.md`. The
> honest limits of what has been measured are restated in §7 under v3 and in the README —
> chiefly that the corpus is one slice of one dump run, and that the HTTP crawl has covered
> ~0.04% of its queue, so every candidate found so far came from DNS + RDAP.

## 1. Purpose

**wikimill** is a local-first, CLI-first crawler that uses **Wikipedia as a trusted seed source** for finding external websites worth knowing about — and, specifically, for finding the ones that have **died, been abandoned, or become acquireable**.

A link that survived Wikipedia's citation review is a *pre-vetted* signal: someone judged that site authoritative enough to cite in an encyclopedia. When such a domain goes dark, it is simultaneously (a) a broken-citation problem and (b) a high-quality acquisition candidate. wikimill finds both and exports the high-value tail as a candidate file.

**The pipeline is deliberately ordered cheapest-first.** Link *context* (anchor text, section, citation) is expensive to extract and is only ever needed for links that turn out to be interesting. So context extraction is **lazy** — it runs last, on the dead/parked/acquireable subset only:

```
externallinks SQL dump ─▶ normalize ─▶ URL queue ─▶ HTTP crawl ─▶ classify
                                                                     │
                                       ┌── all live? ──▶ done. no further work. ──┐
                                       ▼                                          │
                          dead / parked / for-sale / unregistered subset           │
                                       │                                          │
                     domain checks (DNS + RDAP) ─▶ candidate set                   │
                                       │                                          │
        ◀── ENRICH: seek those pages in the XML multistream dump ──▶               │
            section · anchor text · citation context                               │
                                       │                                          │
                              score ─▶ export (candidate file)  ◀────────────────┘
```

If a slice contains no dead links, **the extra work is never done.** Enrichment cost is proportional to findings, not to corpus size.

**Internal tool, single operator.** Not a SaaS, not a product, not a search engine.

## 2. Non-goals

**This section is load-bearing.** Update it whenever a tier gets dropped — capture the reason so the next similar proposal gets dropped at scoping, not after implementation.

- **No eager context extraction.** Anchor text / section / citation context is **never** extracted for the whole corpus — only on demand, for links already known to be interesting. Parsing every article's wikitext to harvest context we will discard for the ~majority of links that are alive is precisely the work this design exists to avoid. (Conformance rule.)
- **No web dashboard / TUI / API in v1.** CLI only. A surface is a later tier and only on operator-felt friction.
- **No distributed crawler.** Single process, single machine, bounded concurrency. No Redis / Celery / K8s / Spark.
- **No browser automation.** Pure HTTP (`httpx`). No Playwright, no headless Chrome. Rendering hostile third-party pages is both a security surface (§18) and a scale tax we have no measured need for. If a *specific, named* classification case genuinely requires JS, it gets its own ADR and its own opt-in flag — never a default.
- **No crawling of Wikipedia itself.** Wikipedia content comes from **dumps**, not from crawling `en.wikipedia.org`. The live API is spot-check only (§6).
- **No full-Wikipedia ingest in v1, and no crawling of every linked site in v1.** v1 proves the pipeline on a bounded, deterministic page-ID slice. Scale is v3, after v1's numbers are measured.
- **No generic search-engine infrastructure.** No full-text index, no page-content archive, no link graph beyond what expired-domain discovery needs. We store bounded classification *evidence*, not pages.
- **No AI / LLM processing in v1.** Extraction and classification are deterministic and auditable. An LLM stage may be introduced later as an **optional, opt-in classifier for the ambiguous residue only** (e.g. soft-404 vs. thin-but-live), never in the hot path, never as the sole basis for a classification. Requires its own ADR + measured evidence that the deterministic classifier plateaued.
- **No write-back of any kind against any external source.** Read-only: `GET`/`HEAD` only against crawl targets, downloads only against dumps mirrors. No POST, no auth, no form submission, no paywall circumvention. (Conformance rule.)
- **No domain acquisition, bidding, or registrar automation.** wikimill *finds and reports*. Acquisition is out of scope and is the operator's manual decision.
- **No WHOIS scraping of registrar web pages.** RDAP is the standardized interface; if a TLD has no RDAP, the status is honestly `unknown` (§11). Never scrape a registrar's HTML to guess.
- **No fabricated or inferred data.** An unmeasured field is blank or `unknown`, never estimated. Expiry dates, registrars, and availability come from RDAP or nowhere. (Conformance rule.)

## 3. Goals

Concrete, observable, prioritized:

1. `wikimill ingest` turns a bounded slice of the **`externallinks` SQL dump** into a deduplicated URL + domain queue — destination URL, destination domain, and every citing Wikipedia page — **without parsing a single byte of wikitext**.
2. `wikimill crawl` + `check` classify each URL and domain into the **eleven-state vocabulary** of §11, distinguishing a dead page from a dead domain from a parked domain from an unregistered one.
3. `wikimill enrich` back-fills **section, anchor text, and citation context** for a *selected subset only*, by seeking directly into the XML multistream dump. On a slice with no dead links, it does nothing and says so.
4. **Crawl history is append-only.** Every observation is a new row; no prior observation is ever overwritten. "This domain was live in July and NXDOMAIN in October" is a queryable fact, not a lost one.
5. **Nothing is reprocessed unnecessarily.** A classified record is not re-fetched until its recheck window opens (§12); terminal records are not re-fetched at all without `--force`; an enriched link is never re-enriched from the same dump run.
6. `wikimill export` produces a **self-contained candidate file** — CSV or JSONL, each row carrying its full Wikipedia evidence chain, readable by a spreadsheet or any downstream consumer.
7. Operator-grade run: idempotent re-runs, `✓ ✗ ↷` markers with transient/permanent colour coding, live per-step output, a final summary.
8. **Proof gate:** at least one *verified* unregistered-or-acquireable domain, cited by ≥1 English Wikipedia article, exported with its complete citation evidence.

## 4. Target user

The **operator**, running a local CLI. One user, one machine, no auth, no tenancy.

Two hats, same person:

- *Domain hunter* — wants a short, trustworthy list of acquireable domains that already sit in a credible citation neighbourhood, with enough evidence attached to judge each one without re-researching it.
- *Link archaeologist* — wants to know which Wikipedia citations are rotten, and what they used to point at.

Success from their seat: run three commands, get a CSV of a few dozen candidates where the top ones are genuinely worth looking at, and never wonder whether a number in it was guessed.

## 5. Primary workflows

**W1 — Seed the queue (cheap).** `preflight` → `ingest --dump <externallinks.sql.gz> --pages p1p41242` → `stats`. Streams the SQL dump, keeps the links whose source page falls in the slice, normalizes, dedupes, and seeds `urls` + `domains`. No wikitext, no context.

**W2 — Crawl the queue.** `crawl --limit N` walks pending URLs oldest-first, respecting robots.txt and per-host politeness, writes one `url_checks` row per attempt, and advances each URL's state. Interruptible and resumable — Ctrl-C loses at most the in-flight batch.

**W3 — Check domains.** `check --limit N` runs DNS + RDAP for domains whose URL-level evidence suggests they may be dead or acquireable, and classifies the domain itself. This is where `unregistered` is established — a fact no HTTP request can establish.

**W4 — Enrich only what matters.** `enrich --state unregistered,for_sale,parked,dns_failure` resolves the citing page IDs for that subset, seeks each one directly in the XML multistream dump, parses only those pages' wikitext, and fills in section / anchor text / citation context. **If the subset is empty, it exits `↷ nothing to enrich` and costs nothing.**

**W5 — Investigate one thing.** `inspect <url|domain>` prints everything known: current state, full check history, every Wikipedia page that cites it (with context if enriched), redirect chain, RDAP snapshot.

**W6 — Harvest candidates.** `export --min-pages 2 --state unregistered,parked,for_sale` writes a CSV/JSONL candidate file with evidence columns.

**W7 — Re-check over time.** Re-run `crawl` / `check` on a cadence the operator chooses; the scheduler (§12) picks only records whose recheck window has opened. Nothing is scheduled automatically — no daemon, no cron in v1.

## 6. Data-source decision

The load-bearing decision of this project. Two questions, answered separately because the lazy-context design decouples them:

**Q — Where does the *link set* come from?** (Needed for every link. Must be cheap.)
**Q — Where does the *link context* come from?** (Needed for a small subset. May be expensive per item, but must support random access.)

All figures **verified 2026-07-25** against the sources cited in §24.

| Option | Gives us | Costs / limits | Verdict |
|---|---|---|---|
| **SQL dump** — `enwiki-<date>-externallinks.sql.gz` (**~4.9 GB**) | The **post-expansion, authoritative link set** — exactly what MediaWiki itself recorded, including links generated by template/module expansion that never appear literally in wikitext. Columns: `el_id, el_from, el_to_domain_index, el_to_path`. Gives destination URL + citing `page_id`, which is all the queue needs. | No anchor text, no section, no citation context. `el_to_domain_index` is a *reversed* domain (`https://org.example.www.`) that must be un-reversed and rejoined with `el_to_path`. (`el_to` / `el_index` / `el_index_60` were removed in MW 1.41.) It is a **MySQL dump** — it must be *parsed*, never executed (§18). | ✅ **Primary — the link set** |
| **XML multistream + its index** — `pages-articles-multistream.xml.bz2` (~26.6 GB) **and** `pages-articles-multistream-index.txt.bz2` (**~283 MB**) | **Random access to any single page's wikitext.** The index is `offset:page_id:page_title`; each compressed stream holds ~100 pages. Seek to the offset, decompress one small block, get the page. Yields section headings, anchor text, `<ref>` / `{{cite …}}` context, `{{dead link}}` / `{{webarchive}}` tags. The index *also* supplies `page_id → title`, so **`page.sql.gz` (2.4 GB) is not needed**. | Large one-time download of the article dump; wikitext parsing (`mwparserfromhell`). But cost is **per enriched page**, not per corpus. | ✅ **Primary — the context, on demand** |
| **Wikimedia Action API** — `prop=extlinks`, `list=exturlusage`, `action=parse` | Live per-page data; `exturlusage` answers "who links to this domain?" directly. | Rate-limited: **10 req/min** unidentified, **200 req/min** with a compliant User-Agent; `eulimit` max 500/request. Useless for bulk. | ✅ **Spot-check / verification only** |
| **Wikimedia Enterprise** — Snapshot API, Structured Contents | Pre-parsed article structure; Structured Contents became free-tier on **2026-07-01**. On-demand fetch of single articles would be an alternative enrichment path. | Requires an account + auth. Free tier: **30 Snapshot requests/month**, **50,000 On-demand requests/month**. Adds a credential and an external dependency for something the local index already does offline. | ↷ **Deferred — re-evaluate at v4** |
| **Free Enterprise HTML dumps** — `dumps.wikimedia.org/other/enterprise_html/` | Would have been parsed HTML with rendered links. | **Dead.** Last run `20250320`; the mirror states it is *"no longer replicated here"* as of **24 March 2025**. (Regular SQL/XML dumps are current — `20260701` available at verification time.) | ✗ **Rejected — no longer published** |
| **Common Crawl Wikipedia WARC records** | Wikipedia HTML as crawled by CC. | Strictly worse for this purpose: partial, non-deterministic coverage; staler; needs CDX lookups + ranged WARC reads; *still* needs HTML parsing. Official Wikimedia data is simpler, complete, and free. | ✗ **Rejected for Wikipedia** |

### Recommendation

**SQL dump for the link set; XML multistream + index for lazy context.**

This ordering is better than the context-first alternative on every axis:

- **Cheaper by default** — 4.9 GB streamed once, versus 26.6 GB parsed in full. Most links are alive; their context is never needed and is never extracted.
- **Better recall, for free** — the SQL table is MediaWiki's own post-expansion record, so template-generated links (e.g. `{{Official website}}` pulling a URL from Wikidata) are included. A wikitext-first pipeline would silently miss them; this one cannot.
- **Work scales with findings** — enrichment touches ~one 100-page block per citing page in the candidate set, not the corpus.
- **Nothing is lost** — the multistream index makes the deferred context retrievable at any time, offline, without re-downloading anything.

**Namespace filtering.** `externallinks` has no namespace column. Rather than pull `page.sql.gz` (2.4 GB) just for it, intersect `el_from` with the page-ID set in the multistream index — which covers the article dump's pages. **This must be verified at `v1.C`**; if the intersection turns out not to be a clean article-namespace filter, `page.sql.gz` is the documented fallback. Not assumed — checked.

**v1 sample slice.** One contiguous **page-ID range** matching a single multistream part (e.g. `p1p41242`), applied as an `el_from` filter during SQL ingest. Rationale: a query- or category-based sample systematically biases toward well-maintained topics, which is exactly the opposite of where abandoned assets live. A contiguous page-ID range is unbiased, deterministic, reproducible, and keeps the SQL slice and the enrichment slice trivially aligned.

**Dump-run pinning.** The SQL dump and the XML dump **must be from the same run date**, and the run is recorded on every row. A page revised between two runs would otherwise yield context that does not match the link. `preflight` verifies the two agree.

## 7. Versions / phases

Strict two-level versioning only: `vN` tier, `vN.X` phase. Never three-level. **Phase `.A` of every tier is planning** for that tier; implementation starts at `.B`.

### v1 — Bounded pipeline proof

The whole path on one page-ID slice: SQL link set → normalize → crawl → classify → domain check → **lazy enrich** → export.

| Phase | Status | Title |
|---|---|---|
| v1.A | ✅ done | Plan: this PRD — cheapest-first ordering, data-source decision, data model, state machine, CLI |
| v1.B | ✅ done | Scaffold: `pyproject.toml` (uv), `Dockerfile`, `Makefile` + `Makefile.local` (container `wikimill1`), `bin/wikimill` launcher + `bin/install`, `wikimill.env` config/secrets loading + `.example` + gitignore, SQLite schema + migrations, `preflight`, logging/markers |
| v1.C | ✅ done | SQL ingest: streaming `INSERT`-tuple parser, reversed-domain un-mangling, page-ID slice filter, namespace-filter verification → `wiki_pages` (id+title from the index) + `external_links` (context columns null) |
| v1.D | ✅ done | Normalization + dedup (§10) → `urls` + `domains`; archive-URL unwrapping; scheme / internal-domain / resolver filtering |
| v1.E | ✅ done | Crawler: robots-aware, rate-limited, redirect-tracking `httpx` fetcher → append-only `url_checks` |
| v1.F | ✅ done | Classifier: the eleven-state vocabulary (§11) + URL state machine + bounded evidence capture |
| v1.G | ✅ done | Domain checks: multi-resolver DNS + RDAP → `domain_checks` + domain state; `unregistered` established here |
| v1.H | ✅ done | **`enrich`**: multistream index loader → **offset-sorted, block-batched** seek/decompress → `mwparserfromhell` → section, anchor, ref/cite context, dead-link tags, for the selected subset only |
| v1.I | ✅ done | `inspect`, `stats`, scoring, `export` (CSV + JSONL, evidence columns, licence header) |
| v1.J | ✅ done | First full bounded run + soak; measured in `docs/soak-report.md` |

#### Design notes

**v1.J** — ✅ shipped 2026-07-26. The soak. Full report in **`docs/soak-report.md`**; the load-bearing results:

**The full 4.9 GB dump was ingested for real: 61.8 minutes, 28 MB peak RSS**, ~190M rows scanned → 1,432,352 link occurrences, 1,326,045 URLs, 135,591 domains, 1.15 GB database. Twenty-eight megabytes to process 4.9 gigabytes is the streaming design validated completely.

**Two filters designed on reasoning were vindicated by measurement.** Archive unwrapping handled **428,101 links (~30%)** — without it nearly a third of the corpus would have measured the Internet Archive's uptime instead of the cited domain's. Identifier-resolver filtering removed **205,472 (~14%)**, exactly the queue domination §10 predicted. Together, 44% of raw links.

**Scale changed the answer to the most important question.** On the 4 MB sample, 89% of domains were cited by exactly one article, and citation count looked useless as a quality signal. On the full dump it is 71%, with **22,328 domains cited by 3+ articles and 6,110 by 10+**. `--min-pages` is a real lever at scale rather than a way to empty the output — so the earlier conclusion that availability swamps citation weight was drawn from a corpus where citation weight could not vary, and should be re-tuned after a crawl at this scale rather than against the same 440 URLs.

**The proof gate passed and the operator judged all three finds duds** — the most important result in the soak. `tetris-today.com` is trademarked; `radiopr740.com` is a niche mismatch. Both are genuinely available. **The tool was factually correct and useless on judgement**, which is a different failure from a miscalibrated weight and one no reweighting would have caught. Trademark screening and niche matching are recorded as known gaps, deliberately not built.

**One real defect found:** `count_pending` did not mirror `select` — it omitted the `wiki_pages` join and `ms_offset` guard, so an unreachable link was counted as pending, made `enrich` open the archive for nothing (defeating the criterion-12 fast path), and stayed pending forever. Fixed with regression tests. The soak harness was also wrong twice in ways worth recording, since both were false alarms that could have prompted bad fixes.

**Honestly unmeasured:** enrichment cost on HDD storage (only SSD available — the criterion-23 comparison is outstanding, not estimated), and any crawl at corpus scale (440 of 1,326,045 URLs is 0.03%).

**v1.I — ✅ shipped 2026-07-25. `score.py`, `export.py`, `inspect.py`, and the CLI wiring for `inspect` and `export`. **Every command in §15 is now implemented — no stubs remain**, and a test asserts that.

**Scoring ranks; it never excludes.** §10's filters decide what is out of scope; the score only orders what is left. A domain scoring zero still appears with its zero, because "we looked and it is uninteresting" is a different statement from "we never looked". Every component records its own contribution and a human-readable reason, so `inspect` can show *why* a domain ranked where it did and the operator can argue with the weighting rather than with an unexplained number. **The weights are policy defaults, not measured figures**, and are versioned so a change is detectable.

**The export is deterministic by construction** — fixed column order, fixed row order with a name tie-break rather than insertion order. The same filter over the same database yields a byte-identical file with a matching `sha256` (acceptance criterion 19). Two exports a week apart therefore `diff` meaningfully, which is the change report the roadmap would otherwise have needed a feature for.

**Attribution is structural, not a footnote.** Anchor text, section names and article titles are CC BY-SA excerpts, so every export carries the licence header and every row carries `example_article_url`. Whatever the operator later does with the file, the attribution is already in it (§17).

`inspect` is read-only and never re-checks, so it is safe to run against a database a crawl is writing to.

Two defects fixed while building this, both found by running the tool rather than by testing it:

1. **The crawl wrapped its entire run in one transaction**, contradicting §13's "checkpoint after each batch" — a crash mid-crawl would have lost all 400 URLs, and the write lock blocked any concurrent writer for the run's duration. Now commits every 25 results. Re-crawling is not cheap: it costs real requests to other people's servers.
2. **`RunLog.progress()` wrote to the terminal but not to the JSONL log**, so the durable record went silent during exactly the long stretch where it matters — the only way to distinguish a working crawl from a hung one without a debugger. Now written to both.

**Three more corrections came from running the whole pipeline on 570 crawled URLs and 742 checked domains — none would have been found by testing:**

3. **`autoRenewPeriod` was in `EXPIRING_STATUSES`.** It is the routine grace window *after* an automatic renewal — the opposite of expiring. It flagged `wildlifetrusts.org`, whose registration runs to **2031**. `pendingRestore` was removed for the same class of reason: a registrant actively reclaiming a domain is not a signal it is becoming available.
4. **"Expiry within 60 days" was not evidence of anything.** It flagged `ca.gov`, `gao.gov`, `osce.org` and `mindat.org` — institutions on ordinary annual renewal cycles. Roughly a sixth of all domains sit inside any 60-day window at any moment, so the date carries almost no predictive weight, and it swamped the export with 13 "candidates" of which zero were real. **`expiring` now means a registry lifecycle status only** (`pendingDelete`, `redemptionPeriod`, `clientHold`/`serverHold`); an approaching expiry shortens the *recheck window* instead, so a domain that does lapse is still caught. §11 is corrected.
5. **Enrichment triggered on URL state alone, so the best finds exported with no context.** Domain-level discovery outpaces URL-level classification: a domain is confirmed `unregistered` by DNS + RDAP even when its URLs were never crawled and remain `pending`. Both confirmed-available domains therefore exported with an empty section and anchor — the one thing that makes a candidate actionable. `enrich` now triggers on domain state as well (`DOMAIN_ENRICH_TRIGGER_STATES`).

**The proof gate (criterion 17) is met, and independently verified.** From the real corpus: **`tetris-today.com`** — cited by *Tetromino* under §External links as a `{{cite}}` citation anchored **"The Father of Tetris"** — and **`radiopr740.com`** — cited by *Telecommunications in Puerto Rico*, anchored **"Radio Puerto Rico"**. Both confirmed `unregistered` by two independent resolvers plus an RDAP 404, and re-verified outside wikimill with `dig` and `curl` (NXDOMAIN + HTTP 404). Also surfaced: `marygordon.org.uk`, `for_sale`, cited by *Electric boat* §Golden Age.

**The honest counterpoint:** before correction 4, the same corpus produced 13 candidates and *all* were noise. After it, six — of which three are real and three are `no_rdap_for_tld` (unverifiable, correctly scored low, one negative). That ratio is the argument for v1.J.

**v1.H — ✅ shipped 2026-07-25. The expensive stage, and the one the whole pipeline is ordered to defer: `enrich/select.py` (what deserves it), `enrich/seek.py` (multistream random access), `enrich/wikitext.py` (context extraction), `enrich/runner.py`.

**The empty-subset fast path was written first and tested first**, per the implementation sequence — it is the entire cheapest-first design made testable. `enrich` begins with a single indexed count; if nothing is pending it stops there, having opened neither the archive nor the index. A test asserts this by monkeypatching both `find_archive` and `read_block` to raise.

**Block batching, not per-page seeking.** Candidates are ordered by `ms_offset` and grouped, so one seek and one decompression serve every candidate page sharing a block. Measured on the real 298 MB `p1p41242` part: 5 dead links across 5 blocks → **500 pages decompressed in 1.4s**, confirming the ~100-pages-per-block assumption the design rests on.

`BZ2Decompressor` (not `bz2.open`) is what makes a concatenated archive addressable at all — it stops at its stream's end instead of running on into the next block. Offsets are operator-influenced and the archive is a public download, so decompression is bounded by both absolute size and expansion ratio; a wrong offset fails cleanly rather than consuming the machine.

Pages are extracted from a block by regex rather than an XML parser, because a block is a **fragment with no root element** — a conforming parser rejects it outright. Safe here only because the shape is machine-generated and rigid.

**Two honest outcomes are first-class, not failures:** `url_not_found_in_wikitext` (the link came from template expansion and has no literal occurrence — the link is real, only its surface context is absent) and `page_missing` (deleted or moved between dump runs). URL matching is deliberately loose on `www`/scheme/trailing slash, since the stored URL is normalized while the wikitext holds whatever an editor typed.

**Verified on real data.** Five dead links enriched from the real archive with their Wikipedia context: *Foreign relations of Cambodia* → a dead German Foreign Office page (anchor "Foreign relations between Cambodia and Germany"); *B'Elanna Torres* → a dead StarTrek.com bio; *Vladimir Markovnikov* → a dead university chemistry page in a `{{cite web}}` that Wikipedia has **already replaced with a web.archive.org snapshot**; *Tru64 UNIX* → a dead Google Groups link. Re-running enriches nothing. 412 hermetic tests.

**v1.G — ✅ shipped 2026-07-25. Domain checks: `domain/dns.py` (multi-resolver), `domain/rdap.py` (IANA bootstrap + registry query), `domain/rules.py` (the pure classifier), `domain/runner.py`. Mirrors v1.F's shape exactly — pure function, versioned verdicts, append-only `domain_classifications` (migration 4), for the same §20 reason.

**The `unregistered` gate is deliberately hard to pass:** ≥2 independent resolvers must return NXDOMAIN **and** the authoritative registry must return an explicit 404. Either alone is insufficient, and disagreement always errs toward "registered" — a missed candidate costs nothing, a fabricated one costs the operator real money and the trust of every other row. An RDAP server that is rate-limited, erroring, or unreachable yields `unavailable`, **never** `not_found`.

**RDAP coverage is a reported fact, not a workaround.** Measured against the live IANA bootstrap: 1,200 TLDs / 590 groups, with `.de`, `.es`, `.io`, `.ru` and `.edu` absent entirely. A domain there that NXDOMAINs is recorded `no_rdap_for_tld` — DNS says gone, nothing authoritative can confirm it, and claiming availability would be a guess dressed as a fact.

Two bugs found during the build, both of which would have failed silently:

1. **The bootstrap matched the shortest TLD suffix instead of the longest**, sending `bbc.co.uk` to the `.uk` registry rather than `.co.uk` — with plausible-looking answers. RFC 9224 requires longest-label match; fixed, and verified live (`bbc.co.uk` now reaches Nominet's `couk` endpoint).
2. **`confirmed_nxdomain` counted NXDOMAIN votes without checking for disagreement**, so a domain could be "confirmed gone" while another resolver was resolving it happily — the exact path to a fabricated available domain.

Parking is **lifted from URL verdicts rather than re-derived** (the crawler already saw the page), and requires a majority so one parked URL cannot condemn a healthy domain. `expiring` outranks it: a registration winding down is more actionable than a parking page.

**Verified on real data:** 12 domains — 11 `active`, 1 `expiring`, and 5 `.edu` honestly reported as unverifiable. The expiring find is real: **`georgehart.com`, expires 2026-08-15** (21 days out, registrar Tucows), cited by Wikipedia. 381 hermetic tests.

**v1.F — ✅ shipped 2026-07-25. The classifier: `classify/signals.py` (marker vocabularies), `classify/rules.py` (the pure function), `classify/state.py` (state machine + cadences), `classify/runner.py` (offline re-classification). Runs inline inside `crawl`, and re-runnable via `crawl --reclassify` — no new verb, since classify is a sub-step of crawl in the stage contract.

**A spec conflict this phase had to resolve.** §9 put `classification` on `url_checks`; §20 forbids ever `UPDATE`-ing that table; and re-judging stored evidence is the whole design. All three cannot hold. Resolved in favour of the invariant: verdicts moved to an append-only `url_classifications` table (migration 3), and the three verdict columns were dropped from `url_checks`. Re-classification now appends, so a rule change is auditable rather than destructive. §9 is corrected above.

**Rule ordering is by confidence, not convenience:** transport facts first (a DNS failure is not a matter of opinion), then status codes, then content heuristics — the only guesswork in the system. Every verdict records *which markers fired* and a confidence, so a wrong call is traceable to the rule that made it rather than argued about.

**`unregistered` is unreachable from this module by construction**, and a test asserts it across every HTTP shape. It requires two resolvers agreeing plus RDAP (v1.G); a false "available domain" is the most expensive error this tool can make.

**A false positive caught by its own test suite.** Bare `"error"` and `"oops"` were soft-404 title markers, which matched genuine titles like *"Standard Error in Statistics"* and *"Trial and Error"* — marking live pages dead, the mistake the operator would actually act on. Both removed; a marker now only earns its place if it is implausible in a real page title. Weak parking words (`"related searches"`) likewise never fire alone, and an article *about* domain parking is not classified as parked.

**Verified on real data:** 40 URLs crawled and classified — 14 `redirect`, 13 `blocked_by_robots`, 7 `live`, 5 `hard_404`, 1 `temporarily_unavailable` — with cadences landing exactly per §12 (90 days for live/redirect, 180 for robots-blocked, 30 for hard_404 pending its third confirmation, 1 hour for the transient). Re-classifying all 40 stored observations took **0.0s and zero network requests**, which is the payoff of keeping classification pure. Three real cross-domain handovers surfaced: `iht.com`→NYT, `strangersinparadise.com`→`abstractstudiocomics.com`, `bible.gospelcom.net`→`biblegateway.com`. 347 hermetic tests.

**v1.E — ✅ shipped 2026-07-25. The crawler: `crawl/guard.py` (SSRF), `crawl/robots.py` (RFC 9309), `crawl/politeness.py` (backoff, circuit breaker), `crawl/fetcher.py`, `crawl/runner.py`. First phase to touch the network, so §17 politeness and §18 security stop being theory. **It records evidence and does not classify** — v1.F re-judges these very rows offline, with no refetching.

Two structural properties, chosen so they cannot quietly rot: **work is partitioned by registrable domain** and each partition goes to exactly one worker, so per-domain concurrency of 1 is a property of the shape rather than a lock a later change could drop; and **only the main thread writes**, so Ctrl-C can never interrupt a half-written batch.

Three bugs found by running it for real, all fixed:

1. **A silent hang.** I passed the main thread's SQLite connection into workers for robots caching; connections are thread-bound, so every worker died instantly — and `ThreadPoolExecutor` swallowed the exceptions into futures I never checked, so the main thread blocked on `out.get()` forever with no output at all. The robots store is now a plain dict (main thread loads and persists it), every task emits exactly one result even on failure, and the collector polls with a timeout plus a liveness check that raises if workers died. A worker bug is now a loud error, never a hang.
2. **robots.txt truncated at 8 KB.** The robots fetch reused the evidence-blob cap, so a larger file lost its later rules and we could have fetched something disallowed — breaking a rule the PRD says is honoured unconditionally. `max_evidence` is now separate from `max_body`.
3. **`content_length` recorded the declared header** rather than the bytes observed. A server's claim can be wrong, and the classifier uses this to judge thin bodies, so it must match what was hashed and stored.

Also corrected: `classify_address` checked `is_private` before `is_link_local`/`is_unspecified`, so every diagnostic read "private range" and told the operator nothing.

**Verified against real sites** (identifying as `vik@lamill.us`): 15 URLs crawled across 15 domains, robots.txt fetched and cached per origin, 2 URLs `blocked_by_robots` and never fetched at all, and a real find already — `iht.com` returns 403 with title `nytimes.com` and `cross_domain_redirect=1`, the International Herald Tribune absorbed into NYT. Two hosts whose robots.txt was unreachable were treated as complete disallow per RFC 9309, which is the counter-intuitive rule that matters: a struggling server must not be hammered on the assumption that silence means consent. Re-running re-selects zero already-checked URLs. 290 hermetic tests.

**v1.D** — ✅ shipped 2026-07-25. Stage 2: `normalize/url.py` (RFC 3986 + the §10 policy layer), `normalize/domain.py` (PSL via `tldextract`), `normalize/archive.py` (archive unwrapping). Normalization runs **inline in `ingest`** rather than as a later rewrite pass, so `url_hash` is the normalized hash from the moment a row exists — a rewrite pass would have had to mutate a UNIQUE key and then merge the collisions it created. Existing databases are re-ingested, not migrated; that was the plan from v1.C.

The governing bias throughout: **a false merge is worse than a missed one**, because it silently attributes one site's liveness to another. So path case, trailing slashes, query order, encoded `%2F`, and every ambiguous parameter (`ref`, `id`, `source`) are left alone; only unambiguous tracking identifiers are stripped. `NORMALIZER_VERSION` is stamped on every `urls` row, because changing any rule changes every hash.

**The PSL is never fetched at runtime** (`suffix_list_urls=()`), keeping ingest deterministic and stopping a purely local stage from making a surprise network call. Refreshing the list is a dependency bump, visible in review.

**A design error caught by real data — `is_user_content_suffix` is now `is_private_suffix` (migration 2).** The PRD assumed the PSL's private section identifies user-content platforms (`blogspot.com`, `github.io`) whose subdomains are never acquireable, and hard-excluded them from candidacy. Real enwiki links showed it also flagging `wbc.poznan.pl`, `spb.org.ru`, `pdmi.ras.ru` — regional and institutional registries, some genuinely registrable. The PSL cannot separate the two. The column now says what it measures, and the flag is carried into scoring and the export rather than silently dropping candidates; hard exclusion is reserved for bare IPs, Wikimedia hosts, and identifier resolvers. §10 rule 3 is corrected accordingly.

**Verified on real data:** 1,075 link rows → 1,051 unique URLs → 808 domains, with `identifier_resolver` filtering firing and the counts (`cite_count`, `distinct_page_count`, `wiki_page_count`) recomputed from scratch each run so they cannot drift. Re-running inserts zero. 232 hermetic tests.

**v1.C** — ✅ shipped 2026-07-25. The ingest stage: `wiki/dump_sql.py` (streaming `INSERT`-tuple scanner), `wiki/eldomain.py` (URL reconstruction), `wiki/msindex.py` (multistream index), `ingest.py` (orchestration), plus `ingest` and `namespaces` on the CLI. Every parsing assumption was **validated against a real 4 MB slice of `enwiki-20260701-externallinks.sql.gz`** rather than inferred — 170,426 real rows, **99.98% reconstructed**; the residue is the literal string `http://...`, which a human typed into an article and which is correctly refused rather than guessed.

Four things real data taught us, each of which would otherwise have shipped as a silent corruption:

1. **IP hosts are not reversed.** They carry a `V4.`/`V6.` marker and appear in normal order — `http://V4.66.102.9.104.` is `66.102.9.104`, not `104.9.102.66`. 482 in the sample.
2. **Ports follow the trailing dot** — `http://uk.co.linearb.:8080` → `linearb.co.uk:8080`. 223 in the sample. Splitting the port after reversing produces nonsense.
3. **Values contain backslash-escaped quotes** (`/wiki/Stating_the_bleedin\'_obvious`), and statements are **~1 MB lines** holding thousands of tuples. Splitting on `'` corrupts data; a naive regex backtracks. Hence a hand-written character scanner.
4. **Opaque schemes exist.** `mailto:`/`news:` are written `scheme:` with no `//` (269 in the sample). They are a known non-crawlable category, so they parse into an opaque result and are *counted*; classing them as malformed would have inflated the error count and hidden real parse failures.

**Namespace filtering — hypothesis tested, and it failed.** §6 proposed intersecting `el_from` with the multistream index instead of pulling `page.sql.gz` (2.4 GB). Measured on the real `20260701` index for slice `p1p41242`: **99.27% articles** — the article dump also carries `Wikipedia:`, `Portal:`, `Help:`, and `Draft:` pages (201 of 27,353). So intersection is a good proxy but **not** a clean namespace filter. Resolution: ingest defaults to articles-only using known `Namespace:` prefixes, with `--include-namespaces` to opt out, and a `namespaces` command that reports the measurement. Only *known* prefixes count — "Star Trek: First Contact" is an article, and dropping colon-bearing titles would silently lose encyclopedic pages. `page.sql.gz` stays unneeded.

**Verified end to end on real data:** 27,152 article pages and 1,082 links ingested from the real dump; re-running inserts zero and reports `↷ already ingested`; context columns are all NULL (no wikitext read, the whole point of the ordering). 167 hermetic tests.

Two soak fixes to the v1.B launcher: it now runs as the invoking user (`-u`), because container-written `state/` was landing **root-owned on the host** and the operator could not delete their own database without sudo; and the dumps mount is now **read-only**, so an accidental write is a clear error rather than a corrupted 26 GB archive. The database open error also now distinguishes a permissions failure from an external-media failure — it previously sent the operator to the wrong remedy.

**v1.B** — ✅ shipped 2026-07-25. Scaffold plus the three cross-cutting mechanisms every later phase leans on. Central-builder `Makefile` (container `wikimill1`, not the shared `mb1`) + `Makefile.local`; `Dockerfile` bakes deps and bind-mounts source. **`bin/wikimill`** host launcher (bash, symlink-safe via `readlink -f`, `WIKIMILL_DRY_RUN`, `shell` subcommand) + **`bin/install`** PATH shim. **Config:** `wikimill.env` loading with `process env > file > default` precedence, name-based secret redaction, committed `.example` — built before any credential exists rather than retrofitted around one. **Storage:** the full ten-table schema in one forward-only migration, WAL, append-only check tables, `urls.normalizer_version`. **Preflight:** a check registry — blocking on crawler identity, ↷ on absent dumps (v1.B must run with no 32 GB on disk), dump checksums cached on `(size, mtime)`. **Output:** `✓ ✗ ↷` on stderr, JSONL run log, typed errors carrying remediations. 80 hermetic tests, green inside Docker.

Two decisions were forced during the build, recorded because they are easy to re-break:

1. **Typer's `no_args_is_help` exits 2**, which collides with our own exit-code contract (2 = preflight failure) — `wikimill || handle_preflight_failure` would misfire. The root callback prints help and exits 0 instead.
2. **The launcher must forward host `WIKIMILL_*` variables with `-e` *after* `--env-file`.** Without it the documented precedence silently fails for every application-layer variable — caught in soak, when an inline `WIKIMILL_CONTACT=…` override stopped at the host and never reached the container.

### v2 — Configuration, recheck & coverage

Adds the **policy configuration file** the CLI design always specified but v1 never built, then the recheck and coverage work. Config comes first in the tier because every later phase — and the v1.J soak before it — wants to tune thresholds without editing Python.

| Phase | Status | Title |
|---|---|---|
| v2.A | ✅ done | Plan the tier: `wikimill.toml` schema, precedence, and the policy-vs-code line |
| v2.B | ✅ done | **`wikimill.toml` policy config**: loader, validation with typed errors, `config show` / `config validate`, `.example` file |
| v2.C | ✅ done | **Move the policy constants into it**: export candidate states, `--min-pages` floor, scoring weights (§ `score.py`), enrichment trigger sets, recheck cadences, expiry watch window, per-host delay and concurrency |
| v2.D | ✅ done | **Operator-editable marker lists**: parking/for-sale/soft-404 signatures, tracking-parameter list, Wikimedia + resolver exclusion lists — with a `CLASSIFIER_VERSION` bump on change so verdicts stay auditable |
| v2.E | ✅ done | Recheck scheduler (§12): `next_check_at`, tiered cadences, `--due` selection, terminal-record protection |
| v2.F | ✅ done | `exturlusage` verification pass before export ("does enwiki still link here?") |
| v2.G | ✅ done | Cross-dump-run diff: links appearing/disappearing between runs — a link *removed* from Wikipedia is its own signal |
| v2.H | ✅ done | Enrichment cache: keep decompressed blocks warm across runs when re-enriching a new dump run |
| v2.I | ✅ done | **Parallel domain checks** — pulled forward ahead of `v2.A` because the v1.J tail sweep needed it |

**v2.A** — ✅ planned 2026-07-26, unblocked by the operator finding candidates worth pursuing. The tier's load-bearing decision is **where the line falls between policy and code**, and it is not arbitrary.

**Tunable — what the tool looks for:** scoring weights, candidate states, the citation floor, enrichment triggers, recheck cadences, marker vocabularies, concurrency and pacing. Every one is a judgement made without evidence in v1; the soak exists to challenge them, and until they are config, "tune it and re-run" is not something the operator can do.

**Not tunable — what protects other people, and what makes results auditable:** per-domain concurrency of 1 (a guarantee to every site we crawl, not a knob — *configurable politeness is politeness you will eventually turn off*), the redirect/body/evidence caps that are SSRF and resource-exhaustion defences, robots.txt obedience, the two-resolver rule for `unregistered`, and the version stamps and licence header that carry provenance and attribution obligations. A test asserts none of these appear as config keys.

**Precedence:** CLI flag > environment > `wikimill.toml` > built-in default. The split stays **`.env` for credentials and environment, `.toml` for policy** — a secret does not belong in a checked-in config file.

**v2.B** — ✅ shipped 2026-07-26. `policy.py`: seven typed sections, 31 tunables, `tomllib` (stdlib), and a `config show` / `config validate` command pair. A missing file is not an error — the defaults are the shipped policy and a fresh checkout must work without one.

**An unknown key is an error, never silently ignored.** A typo that is quietly dropped is worse than a crash: the operator believes they changed a threshold and the tool carries on with the old one. `min_page` instead of `min_pages` fails with the valid key list.

**Version stamping is automatic.** v2.D's requirement was "a `CLASSIFIER_VERSION` bump on change so verdicts stay auditable" — relying on someone remembering to bump a constant. Instead `effective_classifier_version` folds in a fingerprint of every value that affects a verdict (`1+b3bd73bee788`), so editing a weight or a marker list makes stored verdicts distinguishable from ones judged under the old rules, automatically. Non-classifying changes — crawl pacing, concurrency — deliberately do *not* shift it, or unrelated runs would look incomparable.

**v2.C / v2.D** — ✅ shipped 2026-07-26, together. v2.B built the loader; these two make it *load-bearing*. Every constant listed above now reaches its consumer as a `policy` argument — scoring weights, marker vocabularies, thin-body threshold, enrichment triggers, recheck cadences, the expiry watch window, the RDAP gate. v2.D collapsed into v2.C because the marker lists were already `[markers]` sections in v2.B's schema; what was missing was the same thing v2.C was missing, namely a consumer that reads them.

**Measured end-to-end on the real corpus**, not asserted: a two-line `wikimill.toml` (`[export] min_pages = 5`) took the export from **2,967 candidates to 43** across all 135,591 domains, with no rebuild and no code change.

**The tests that matter here assert outcomes, not fields.** `test_policy.py` proves the file parses; that is the weaker claim, and a config which loads perfectly and is then ignored by every consumer would pass all of it. `test_policy_effect.py` (16 tests) writes a real toml, runs the real consumer, and asserts the *result* differs — a weight edit that **reverses which of two domains ranks higher**, a new parking phrase that flips a verdict to `parked`, a removed one that flips it back, a narrowed trigger set that halves `count_pending`, and a CLI flag that still beats the file.

**Two defects this tier surfaced, both fixed:**

* **The fingerprint never reached the database.** `effective_classifier_version` was computed, printed, and then discarded — `record()` stored the bare `CLASSIFIER_VERSION` integer. Since `url_classifications` is unique on `(check_id, classifier_version)`, a re-classify under an edited marker list would have been silently swallowed as a duplicate, and the auditability this section claims would have been decorative. Verdict rows now carry the effective version, and the reclassify staleness check became **equality rather than `>=`**: two different marker lists at the same `CLASSIFIER_VERSION` are different rules, and neither is "newer".
* **A circular import.** `policy.py` read its defaults from the modules that consume it. The marker vocabularies moved to a leaf `markers.py` and the domain-check defaults to `constants.py` — data one hop below both, rather than an import hidden inside a function.

**Not made tunable, deliberately** — the safety invariants of §18 remain code: per-domain concurrency of 1, redirect and body-size caps, the two-resolver rule for `unregistered`, robots.txt obedience. `test_policy.py::test_safety_invariants_are_not_configurable` fails if any of them acquires a key.

**v2.E** — ✅ shipped 2026-07-26. Most of §12 already existed by v1.G: `next_check_at`, the cadence table, terminal-record protection, `--force`. What was missing were three things, and the first two were defects rather than absences.

**Ordering *is* the scheduler.** Selection was ordered by age alone. At 1,326,045 URLs every run is `--limit`-capped, so a due `for_sale` record queueing behind a hundred thousand due `live` ones is the difference between finding it this week and never finding it. Both queues now order by candidate value, then oldest — and the value comes from the operator's existing `[scoring]` weights rather than a second priority table, because a ranking that could disagree with the export order would be a trap. On the domain side this fixes a concrete inversion: `wiki_page_count DESC` alone let a heavily-cited `unknown` domain bury an `expiring` one, which is the single state whose whole point is that its window is closing. **18 domains were sitting in exactly that state on the real corpus.**

**`temporarily_unavailable` retried hourly, forever.** §12 has specified "1h, exponential ×2, cap 24h, then re-queue at 7 days" since v1.A; only the 1h was built. A host down for a week collected 168 requests from us on the theory it might return any minute — a politeness failure aimed precisely at the site least able to absorb it, and the failure mode is invisible from our side because nothing errors. **9 URLs were in that state on the real corpus.** Now implemented in full, ceilings tunable.

**The queue is now observable without running it.** `stats --due` reports both queues as five disjoint buckets — never checked / due now / due within 7d / due later / terminal — plus which states are due, because a bare count is not actionable: three due `for_sale` records justify a run that a hundred thousand due `live` ones do not. No new verb; `stats` gains a flag. It reads the database and makes no requests, so the cheapest question the operator has stops costing a crawl against real hosts.

Adding the two escalation ceilings to `[classify]` shifted the policy fingerprint (`1+4458ff725a4c` → `1+00652c9f0efd`), so the next `crawl --reclassify` will re-judge the 440 stored verdicts. That is the fingerprint working as designed — cadences live in a classifying section — and it is cheap here, but it is worth knowing the rule bites on scheduling changes too, not only on marker edits.

**v2.G** — ✅ shipped 2026-07-26. Wikipedia's editors are, in effect, a large and unusually careful dead-link detector, and the dumps record their output for free — no requests to anyone. Editors remove citations for reasons that correlate with what this tool hunts: the site died, the content vanished, the domain got parked, a bot swapped in an archive.

`link_diffs` (migration 5) is append-only like every other observation table, holding two transitions per run pair. Removal adds **+7** to a domain's score — more than the `{{dead link}}` tag's +5, because tagging is a note and removal is an act — and gets its own `wiki_removed` export column rather than only a line in `score_explanation`, since it is the one piece of evidence in the file that came from a human who looked at the page. It is corroboration and **never sets a state**; links also get dropped when a paragraph is rewritten or a source upgraded. A test asserts a removal cannot move a `live` URL or an `active` domain.

**The load-bearing decision was what *not* to compare.** wikimill ingests slices, so a page absent from the newer run may have been deleted or may simply never have been ingested — indistinguishable from inside the database, and opposite in meaning. Treating that as removal would manufacture one high-confidence false positive per link on every page the operator chose not to ingest, which is the most expensive error this project can make. So the comparison is scoped to the **intersection of the two runs' pages**, and everything outside it is reported as a *not comparable* coverage number. `page_deleted` is absent by design, not by omission, and five tests exist purely to prove a partial ingest cannot fabricate a signal.

No new verb: the diff runs inside `ingest` — the moment a second run lands is exactly when the comparison becomes possible and when the operator wants it — and `stats --diff` displays it. Both read tables only.

**Not yet validated against real data.** Only `20260701` is ingested, so `stats --diff` correctly reports "needs two ingested dump runs". The logic is covered by 26 hermetic tests and migration 5 applied cleanly to the real 135,591-domain database, but the *signal itself* — how often editors drop a citation, and how well that predicts a dead domain — is unmeasured until a second dump is ingested. That is the next real-data question this tier raises.

**v2.H** — ✅ shipped 2026-07-26, and the framing changed while building it. The phase was written as throughput — keep blocks warm, re-enrich faster. That win is real but modest: a block decompresses in about a quarter-second, and the cheapest-first ordering means most links never reach this stage.

**The larger win is that enrichment becomes re-runnable offline**, which is the property classification has had since v1.F. `crawl --reclassify` re-judges every stored observation with an improved classifier and refetches nothing (architecture.md §2). Extraction had no equivalent: improving `wikitext.py`'s section or citation-kind rules meant re-seeking a 26.6 GB archive — one that lives on an external drive which, as of this writing, **is not currently mounted**. With the wikitext of already-read pages kept, that improvement re-applies to every past candidate with no archive, no seek and no decompression, and if every candidate page is cached `enrich` never opens the archive at all. A test asserts exactly that, using an empty dumps directory so a stray archive read would raise.

**The key is `(dump_run, page_id, lang)` — never the byte offset.** Offset X in one run's archive is a different block from offset X in another's, and an article's text genuinely changes between runs. An offset-keyed cache would hand one revision's wikitext to a link recorded against another, which is what `check_dump_runs_agree` refuses at ingest, except silently and after the fact.

The cache is derived and disposable, so eviction is plain LRU against a byte budget (256 MB default, both knobs in `[enrich]`), `--no-cache` forces a read from the dump, and clearing it costs time rather than information. Redirect stubs are never stored — they carry no citation context and would masquerade as cached articles.

Two things fixed in passing: eviction deleted a whole 100-row batch regardless of the actual overshoot, which would have made the cache useless at any size near its cap; and `enrich --dry-run` required the archive to exist merely to report what it *would* read. Asking what a run would cost is what an operator does **before** deciding whether to go and plug the drive in, so refusing to answer because the drive is unplugged inverts the point. It now reports the plan either way.

**v2.F** — ✅ shipped 2026-07-26. The export's strongest claim is "cited by N distinct Wikipedia pages" — it is why a candidate is worth anything — and it rests on a dump that may be weeks old. This asks the live wiki whether those citations still exist.

**`export` stays offline and deterministic.** `--verify` runs the pass first, writes to `wiki_usage_checks`, and then the export proper collects from the database exactly as before. The digest still covers a pure function of stored rows. `--verify` is opt-in and never implied; a test asserts a plain export makes no request.

**Etiquette is structural, not configurable** — the same posture as per-domain crawl concurrency. A contact `User-Agent` (refusing to run without one), `maxlag=5` on every request, `Retry-After` obeyed, and **serial requests with no concurrency knob**. v2.I parallelised domain checks because registries are many independent operators; this is one operator's shared cluster, which asks bots to run series of requests sequentially. A test asserts the knob's absence so it is not added later as an oversight.

**The subtle correctness trap:** the Action API reports rate limits and replication lag with **HTTP 200 and an `error` object in the body**. A client checking only status codes records "0 articles link here" — a maximally confident false negative about the single strongest claim in the export. Failures are stored as errors with a NULL count, so "we could not ask" can never later read as "the answer was zero".

**Validated against the real API** (5 candidates, serial, 1 s apart), and the run immediately exposed something the design had assumed away.

**The two counts are not symmetric.** `dump_page_count` counts pages *in the ingested slice*; `live_page_count` counts articles *in all of enwiki*. Our corpus is a 27,152-page slice, so every verified domain came back far larger live than in the dump — `fed.us` 52 → 1,240+, `nrel.gov` 22 → 568. The direction of that asymmetry is what makes the signal safe rather than wrong: since `slice_then ≤ enwiki_then`, a result of `dump > live` proves `enwiki_then > enwiki_now`, and the reported loss `dump − live ≤ enwiki_then − live` is a **lower bound on the true loss**. It will miss removals; it cannot invent one. The converse carries no information at all, so `live > dump` is now reported as *cited beyond slice* — coverage, explicitly not good news about the source. Until the full-enwiki ingest of v3.D, only reductions are informative.

**Not double-counted with v2.G.** Both measure citations editors dropped — the dump-to-dump window sits inside the dump-to-now window — so scoring takes `max()` of the two, not the sum. A test pins it.

The export gains `wiki_pages_live` and `wiki_verified_at` beside `wiki_pages`, blank when unasked (an empty cell reads as "not checked"; a zero reads as "checked, nothing links here"). A truncated count is written as `N+` so a floor is never mistaken for a total.

**v2.I** — ✅ shipped 2026-07-26, out of tier order. Stage 5 was the last sequential stage, at 1.36 domains/sec, making a 112,349-domain tail sweep a 23-hour job. Now **9.46/sec measured on 300 real domains — 7× faster**, bringing the sweep to ~3.3 hours.

The crawler's parallelisation trick does **not** transfer. It partitions by the shared resource (one worker per registrable domain), which makes per-domain concurrency of 1 structural. The equivalent here would be partitioning by registry — but `.com` alone is **40.6%** of the unchecked tail and `.org` another 18.2%, so one partition would hold nearly half the work and cap the speedup at ~2.5× however many workers were added.

So the two lookups get different treatment, because they consume different resources: **DNS** runs at full worker concurrency (public resolvers are built for the volume), while **RDAP** is guarded by a **per-registry semaphore** — the registry is gated, not the worker, so Verisign sees at most 4 in flight while the long tail of 1,514 other suffixes proceeds freely. A test asserts that bound holds under 16 workers.

Two things deliberately unchanged: the **single writer** on the main thread (the v1.E bug where a SQLite connection reached worker threads is not one to repeat), and the **two-resolver agreement rule** for `unregistered` — parallelism must never become a reason to consult one resolver. Batch checkpointing every 50 results was added for the same reason as the crawler: re-running costs real requests to registries.

Also corrects a doc-vs-code drift: `domain/runner.py` had claimed since v1.G that "work is paced per RDAP registry". It never was, until now.

**Why config is a tier, not a chore.** Every threshold in v1 is a judgement call of mine, not a measured value: `unregistered` scoring +50, citations capped at +30, `is_private_suffix` at −15, the 60-day expiry watch, the parking marker lists. The v1.J soak exists to challenge those, and challenging them currently means editing Python and rebuilding. Until they are config, "tune it and re-run" is not a thing the operator can actually do.

**Resolves a documented drift.** §15 states config lives in `wikimill.toml`; v1 shipped env-var configuration only, and left policy hardcoded. v2.B/v2.C close that. Operational settings (`WIKIMILL_CONTACT`, dumps dir, resolvers) stay in `wikimill.env` — a secret does not belong in a checked-in toml — so the split is deliberate: **`.env` for credentials and environment, `.toml` for policy**.

### v3 — Reporting & scale

Measures what the pipeline's expensive stage is actually worth, then adds a **self-contained HTML report** — a local, offline file in the spirit of the pipeline page that made this project legible — then the scale work. The order is deliberate and was changed at `v3.A` on measured evidence; see below.

| Phase | Status | Title |
|---|---|---|
| v3.A | ✅ done | Plan the tier — then scope it down to the operator's actual requirement |
| v3.B | ✅ done | **Heartbeat + `report`**: long stages record liveness as they work; one self-contained, filterable HTML page shows the domains found *and* what is running now |
| ~~v3.C~~ | dropped | ~~Crawl yield measurement~~ — existed only to gate v3.F |
| ~~v3.D~~ | dropped | ~~Large bounded ingest~~ — the page renders what is already in the database |
| ~~v3.E~~ | dropped | ~~Storage decision (SQLite vs Postgres)~~ — not a problem this project has at 1.6 GB |
| ~~v3.F~~ | dropped | ~~Crawl throughput tuning~~ — fell with v3.C, which was its go/no-go |

**v3.A** — ✅ 2026-07-26/27. The tier was first planned as six phases: reporting, a crawl-yield measurement, a large ingest, a storage decision, throughput tuning. The operator then stated the actual requirement — *observability about domains found, through filters, on a web page* — and four of those phases were revealed as speculation rather than operator-felt friction. They were dropped rather than delivered.

What survived the cut, and why the rest did not:

- **Crawl yield measurement** existed only to decide whether throughput tuning was worth doing. Drop the tuning and its gate goes with it.
- **Storage (SQLite vs Postgres)** is not a problem at 1.6 GB. It becomes one if the corpus grows an order of magnitude; that is a reason to revisit it then, not to pre-decide it now.
- **A larger ingest** only matters if the operator wants *more* domains. The requirement was visibility into the ones already found.

This is the "don't manufacture work" rule catching a real instance of it. The measurements in the first draft of this plan were sound; the conclusion drawn from them — that they implied four more phases — was not.

**v3.B** — ✅ shipped 2026-07-27. Two requirements, one file.

**The page.** `wikimill report` writes one self-contained HTML file. Every candidate is embedded with its state, score, citation count, live count, removal count, registrar, expiry and an example Wikipedia citation deep-linked to its section. Filtering is client-side over the embedded rows — text search plus state toggles plus sortable columns — because the page is a file, not a service, and there is nothing to query. Measured on the real corpus: **2,967 candidates, 1.8 MB, and the only external references are hyperlinks to Wikipedia and to the CC BY-SA licence.** No script src, no link href, no font, no image. It opens with the network off.

**The liveness.** The second requirement arrived mid-build: *in long-running processes, I want to see what is going on, whether it is getting stuck.* A crawler being polite and a crawler being wedged produce identical output — nothing — so the only thing that separates them is whether the process says so. `run_progress` is one upserted row per (run, stage) carrying `done`/`total`, the item currently being worked, and `updated_at`. A row with no `finished_at` whose `updated_at` has stopped moving is a **stalled** stage, and that is detectable from another terminal without attaching a debugger.

`report --watch N` regenerates the page on an interval and prints one line per cycle, so the terminal and the browser both show movement. Demonstrated against a real 40-URL crawl:

```
↷ 16:32:25 crawl 52.5% 21/40  https://api.semanticscholar.org/CorpusID:144780750
```

Stage, percent, count, and the URL being fetched at that instant — which is the field that distinguishes "hung" from "waiting on a DNS timeout for that host".

**Design notes that are load-bearing rather than decorative:**

- **`run_progress` is the one table here that is deliberately not append-only.** Everything else keeps history because history is the product; this answers a question about *now*, and one current row beats scanning ten thousand stale ones. It is derived state and losing it costs nothing.
- **The heartbeat is throttled and never fatal.** Writing on every item turns an I/O-bound loop into a database-bound one, so writes are rate-limited by wall clock with the final write forced. Every write is best-effort: the crawl matters, its bookkeeping does not. A test drops the table mid-run and asserts the run survives.
- **A crash leaves a row saying it crashed**, rather than a row that merely went quiet and has to be diagnosed by its silence.
- **A finished stage is never stalled, however old.** Otherwise every completed run eventually turns red and the operator learns to ignore the signal.
- **The page only auto-refreshes while a stage is running.** A page that reloads forever fights the person trying to read it.
- **Written whole, then moved into place**, so a browser mid-refresh never reads a half-written file.
- **Truncation is stated, never silent.** Showing 3,000 of 50,000 rows without saying so reads as complete.

**Portability — this was commissioned as a pilot for other crawlers.** Nothing in `progress.py` is wikimill-specific: stage names are strings, counters are integers, and the only dependency is a SQLite connection and one table. The pattern is *heartbeat-with-current-item plus stall-by-staleness*, and it transfers to any pipeline with stages long enough that silence is ambiguous.

**Constraints already known.** The file must be self-contained and open offline from `outputs/` — no CDN, no webfonts, no analytics. It carries the same CC BY-SA header and per-row attribution as the CSV, because anchor text and section names are Wikipedia excerpts either way (§17). And it must be **deterministic in its data** like the CSV, so two reports diff meaningfully; the generated-at stamp stays outside the hashed content.

### v4 — Archive gaps

Turns the pipeline around: instead of asking *which dead domains are worth acquiring*, it asks **which dead citations can still be recovered, and which are already lost**. Same machinery, and the output is a contribution back rather than an extraction.

| Phase | Status | Title |
|---|---|---|
| v4.A | ✅ done | Plan the tier: selection funnel, Internet Archive etiquette, output grain, CLI shape |
| v4.B | ⏳ planned | **`gaps` command**: Wayback availability adapter, `archive_checks`, the recoverable/lost/unknown partition |
| v4.C | ⏳ planned | **`report --gaps`**: citation-grain HTML artifact in its own file |

**v4.A** — ✅ planned 2026-07-27. Two facts from the existing corpus set the shape, and both were measured rather than assumed.

**13.2% of citations already carry an archive URL** — 188,538 of 1,433,427. That is free information: normalization unwraps `web.archive.org/…` wrappers at ingest, so we already know which citations Wikipedia has archived without asking anyone.

**The actionable population needs no crawling.** A citation pointing at an `unregistered` domain is dead by definition, and there are **1,799 of them across 1,301 distinct articles** — 1,752 distinct URLs. At the pacing this tier will use that is a ~30-minute run, today, on data already in the database. The crawl dependency this tier appeared to have does not exist.

The funnel, in the project's existing cheapest-first grain:

```
citations to dead domains          1,799
  − those already carrying archive_url      (free, already known)
  → ask Wayback: does a snapshot exist?
        yes → RECOVERABLE   an edit fixes the citation
        no  → LOST          irrecoverable; report it as such
        err → UNKNOWN       we could not ask — never "no"
```

**The snapshot is requested for the dump run's date**, not for today. The version Wikipedia was citing is the one that matters; the closest snapshot to *now* may postdate the site's death or its sale to someone else.

**Errors are stored as errors with a NULL result.** Declaring a recoverable citation permanently lost is the one expensive mistake this stage can make, and it is exactly the trap v2.F found in the Wikimedia API — HTTP 200 with an error body, which a status-code-only client reads as "nothing found". Same discipline here.

**Etiquette matches the Wikimedia posture** (§v2.F): the Internet Archive is a nonprofit running this service for free, so a contact `User-Agent`, paced serial requests, `Retry-After` obeyed, and no concurrency knob.

**CLI shape, settled with the operator.** `gaps` is its own verb because it is its own stage — `archive` was rejected as a name, since it reads as an imperative and this command never writes to any archive. Rendering stays with `report`, which gains a flag writing a **separate** `outputs/archive-gaps.html`; there is no second reporting verb. The separate file is what lets the artifact be **citation-grain** — article + URL, because the actionable unit is "someone must edit this article" — without distorting the domain-grain `report.html`.

**Deferred:** a preventive pass over *live* citations, to archive at-risk ones before they die. Higher leverage but it means asking about URLs that are not yet dead, and the complete version is ~1.33M requests to a nonprofit. Scope it only once the reactive version proves useful.

**Scope boundary, stated so it is not assumed away.** This tier *finds* gaps. It never edits Wikipedia and never submits anything to any archive. Both are plausible next steps and both need their own decision: editing at scale requires WP:BOT approval, and pushing URLs to the Wayback save endpoint is a write against someone else's infrastructure, not a read.

### v5+ — candidate tiers (not committed)

Reactive; not scoped until earlier tiers soak.

- **Other language wikis / other Wikimedia projects** (dewiki, frwiki, Wikivoyage — Wikivoyage in particular is dense with small-business external links).
- **Wikimedia Enterprise** — re-evaluate free-tier Structured Contents as an alternative enrichment path once the local multistream path has soaked and its actual pain is known.
- **Optional LLM classifier** for the ambiguous residue only (soft-404 vs. thin-but-live, unseen parking templates). Opt-in, off the hot path, own ADR, only after the deterministic classifier demonstrably plateaus.
- **Watch mode** — a long-running recheck loop, or scheduled runs. Mostly a scheduler problem rather than an AI one; needs a run lock, since cron will fire again while a long crawl is still going.
- **LLM candidate judging** — the operator's own verdict on the first finds ("duds": trademarked, wrong niche) is a *judgement* problem no scoring weight fixes. Off the hot path, after export, stamped with model + prompt version the way `classifier_version` is, so verdicts stay attributable. Kept out of classification, which must stay reproducible.
- **Wikidata** — a different shape entirely: JSON entities, no wikitext, so enrichment has no analogue. But `P856` (official website) is a *typed, curated* claim, which is stronger evidence than a citation, and SPARQL reaches it without dump processing.
- **Archive-gap follow-through** — submitting at-risk URLs for archiving, or feeding recoverable ones to editors/IABot. Each is a write against someone else's system and needs its own decision (see v4).

## 8. Architecture

Six separated concerns, each independently testable, communicating **only through SQLite**. No in-memory pipe between stages — any stage can be re-run without re-running its predecessor.

```
  ┌─ WIKIPEDIA INGESTION (cheap, whole slice) ─────┐
  │  wiki/dump_sql.py    stream gz → INSERT tuples │
  │  wiki/eldomain.py    un-reverse el_to_domain_index + el_to_path
  │  wiki/msindex.py     multistream index → page_id → (offset, title)
  └───────────────────┬────────────────────────────┘
                      ▼  wiki_pages, external_links (context columns NULL)
  ┌─ NORMALIZATION ────────────────────────────────┐
  │  normalize/url.py     RFC-3986 canonicalization │
  │  normalize/domain.py  PSL → registrable domain  │
  │  normalize/archive.py unwrap web.archive.org/…  │
  └───────────────────┬────────────────────────────┘
                      ▼  urls, domains  (the queue)
  ┌─ URL CRAWLING ─────────┐   ┌─ DOMAIN CHECKS ────────┐
  │  crawl/fetcher.py      │   │  domain/dns.py         │
  │  crawl/robots.py       │   │  domain/rdap.py        │
  │  crawl/politeness.py   │   │                        │
  └──────────┬─────────────┘   └──────────┬─────────────┘
             ▼ url_checks                 ▼ domain_checks
  ┌─ CLASSIFICATION ───────────────────────────────┐
  │  classify/http.py  · classify/parked.py        │  versioned; re-runnable
  │  classify/soft404.py · classify/state.py       │  offline over stored evidence
  └───────────────────┬────────────────────────────┘
                      ▼  the interesting subset  ── empty? stop here ──▶ ✓ done
  ┌─ CONTEXT ENRICHMENT (expensive, subset only) ──┐
  │  enrich/select.py    which pages actually need it
  │  enrich/seek.py      index offset → bz2 block → page XML
  │  enrich/wikitext.py  mwparserfromhell → section/anchor/ref
  └───────────────────┬────────────────────────────┘
                      ▼  external_links context columns filled
  ┌─ EXPORT ─ score.py → export.py → outputs/*.csv|jsonl ┐
  └──────────────────────────────────────────────────────┘
```

**Two structural decisions carry the design:**

- **Enrichment is a consumer of classification, not a predecessor of it.** This is what makes the work proportional to findings. Every stage before it operates on URLs and page IDs alone.
- **Classification is a pure function over a stored check row.** Every `url_checks` row keeps its raw evidence plus the `classifier_version` that judged it, so an improved classifier **re-classifies offline** — no re-fetching, no extra load on third-party sites.

### Stage contract

Every stage declares what it reads, what it writes, and **what makes re-running it safe**. Idempotency is not a nice-to-have here: dumps are 32 GB, crawls take hours, drives get unplugged, and Ctrl-C is a normal way to end a run. Every stage must be safe to run again, always.

| # | Stage | Command | Reads | Writes | Idempotency key | Re-run does |
|---|---|---|---|---|---|---|
| 0 | acquire | *(out of band)* | dumps mirror | `state/dumps/` | file checksum | nothing — dumps are downloaded once, by hand |
| 1 | ingest | `ingest` | SQL dump + ms-index | `wiki_pages`, `external_links` | `(page_id, url_hash, dump_run)` | `↷ already ingested`, 0 new rows |
| 2 | normalize | *(within `ingest`)* | `external_links.url_raw` | `urls`, `domains` | `url_hash` PK / `registrable_domain` UNIQUE | recomputes identical hashes — pure function |
| 3 | crawl | `crawl` | due `urls` | `url_checks` (append) | `next_check_at > now()` | fetches nothing; `↷ none due` |
| 4 | classify | *(within `crawl`)* | a `url_checks` row | `classification` on that row | `(check_id, classifier_version)` | same verdict, byte for byte |
| 5 | check | `check` | due `domains` | `domain_checks` (append) | `next_check_at > now()` | fetches nothing; `↷ none due` |
| 6 | enrich | `enrich` | candidate subset + XML dump | `external_links` context cols | `(link_id, enrich_dump_run)` | re-parses nothing; `↷ already enriched` |
| 7 | score | *(within `export`)* | `domains`, `external_links` | `candidate_score` | pure function of current state | same scores |
| 8 | export | `export` | scored candidates | `outputs/*.csv\|jsonl`, `exports` | `(filter, db state)` → `sha256` | **byte-identical file** |

Three consequences worth naming, because they are what make the table true rather than aspirational:

- **Append-only ≠ non-idempotent.** `crawl` and `check` append a row per *attempt*, so "idempotent" here means *no duplicate work*, not *no new rows*. The recheck window (§12) is the guard; without it, a re-run would hammer every target again.
- **Export is deterministic.** Ordering is fixed (not `SELECT` insertion order), so the same filter over the same DB state produces a byte-identical file with a matching `sha256`. Two exports a week apart therefore `diff` meaningfully — that *is* the change report, with no separate feature needed.
- **Normalization is versioned.** `urls.normalizer_version` records which ruleset produced each `url_hash`. Changing a normalization rule changes the hash, which would silently fork identity across the table; the version column makes that detectable and makes a re-normalize migration possible. This is the one stage where "pure function" is only true *per version*.

Stages 2, 4, and 7 have no command of their own — they run inside the stage above them. They are listed because they are independently testable pure functions, which is what makes stages 1, 3, and 8 cheap to trust.

## 9. Data model

SQLite. Tables, with the fields that matter. (`✱` = indexed.)

**`wiki_pages`** — `page_id ✱ PK` · `lang` · `title ✱` · `ms_offset` (byte offset into the multistream archive, from the index) · `dump_run` · `ingested_at`
Populated from the multistream index at ingest — cheap, and it is what makes `enrich` a seek rather than a scan.

**`external_links`** — one row per link occurrence. **Context columns are `NULL` until `enrich` fills them.**
`id PK` · `page_id ✱ →wiki_pages` · `url_raw` · `url_hash ✱ →urls` · `dump_run` · `first_seen` · `last_seen`
— *enrichment-populated:* `section` (nearest preceding `==` heading; `"(lead)"` when none) · `section_level` · `anchor_text` · `link_kind` (`citation` | `external_links_section` | `infobox` | `inline` | `template` | `further_reading`) · `ref_name` · `template_name` (`cite web`, `cite news`, …) · `context_excerpt` (bounded, ~300 chars) · `dead_link_tagged` (bool — Wikipedia's own `{{dead link}}`) · `archive_url` · `archive_date` · `enriched_at` · `enrich_dump_run` · `enrich_status` (`pending` | `done` | `page_missing` | `url_not_found_in_wikitext`)
Unique: `(page_id, url_hash, dump_run)`.

`enrich_status = url_not_found_in_wikitext` is an expected, honest outcome, not an error: it means the link came from template expansion and has no literal wikitext occurrence. That is *information* — it is recorded, not papered over.

**`urls`** — the crawl queue, one row per normalized URL.
`url_hash ✱ PK` (SHA-256 hex of the normalized URL) · `url_normalized` · `normalizer_version` · `domain_id ✱ →domains` · `scheme` · `state ✱` (§11) · `terminal` (bool) · `first_seen` · `last_checked` · `next_check_at ✱` · `check_count` · `consecutive_failures` · `cite_count` · `distinct_page_count`

**`url_checks`** — **append-only.** One row per fetch attempt; nothing is ever updated.
`id PK` · `url_hash ✱` · `checked_at ✱` · `http_status` · `final_url` · `final_url_hash` · `redirect_chain` (JSON: per hop — url, status, resolved IP) · `redirect_count` · `cross_domain_redirect` (bool) · `content_type` · `content_length` · `page_title` · `body_sha256` · `evidence_blob` (bounded head of response) · `latency_ms` · `classification` · `classifier_version` · `classifier_reasons` (JSON) · `error_kind` · `error_detail` · `robots_decision` · `crawler_version`

**`domains`** — one row per **registrable domain** (PSL-derived).
`domain_id PK` · `registrable_domain ✱ UNIQUE` · `public_suffix` · `is_private_suffix` (bool — the host sits under a PSL *private-section* suffix; a **fact, not a verdict**, see §10) · `state ✱` (§11) · `terminal` · `first_seen` · `last_checked` · `next_check_at ✱` · `wiki_page_count` · `wiki_link_count` · `url_count` · `candidate_score` · `score_explanation` (JSON)

**`url_classifications`** — **append-only verdicts, separate from observations** (added v1.F, migration 3).
`id PK` · `check_id ✱ →url_checks` · `url_hash ✱` · `classified_at` · `classifier_version` · `classification ✱` · `reasons` (JSON — which markers fired) · `confidence`
Unique: `(check_id, classifier_version)`.

*Why a separate table:* §20 forbids ever `UPDATE`-ing `url_checks`, and re-judging stored evidence with an improved classifier is the point of the design — the two cannot both hold with a verdict column on the observation. Verdicts moved out, so `url_checks` stays a pure immutable record of what was *observed*, and re-classification appends. That also makes classifier disagreement auditable: you can see exactly which verdicts a rule change flipped, and when.

**`domain_classifications`** — **append-only verdicts** (added v1.G, migration 4).
`id PK` · `check_id ✱ →domain_checks` · `domain_id ✱` · `classified_at` · `classifier_version` · `state ✱` · `reasons` (JSON) · `confidence`

*Same resolution as `url_classifications`, for the same reason:* §20 forbids `UPDATE`-ing `domain_checks`, so a verdict column there could never be revised. The symmetry is not tidiness — it is what lets re-classification work uniformly across both halves of the pipeline.

**`domain_checks`** — **append-only.**
`id PK` · `domain_id ✱` · `checked_at ✱` · `dns_status` (`ok` | `nxdomain` | `servfail` | `timeout` | `no_records`) · `a_records` (JSON) · `ns_records` (JSON) · `resolvers_agreed` (bool) · `rdap_status` (`registered` | `not_found` | `unavailable` | `no_rdap_for_tld`) · `rdap_raw` (JSON, bounded) · `registrar` · `registration_expiry` · `domain_statuses` (JSON — EPP codes: `clientHold`, `pendingDelete`, `redemptionPeriod`, …) · `latency_ms` · `error_kind`

**`robots_cache`** — `origin PK` · `fetched_at` · `expires_at` · `http_status` · `body` · `crawl_delay`

**`crawl_runs`** — `run_id PK` · `kind` (`ingest`|`crawl`|`check`|`enrich`|`export`) · `started_at` · `ended_at` · `args` (JSON) · `counts` (JSON) · `config_hash` · `crawler_version` · `outcome`

**`exports`** — `export_id PK` · `created_at` · `filter` (JSON) · `row_count` · `path` · `sha256`

**Evidence-blob policy.** Store a bounded head of the response body (default **8 KB**, configurable, decoded to text) for every check whose classification is *not* `live` — exactly the cases where a classifier improvement would change the verdict. `live` pages store only `body_sha256` + title. This keeps offline re-classification possible without turning the DB into a page archive (an explicit non-goal, §2).

## 10. URL and domain normalization rules

**URL normalization** — RFC 3986 canonicalization, then a conservative policy layer. Output is `url_normalized`; `url_hash = sha256(url_normalized)` is the identity key.

1. Lowercase scheme and host. Leave path, query, and fragment case alone (paths are case-sensitive on most servers).
2. IDN host → **punycode** via UTS-46.
3. Drop default ports: `:80` on http, `:443` on https.
4. Resolve dot-segments (`/a/./b/../c` → `/a/c`).
5. Percent-encoding: uppercase hex digits; decode unreserved characters (`A-Za-z0-9-._~`); leave everything else encoded.
6. Empty path → `/`. Otherwise **preserve the trailing slash exactly as written** — `/foo` and `/foo/` are different resources to many servers.
7. **Strip the fragment** (`#…`) — never sent to the server, never affects liveness.
8. **Strip known tracking parameters** from a maintained list (`utm_*`, `fbclid`, `gclid`, `msclkid`, `mc_cid`, `mc_eid`, `_ga`, …). **Preserve all other parameters in their original order** — do not sort. Sorting is a common "normalization" that silently breaks order-sensitive servers and buys nothing here.
9. **Unwrap web archives.** `web.archive.org/web/<timestamp>/<url>`, `archive.today` / `archive.ph`, `ghostarchive.org` → extract the wrapped origin URL. Both are recorded: the origin URL is what gets **queued and crawled**; the wrapper is kept as `archive_url`. Crawling the wrapper measures the Internet Archive's health, not the cited domain's.
10. **Non-crawlable schemes** (`mailto:`, `ftp:`, `irc:`, `news:`, `magnet:`) are recorded as `external_links` rows but never enter the `urls` queue.
11. **Wikimedia-internal hosts** (`*.wikipedia.org`, `*.wikimedia.org`, `wikidata.org`, `*.wiktionary.org`, …) are filtered out — not "external" for this tool's purpose. Configurable list.
12. **Identifier resolvers** (`doi.org`, `hdl.handle.net`, `ncbi.nlm.nih.gov/pmc`, `worldcat.org`, …) are flagged `is_resolver` and **excluded from crawl and export by default** — permanently live infrastructure, never acquireable, and they would dominate the queue. Includable with an explicit flag.

**Reconstructing the URL from the SQL dump** — `el_to_domain_index` stores protocol + *reversed* host (`https://org.example.www.`). Un-reverse the dot-separated labels, drop the trailing dot, rejoin with `el_to_path`, then run the rules above. This is a documented gotcha with a dedicated unit-test fixture, because getting it subtly wrong corrupts every domain in the database.

**Domain normalization**

1. `registrable_domain` = eTLD+1 via the **Public Suffix List** (`tldextract`). The PSL is the only defensible definition of "a domain you could own" — naive `last-two-labels` splitting gets `co.uk` and `com.au` wrong.
2. The full host is stored on the URL; `www.` is **not** stripped for URL identity (it can 404 differently) but *is* irrelevant to domain identity, which uses eTLD+1 only.
3. Flag `is_private_suffix` when the host sits under a PSL **private-section** suffix — and treat it as a *signal*, not an exclusion. **Corrected at v1.D against real data:** this was originally specified as `is_user_content_suffix`, on the assumption that the private section means user-content platforms (`blogspot.com`, `github.io`) whose subdomains are never acquireable. Real enwiki links showed it also flagging `wbc.poznan.pl`, `spb.org.ru`, and `pdmi.ras.ru` — regional and institutional registries, some of which *are* registrable. The PSL cannot tell the two apart, so the flag is recorded, carried into scoring (v1.I), and shown in the export, rather than silently removing candidates. Hard exclusion is reserved for the unambiguous cases: bare IPs, Wikimedia hosts, and identifier resolvers.
4. Bare-IP hosts get no registrable domain; they are crawled but never scored as candidates.

## 11. Crawl-state lifecycle

Two state machines — URLs and domains — because *a dead page is not a dead domain*, and conflating them is the classic error in this problem space.

### URL states

```
                 ┌──────────────────────────────────────────┐
  new ─▶ pending ─▶ in_progress ─┬─▶ <classification>  ──────┴─▶ (recheck when due)
                                 ├─▶ blocked_by_robots
                                 └─▶ skipped  (non-crawlable scheme / resolver / out of scope)
```

`<classification>` is exactly one of the eleven required states:

| Classification | Established by | Terminal? | Triggers enrichment? |
|---|---|---|---|
| `live` | 2xx, real content, fails all parked/soft-404 heuristics | no — recheck | **no** |
| `redirect` | 3xx chain resolving to a final URL. `cross_domain_redirect` distinguishes an in-site move from a domain handover | no — recheck | only if cross-domain |
| `soft_404` | 2xx but the page says "not found": title/body markers, sub-threshold body length, redirect-to-root of a deep-path URL | no — recheck | yes |
| `hard_404` | 404 or 410 | after N confirmations | yes |
| `dns_failure` | Resolution failed. **Sub-kinds recorded, not lumped**: `nxdomain`, `servfail`, `timeout`, `no_records` | no — this is the lead | yes |
| `tls_failure` | Sub-kinds recorded: `cert_expired`, `hostname_mismatch`, `chain_untrusted`, `protocol_error`, `handshake_timeout` | no | yes |
| `parked` | Parking-provider signature: known parking nameservers, known parking-page markers, ad-only body | no — high-value, recheck fast | yes |
| `for_sale` | `parked` **plus** a sale signal (Afternic / Sedo / Dan / "this domain is for sale" markers) | no — highest-value | yes |
| `unregistered` | **Domain-level only** — confirmed NXDOMAIN across ≥2 resolvers *and* RDAP `not_found`. Never inferred from HTTP alone | **no — recheck fastest** | yes |
| `temporarily_unavailable` | 5xx, 429, connection reset, read timeout | no — backoff | no |
| `unclassified` | Fetched, but no rule fired confidently | no — re-classify offline when the classifier improves | no |

The right-hand column **is** the cheapest-first design: enrichment is driven off classification, and the states that dominate a healthy corpus (`live`) drive none of it.

**Two rules that are easy to get wrong, stated explicitly:**

- **`unregistered` is not terminal — it is the most urgent recheck class.** It is the *most* volatile record in the database: someone else can register it tomorrow. "Permanently classified" must never be read as "unregistered".
- **`hard_404` does not kill the domain.** A 404 says the page is gone; the domain may be perfectly alive. Only `domain_checks` may set a domain state.

**Terminality.** Only these are terminal (never re-fetched without `--force`): `hard_404` confirmed N consecutive times, `skipped`, and operator-set `out_of_scope`. Everything else has a `next_check_at`.

### Domain states

`unknown` → `active` | `parked` | `for_sale` | `expiring` (RDAP expiry within the configured window, or EPP `pendingDelete` / `redemptionPeriod` / `clientHold`) | `unregistered` | `no_rdap_for_tld` (honest gap — the TLD offers no RDAP; we do **not** guess) | `out_of_scope`.

### Enrichment states

Tracked per `external_links` row, not per URL: `pending` → `done` | `page_missing` | `url_not_found_in_wikitext`. An enriched row is **never re-enriched from the same `dump_run`**; a new dump run makes it eligible again.

## 12. Scheduling and recheck strategy

No daemon, no cron, no watch loop in v1. The operator runs `crawl` / `check`; the scheduler decides *what* is due.

Selection is a single ordered query: records where `terminal = 0 AND next_check_at <= now()`, ordered by (candidate value, oldest first), capped by `--limit`. Nothing else is touched.

`next_check_at` is set at classification time from a **configurable** cadence table. These are **policy defaults chosen to reflect volatility and value — not measured figures**; they are expected to be tuned once v1.J produces real data:

| State | Default interval | Rationale |
|---|---|---|
| `unregistered` | 3 days | Highest value, most volatile — can be taken by anyone |
| `for_sale` | 7 days | High value; listings and prices move |
| `parked` | 7 days | Often a waypoint to expiry |
| `dns_failure` | 7 days | Could be transient outage or the start of death |
| `tls_failure` | 14 days | Often neglect — a leading indicator |
| `soft_404` / `hard_404` | 30 days, ×2 backoff per repeat, capped at 180 | Page-level, low volatility |
| `live` / `redirect` | 90 days | Healthy; cheap to be patient |
| `temporarily_unavailable` | 1h, exponential ×2, cap 24h, then re-queue at 7 days | Genuinely transient |
| `blocked_by_robots` | 180 days | Policy can change, rarely does |
| `expiring` (domain) | 1 day | The window we actually care about |

`--force` overrides everything, including terminality, and is the only way to re-check a terminal record. Every forced run is logged as such in `crawl_runs`.

**Candidate value comes from `[scoring]`** (v2.E), not a separate priority table. The operator has already declared what a state is worth; a second ranking that could disagree would surface candidates in one order and revisit them in another, with no way to tell which was intended. Never-checked records still lead both queues — a record with no observation at all is the cheapest information available.

**`stats --due` shows the schedule without running it** (v2.E): five disjoint buckets per queue — never checked / due now / due within 7d / due later / terminal — plus which states are due. Database only, no requests. "Is there anything to do?" is the cheapest question the operator has and must not cost a crawl against real hosts to answer.

## 13. Error handling and retry behavior

**Two categories, matching the operator's colour code** — a failure is classified before it is retried.

**↷ Transient (yellow — retry will help):** DNS timeout, `SERVFAIL`, connection reset/refused, read timeout, TLS handshake timeout, HTTP 429, HTTP 5xx.
→ Retry within the run: exponential backoff with full jitter, base 2s, **max 3 attempts**, cap 60s. `Retry-After` is honoured exactly when present and overrides the backoff. If all attempts fail, the URL is classified `temporarily_unavailable` and re-queued per §12 — it is **not** marked dead.

**✗ Permanent (red — operator action or a real verdict):** HTTP 404/410, `NXDOMAIN` confirmed by ≥2 independent resolvers, malformed URL, unsupported scheme, robots disallow, response exceeding the size cap.
→ No in-run retry. Classify and move on.

**Cross-cutting:**

- **Per-host circuit breaker.** After K consecutive transient failures against one host, the *host* is cooled for the rest of the run — the run continues on everything else. One sick server never stalls a crawl.
- **Single-resolver DNS results are never trusted for `nxdomain`.** A second resolver must agree, and `resolvers_agreed` is recorded. A false NXDOMAIN would produce a fabricated "available domain" — the most expensive possible error for this tool.
- **RDAP failures are `unavailable`, never `not_found`.** A rate-limited or 500-ing RDAP server means *we don't know*, and `unknown` is written. This is the §2 no-fabrication rule enforced at code level.
- **Enrichment failures are non-fatal and specific.** A missing page ID, a corrupt block, or a URL with no literal wikitext occurrence sets a distinct `enrich_status` and the run continues — enrichment is auxiliary, and its failure must never abort a candidate export.
- **Ctrl-C is safe.** Checkpoint after each batch; an interrupted run loses at most the in-flight batch and leaves nothing stuck in `in_progress` (startup reclaims stale rows).
- **Exit codes:** `0` clean · `1` operator-actionable failure · `2` preflight failure · `130` interrupted.

## 14. Storage recommendations

**SQLite, single file, WAL mode, at `state/wikimill.db`, host-mounted** — the same posture as threadradar.

Rationale: single-writer local tool, zero-ops, transactional, trivially backed up, and `inspect` becomes a plain query. WAL lets `stats` / `inspect` read while a crawl writes.

- **Migrations:** forward-only, numbered, applied at startup. Schema version stored in-DB.
- **Layout:** `state/wikimill.db` · `state/dumps/` (the SQL dump, the XML multistream archive, and its index — large, gitignored, never committed, downloaded once and reused) · `state/logs/<run_id>.jsonl` · `outputs/` (exports).
- **The dumps are the cold store.** Deferred context lives in `state/dumps/`, not in the database. This is what keeps the DB proportional to *interesting* data rather than to Wikipedia. Roughly 32 GB total, so the directory is relocatable to any host path via `WIKIMILL_DUMPS_DIR` (§15) without moving the database.
- **Bounded growth:** the evidence-blob cap (§9) plus null-until-enriched context columns.
- **Postgres is deferred, not rejected.** Warranted only on *measured* evidence from v3.D — ingest time, DB size, query latency at scale. Guessing that ceiling now would be exactly the speculative-tier mistake. The trigger and the decision get an ADR when the numbers exist.
- **The row counts of a full enwiki ingest are currently unknown.** They are a v3.D measurement, not an estimate in this document. What *is* measured, as of v3.A: 62 KB of database per ingested page, over a 27,152-page corpus.

## 15. CLI command design

Flat, nine commands, no nested subcommands. Typer, `wikimill <verb>`.

**Configuration, as actually built at v1:** operational settings come from a mounted `wikimill.env` (§ Configuration below), and **policy — candidate states, scoring weights, trigger sets, marker lists — is compiled-in constants**, overridable per run only via `--state`. This section previously claimed policy lived in a `wikimill.toml`; it did not, and that drift is called out rather than quietly corrected. **`wikimill.toml` lands at v2.B/v2.C**, with the split being `.env` for credentials and environment, `.toml` for policy. Flags remain for *this run*; config is for *policy*.

### Invocation — the host launcher

There are two run paths over **one** Dockerfile, following threadradar rather than csi. csi is one-shot (`make buildsh` → `make crawl`); wikimill is eight commands re-invoked over weeks, so requiring a container shell first would grate.

```sh
./bin/wikimill preflight            # user path: builds (cached) + docker run --rm
./bin/wikimill crawl --limit 500
./bin/wikimill shell                # interactive container, for debugging
./bin/install                       # optional PATH shim → run from any directory

make buildsh                        # dev path: enter the wikimill1 container
make deps / make test               # uv sync / uv run pytest, inside
```

**`bin/wikimill`** is a host bash script — the only thing that ever runs outside Docker, and it runs no Python. It builds the image if stale, then `docker run --rm`s `python -m wikimill "$@"`. Deps are baked into the image; **source is bind-mounted**, so code edits need no rebuild. It resolves its own real path (`readlink -f`) before deriving the repo root, so it works through the `bin/install` symlink and always runs the latest source with no reinstall.

Quiet on cached builds; verbose only on first build or `WIKIMILL_REBUILD`.

### Configuration — `wikimill.env`

**All configuration is environment variables, sourced from a mounted `wikimill.env`.** The loader lands at **v1.B**, before anything needs it, so every later addition — an Enterprise token, an RDAP provider key, a proxy — is a one-line drop-in rather than a plumbing exercise.

- `wikimill.env` — real values. **Gitignored** (`*.env` with a `!wikimill.env.example` negation, matching threadradar).
- `wikimill.env.example` — committed, every variable documented, **no secrets, no real values**.
- Precedence: real process environment **>** `wikimill.env` **>** built-in default. So a one-off `WIKIMILL_DUMPS_DIR=/mnt/ssd2 ./bin/wikimill enrich` overrides the file without editing it.
- `preflight` prints the **resolved** value of every variable with secrets redacted, and fails `✗` naming the exact variable when a required one is missing.

The variables split across two layers, and the launcher must handle both:

| Layer | Read by | Variables |
|---|---|---|
| **Launcher** (host bash, before Docker) | `bin/wikimill` sources `wikimill.env` itself | `DOCKER_CMD` (default `sudo docker`) · `WIKIMILL_IMAGE` · `WIKIMILL_REBUILD` · `WIKIMILL_DRY_RUN` · `WIKIMILL_DUMPS_DIR` |
| **Application** (inside the container) | `config.py`, via `--env-file` | `WIKIMILL_USER_AGENT` · `WIKIMILL_CONTACT` · `WIKIMILL_DNS_RESOLVERS` · `WIKIMILL_CONCURRENCY` · *future API keys* |

`WIKIMILL_DUMPS_DIR` is the one that must be read on the **host** — it decides what gets bind-mounted, so it cannot be read from inside the container it configures.

**Two different dry-runs, deliberately distinguished.** `WIKIMILL_DRY_RUN=1` is *launcher-level*: print the `docker run` command — image, every mount, every env var passed — and exit without starting a container. It answers "what would this actually mount?", which matters most when `WIKIMILL_DUMPS_DIR` points somewhere unusual, and it makes the launcher testable with no Docker present. `ingest --dry-run` / `enrich --dry-run` are *command-level*: the container starts and reports what work it *would* do (how many pages, how many blocks) without doing it. Same word, different layers — the PRD and the help text always say which.

**Secrets rules:** never in the repo, never in the database, never in a log line, never in a `crawl_runs.args` blob. `preflight` and `--json` output redact anything whose variable name matches `*_KEY|*_TOKEN|*_SECRET|*_PASSWORD`.

### Dumps on external media

`WIKIMILL_DUMPS_DIR` exists because the three dumps total roughly **32 GB** (~4.9 GB SQL + ~26.6 GB XML + ~283 MB index) — more than many operators want in a repo tree. It points at any host path, and an **external SSD or HDD is an expected deployment**, not an edge case. That has four consequences:

1. **The database never goes on external or removable media.** `state/wikimill.db` stays on local disk; only `state/dumps/` relocates. SQLite in WAL mode relies on POSIX locking and durable `fsync`, and exFAT/NTFS/USB-detach breaks both — the failure mode is a corrupted database, not an error message. This is a hard rule, and `preflight` warns `↷` if the DB path resolves onto the same mount as the dumps.
2. **An unplugged drive is a clean `✗`, never a mid-run crash.** `preflight` verifies the mount exists, is readable, and holds the expected files *before* any work starts, and prints the resolved path plus the remediation. A drive that vanishes *during* a run surfaces as a permanent error, and the batch checkpoint means resume picks up where it stopped.
3. **Checksums are cached.** Verifying 32 GB over USB on every command would dominate runtime. `preflight` stores `(path, size, mtime, sha256)` after the first verification and re-hashes only when size or mtime changes; `--verify-dumps` forces a full re-hash.
4. **Random seek cost is why enrichment batches.** `enrich` does one seek + one block decompress per candidate page. On an SSD that is free; on a spinning HDD, thousands of scattered seeks across a 26.6 GB file is the difference between minutes and hours. So `enrich` **sorts candidate pages by `ms_offset` and groups them by block** — one seek and one decompress serves the ~100 pages that share a stream. This was originally a v2 optimization; an HDD being a first-class target moves it into **v1.H**, since sorting by an already-stored column is nearly free to implement and the worst case without it is severe.

| Command | Purpose | Essential flags |
|---|---|---|
| `preflight` | Doctor. Verifies Docker context, both dumps present + checksummed + **same run date**, multistream index readable, DB migrated, DNS/RDAP reachable, User-Agent configured. **Runs before every real command and aborts on `✗`.** | `--json` |
| `ingest` | SQL dump → URL + domain queue (no context) | `--dump <path>` · `--pages <range>` · `--limit N` · `--dry-run` |
| `crawl` | Crawl pending/due URLs | `--limit N` · `--concurrency N` · `--force` |
| `check` | Domain-level DNS + RDAP on due domains | `--limit N` · `--state <list>` · `--force` |
| `enrich` | Back-fill section / anchor / citation context for a **selected subset** | `--state <list>` · `--limit N` · `--dry-run` |
| `inspect <url\|domain>` | Everything known about one thing, incl. full history and Wikipedia evidence | `--json` |
| `export` | Candidates → file | `--state <list>` · `--min-pages N` · `--format csv\|jsonl` · `--out <path>` |
| `stats` | Counts by state, queue depth, due counts, **un-enriched candidate count**, recent runs | `--json` |

Two departures from the six commands originally sketched, both deliberate:

- **`preflight`** — a mandatory preflight gate is a house conformance rule (threadradar ADR-0008), and this tool has more ways to be misconfigured than most (two dumps that must agree on run date, a 283 MB index, DNS/RDAP reachability).
- **`enrich`** — the lazy-context stage needs its own verb precisely *because* it is the expensive one. A separate command means the operator explicitly decides when to pay, sees exactly what it will cost (`--dry-run` prints the page count), and gets `↷ nothing to enrich` for free when there is nothing dead. Hiding it inside `export` would bury an expensive operation in a cheap-looking command.

A `config` command is deliberately **not** included yet — there is no configuration complex enough to warrant one. Add it when there is.

Every command that touches state is **idempotent** and prints `✓ / ↷ / ✗` per step, live.

## 16. Observability and logging

- **`✓ ✗ ↷` on every step**, no exceptions for boring steps — consistency is the value.
- **Colour code:** yellow `↷` = transient / retry-able / skipped-because-already-done; red `✗` = permanent, operator action needed. Categorization is decided at §13, not ad hoc at the print site.
- **Vicarious output.** `ingest`, `crawl`, and `enrich` print progress as work happens (`flush=True`), never batched into a final summary. A 40-minute run that prints nothing for 40 minutes is a bug.
- **Cost is always visible before it is paid.** `enrich --dry-run` reports how many pages and how many multistream blocks would be touched. The operator never discovers an expensive stage by watching it run.
- **Structured run log:** `state/logs/<run_id>.jsonl`, one object per event — machine-greppable, survives the terminal.
- **Every check row is self-describing:** `crawler_version` + `classifier_version` + `classifier_reasons`. "Why is this marked parked?" is answerable from the DB alone, months later.
- **Run summary** on every command: counts by outcome, duration, rows written, next-due count.
- **Error messages name the fix.** A distinct message per failure cause, disambiguated by parsing the actual response — never a bare HTTP status, never a raw stack trace.

## 17. Legal, licensing, robots.txt, and attribution

*Not legal advice; the operator decides what to publish.*

- **Wikipedia text is CC BY-SA 4.0 (dual-licensed GFDL).** Anchor text and `context_excerpt` are **excerpts of CC BY-SA content**. Internal analysis is unencumbered; **any published or redistributed export must attribute and share-alike**. Every export therefore carries a `source_page_url` column and a licence header — so an export is attributable by construction, whatever the operator later does with it. (Wikidata main-namespace structured data and Wikimedia analytics datasets are CC0.)
  *The lazy design has a pleasant side effect here: CC BY-SA excerpts are only ever stored for the small enriched subset, not for the whole corpus.*
- **Dumps etiquette.** Download each dump once, keep it in `state/dumps/`, re-read locally. Never re-download per run. Use a mirror where practical. `preflight` verifies the local files rather than re-fetching them.
- **Wikimedia User-Agent policy.** Any Wikimedia HTTP request (API spot-checks, dump downloads) sends a descriptive UA with contact info, in the documented form: `wikimill/<version> (<contact-url-or-email>) httpx/<ver>`. A generic or absent UA is throttled or blocked.
- **API rate limits** (verified 2026-07-25): 10 req/min unidentified, 200 req/min with a compliant UA. wikimill stays far under this — the API is spot-check only.
- **robots.txt on crawl targets is honoured, always.** Fetched per origin, cached with a TTL in `robots_cache`, re-fetched on expiry. A disallowed URL is classified `blocked_by_robots` and **never fetched** — not even once "to check". `Crawl-delay` is honoured where declared; otherwise a default per-host delay applies.
- **Politeness:** `GET`/`HEAD` only. Per-registrable-domain concurrency of **1**. Bounded global concurrency. `Retry-After` always honoured. The crawler identifies itself honestly and never spoofs a browser UA.
- **RDAP over WHOIS.** RDAP is the standardized, documented interface with sane terms. Port-43 WHOIS is used **not at all** in v1, and registrar web pages are never scraped (§2).
- **Ethical posture, stated plainly.** Wikipedia-cited expired domains are attractive to link-spammers, and buying one to launder its citations degrades Wikipedia. This tool exists to find *legitimate* related properties — the bar is clean, acquireable, and genuinely relevant, not merely high-authority. The export carries citation evidence prominently, so any acquisition decision is made with full sight of what the domain was cited *for*.

## 18. Security considerations

The crawler fetches **operator-untrusted URLs harvested from a publicly editable wiki**. Anyone can add a link to Wikipedia. Treat every target as hostile.

- **SSRF defence.** Resolve first, then check: refuse loopback (`127.0.0.0/8`, `::1`), private ranges (`10/8`, `172.16/12`, `192.168/16`, `fc00::/7`), link-local (`169.254.0.0/16` — including the cloud metadata address `169.254.169.254`), and `0.0.0.0/8`. **Re-check after every redirect hop** — a target that redirects to an internal address is a DNS-rebinding attack, and checking only the first hop is the standard way to get this wrong.
- **Redirect caps.** Max 5 hops, loop detection, full chain recorded.
- **Response caps.** Max body size (default 2 MB) and max total request time, both enforced *while streaming* — a hostile server cannot exhaust memory with an endless body.
- **No execution of fetched content.** No JS, no browser, no PDF/office parsers. HTML is parsed only enough to extract `<title>` and run text heuristics. This is the security half of the no-browser non-goal.
- **Decompression-bomb guards** on all archive handling: gz and bz2 are **streamed** with an output-size cap and a compression-ratio ceiling. This applies to the 4.9 GB SQL dump, to each seeked multistream block, and to `Content-Encoding: gzip` responses alike. The multistream path is the sharpest case — it decompresses at an operator-influenced byte offset, so a bad offset must fail cleanly rather than consume the machine.
- **The SQL dump is parsed, never executed.** `externallinks.sql.gz` is a MySQL dump from a public source; piping it into a database engine executes attacker-influenceable SQL. `v1.C` implements a streaming `INSERT`-tuple parser. All of our own DB access is parameterized — no string-built SQL, ever.
- **Untrusted text stays untrusted.** Page titles, anchor text, and body excerpts are attacker-controlled. They are escaped on output, never interpolated into shell commands, and — if an optional LLM stage ever lands (§2) — must be passed as clearly delimited data with prompt-injection assumed.
- **Container isolation.** Runs in Docker (operator hard rule), with only `state/` and `outputs/` mounted. No host filesystem beyond that.
- **Secrets:** v1 needs no credentials, but the **handling is built at v1.B anyway** (§15) — a mounted, gitignored `wikimill.env`, a committed `.example` with no real values, redaction on every output surface, and no secret ever reaching the DB or a log line. Building the safe path before there is anything to leak is cheaper than retrofitting it around a key that has already been committed once.

## 19. Acceptance criteria

v1 is done when all of these are demonstrably true:

1. Runs end-to-end **inside Docker** with no host Python install.
2. `ingest` on the chosen page-ID slice seeds `urls` + `domains` from the SQL dump **without reading the XML article dump at all** (verifiable: the article archive can be absent and `ingest` still succeeds).
3. URL reconstruction from `el_to_domain_index` + `el_to_path` is correct on a fixture suite including `www`-prefixed, multi-label, IDN, port-bearing, and path-less cases.
4. The namespace-filter approach (index intersection vs. `page.sql.gz`) is **verified, not assumed**, and the result is recorded in this PRD.
5. Re-running `ingest` on the same slice adds **zero** new rows and reports `↷ already ingested`.
6. Every one of the eleven classifications in §11 is **reachable and exercised** — each has at least one real row, or is documented as not-yet-observed in the sample with a stated reason.
7. `unregistered` is only ever set with ≥2 resolvers agreeing **and** RDAP `not_found`. No code path exists to set it from HTTP alone.
8. Archive-wrapped URLs are unwrapped: no queued URL has host `web.archive.org` / `archive.today` / `ghostarchive.org`.
9. `url_checks` and `domain_checks` are append-only — a second check of the same URL produces a **second row**, and the first is byte-identical to before.
10. A second `crawl` run immediately after the first fetches **nothing** (everything is within its recheck window) and says so.
11. `robots.txt` is fetched, cached, and honoured; at least one URL is classified `blocked_by_robots` without ever having been fetched (verifiable from the log).
12. **`enrich` on a subset with zero dead links exits `↷ nothing to enrich` having opened neither the archive nor the index.** This is the criterion that proves the cheapest-first ordering is real and not just documented.
13. `enrich` on a real candidate subset produces correct section, anchor text, and citation context for a **manually spot-checked sample of 20**, verified against the live article.
14. Re-running `enrich` against the same dump run re-parses **nothing**.
15. `export` produces a file whose every row carries source page URL, domain state, and last-checked timestamp — plus section/anchor/context where enriched — and **no blank field is ever filled with a guess**.
16. `inspect` on a known domain shows full check history in chronological order.
17. **Proof gate:** at least one verified acquireable-or-unregistered domain, cited by ≥1 enwiki article, appears in an export with complete evidence — and survives manual verification.
18. **Every stage in the §8 contract is re-run once in sequence and the second pass is a no-op** — no duplicate rows, no re-fetch, no re-parse, every step reporting `↷`. This is one test, run across the whole pipeline, not per-stage assertions.
19. `export` run twice against unchanged state produces a **byte-identical file** with a matching `sha256`.
20. `wikimill.env` is gitignored, `wikimill.env.example` is committed with no real values, and a variable matching `*_KEY|*_TOKEN|*_SECRET|*_PASSWORD` is redacted in `preflight`, `--json` output, logs, and `crawl_runs.args`.
21. `preflight` fails `✗` with the resolved path and a remediation when `WIKIMILL_DUMPS_DIR` is missing or unmounted, and warns `↷` if the database resolves onto the same mount as the dumps.
22. `WIKIMILL_DRY_RUN=1 ./bin/wikimill <cmd>` prints the full docker invocation and starts no container — verified with Docker unavailable.
23. Measured and recorded in v1.J: SQL ingest wall time, rows produced, crawl throughput, **the live/dead ratio of the slice**, enrichment cost per candidate page **on both SSD and HDD dump storage**, DB size, and the classification distribution. *(Measured, not estimated — these are inputs to v3 planning; the live/dead ratio is what tells us how much the lazy ordering actually saved, and the SSD/HDD split is what tells us whether block batching was worth pulling into v1.)*

**Suite-green is not feature-proven.** Tests verify code correctness; criteria 4, 13, 17, and 23 require a real run and operator validation.

## 20. Conformance rules

Violations are bugs, not stylistic preferences.

- **Runs only inside Docker** via the central builder Makefile. No host Python install. (operator hard rule) The single exception is `bin/wikimill` / `bin/install` — host **bash** scripts that execute no Python and exist solely to start the container.
- **Context extraction is lazy.** No code path parses wikitext for links that have not first been classified as interesting.
- **Read-only against everything external.** `GET`/`HEAD` only. No write/post/auth-mutation path may exist in the codebase.
- **robots.txt is honoured unconditionally.** No override flag exists.
- **No fabricated data.** Unknown is `unknown`; unmeasured is blank. RDAP failure never becomes "available".
- **Append-only history.** No `UPDATE` on `url_checks` / `domain_checks`, ever.
- **Dump-run pinning.** SQL and XML dumps must share a run date; every row records it.
- **No browser.** Pure HTTP client.
- **`uv`-managed, single lockfile.** No `requirements.txt` / `poetry.lock` / `pip` / bare `python`.
- **Every stage is idempotent, per the §8 stage contract.** Re-running any command is always safe and never duplicates work; each declares its idempotency key. Every step prints `✓ / ↷ / ✗`, ending in a summary.
- **Deterministic export.** Fixed ordering, so the same filter over the same state yields a byte-identical file.
- **All config via environment,** loaded from a mounted `wikimill.env`. No secret in the repo, the DB, or a log.
- **The database never lives on removable or non-POSIX media.** Dumps may; SQLite may not.
- **Preflight gates every state-touching command** and aborts on `✗` before any work.
- **PSL-derived registrable domains.** No naive label-splitting anywhere in the codebase.
- **Self-contained.** wikimill reads and writes only its own `state/` and `outputs/`. It has no knowledge of, and no dependency on, any sibling project.
- All canonical doc surfaces match reality and code (spec discipline, below).

## 21. Risks and tradeoffs

| Risk | Impact | Mitigation |
|---|---|---|
| **Deferred context may be unreachable later.** If the XML dump for the pinned run is deleted from disk or the mirror, enrichment for that run becomes impossible. | Candidates without evidence — the export loses most of its value. | Dumps are kept locally in `state/dumps/`; `preflight` verifies presence and checksum before any run. Wikimedia keeps several runs, but we do not rely on that. |
| **Dump-run skew.** SQL and XML from different dates → a page revised in between yields context that does not match the link. | Subtly wrong anchor text / section attributed to a link. | Same-run-date pinning enforced by `preflight` and recorded per row (§6, §20). |
| **`url_not_found_in_wikitext`.** Template-expanded links have no literal wikitext occurrence, so some candidates will never get anchor text. | Some high-value candidates ship with partial evidence. | Recorded as an explicit, honest status — not an error and not silently blank. The link and its citing page are still known; only the surface context is absent. Frequency is measured at v1.J. |
| **Namespace filtering is unverified.** `externallinks` has no namespace column; the index-intersection approach is a hypothesis. | Non-article pages could pollute the corpus. | Acceptance criterion 4 makes verification a gate, with `page.sql.gz` as the documented fallback. Not assumed. |
| **Archive-wrapped citations.** A large (unmeasured) share of Wikipedia refs point at `web.archive.org`. | Naive crawling would measure the Internet Archive's uptime instead of the cited domain's — confidently wrong results. | Unwrapping at normalization (§10 rule 9); acceptance criterion 8 enforces it. |
| **Parked-page detection is heuristic** and drifts as parking providers change templates. | False positives/negatives on the highest-value class. | Versioned classifier + stored evidence → re-classify offline when signatures are updated, with zero re-fetching. |
| **RDAP coverage is uneven** across TLDs; some ccTLDs offer none. | Some domains can never be confirmed unregistered. | Honest `no_rdap_for_tld` state. Never guessed. Coverage becomes a measured, reportable number. |
| **False NXDOMAIN → fabricated "available" domain.** | The single most expensive error this tool can make; the operator could act on it. | Two-resolver agreement required *and* RDAP confirmation, both recorded (§13). |
| **SQLite at full-enwiki scale.** | v3 could stall on write throughput or DB size. | v1/v2 stay bounded; v3.D measures before v3.E decides. No premature Postgres. |
| **Dump-source fragility.** The free Enterprise HTML mirror already died once (March 2025). | A source we depend on could vanish. | Both primary sources are the plainest, oldest, most stable artifacts Wikimedia publishes. Slices are kept locally, so a mirror outage does not stop work. |
| **Politeness vs. throughput.** Per-domain concurrency of 1 caps crawl speed. | Large runs take a long time. | Accepted deliberately. Breadth across many domains provides the parallelism; a faster crawler is not worth being a bad citizen. |
| **Ethical / reputational.** Wikipedia-cited domains attract link-spam acquisition. | Misuse would degrade Wikipedia. | Evidence-forward exports; clean+relevant scoring; no auto-acquisition (§2, §17). |

## 22. Open questions

None of these block approval; each is answerable at its phase. Q1 and Q2 are wanted before `v1.C` starts.

- **Q1 (v1.C) — the v1 page-ID slice.** *Recommendation:* the range covered by one multistream part (e.g. `p1p41242`). Which part, and how large? Operator confirmation wanted.
- **Q2 (v1.B) — crawler identity.** The exact `WIKIMILL_USER_AGENT` / `WIKIMILL_CONTACT` values. The mechanism is settled (§15 — `wikimill.env`); only the values are outstanding. Operator must supply: this is a public identity, and the Wikimedia UA policy requires real contact info.
- **Q3 (v1.C) — English only for v1?** *Recommendation: yes* — enwiki only; other wikis are a v4 candidate.
- **Q4 (v1.F) — evidence-blob default.** Store 8 KB of body for non-`live` checks? Trades disk for offline re-classification. *Recommendation: yes, 8 KB, configurable.*
- ~~**Q5 (v1.G) — RDAP access strategy**~~ — **resolved 2026-07-25 by measurement.** IANA bootstrap (RFC 9224) cached on disk for 7 days, RFC-correct longest-label match, then a direct query to the registry's own RDAP endpoint. Measured against the live registry: **1,200 TLDs across 590 service groups** — but `.de`, `.es`, `.io`, `.ru` and `.edu` publish **no RDAP at all**, so domains under them can never be *confirmed* unregistered and are recorded `no_rdap_for_tld`. No TLD needs bespoke handling; the gap is a coverage fact to report, not a special case to code around.
- ~~**Q6 (v1.H) — enrichment trigger set**~~ — **resolved 2026-07-25 as recommended.** Defaults to `unregistered, for_sale, parked, dns_failure, tls_failure, soft_404, hard_404`; `live`, `redirect`, `temporarily_unavailable` and `unclassified` are excluded, because paying to extract context for a working link is exactly the work this ordering exists to avoid. Overridable with `--state`.
- **Q7 (v1.I) — export columns.** Implemented as recommended, plus score, registrar, expiry, public suffix, private-suffix flag, `{{dead link}}` flag, archive URL, and the score breakdown — 18 columns. *Still open for operator confirmation:* whether that set is right, or too wide. It is cheap to trim; the deterministic-ordering guarantee is unaffected by which columns are present.
- **Q8 (v4) — Wikimedia Enterprise.** Worth an account for free-tier Structured Contents (free since 2026-07-01) as an alternative enrichment path? Only re-evaluate after the local multistream path has soaked.

## 23. Implementation sequence

Post-approval order. Each step lands with its tests and its doc updates in the same commit. Steps 2–8 are each independently runnable and verifiable, because every stage communicates only through the database.

1. **Scaffold** (`v1.B`) — repo shape copied from csi/threadradar: `pyproject.toml` (uv, single lockfile), `Dockerfile`, **`Makefile`** (thin — `BUILDER_PATH ?= ../../builder`, `STACK ?= python`, `CONTAINER_NAME ?= wikimill1`, empty `HOST_PORT`/`CONTAINER_PORT` → `--network=host`, `include $(BUILDER_PATH)/Makefile`) + **`Makefile.local`** (project targets; must not override `run`), `src/wikimill/`, `docs/`, `state/`, `outputs/`, `.gitignore`, `.dockerignore`. **`bin/wikimill` launcher + `bin/install` PATH shim** (§15), symlink-safe, with `WIKIMILL_DUMPS_DIR` mount support and a `WIKIMILL_DRY_RUN` path so the launcher is testable without Docker. **`config.py` env loading + `wikimill.env.example` + gitignore + redaction** — built now, before any secret exists. SQLite schema + forward-only migrations. `preflight` (incl. dump presence, cached checksums, DB-not-on-removable-media check). Logging + `✓ ✗ ↷` markers + typed error hierarchy + exit-code contract.
2. **SQL ingest** (`v1.C`) — streaming gz → `INSERT`-tuple parser → reversed-domain un-mangling → page-ID slice filter → `external_links` (context null) + `wiki_pages` from the multistream index. **Verify the namespace filter here.** Tests run against a small committed SQL fixture, never a live dump.
3. **Normalize** (`v1.D`) — URL canonicalization, archive unwrapping, scheme/internal/resolver filtering, PSL domain extraction → `urls` + `domains`. Heavily unit-tested; this is where subtle bugs become wrong answers.
4. **Crawl** (`v1.E`) — robots cache, per-host politeness, redirect tracking with per-hop SSRF checks, streaming size caps, transient/permanent retry → append-only `url_checks`. Tests hermetic via `httpx.MockTransport`.
5. **Classify** (`v1.F`) — the eleven-state classifier as a **pure function over a stored check row**, so it is testable offline and re-runnable without network. State machine + `next_check_at`.
6. **Domain checks** (`v1.G`) — multi-resolver DNS + RDAP → `domain_checks` → domain state. `unregistered` gated behind both signals.
7. **Enrich** (`v1.H`) — index loader → seek + single-block decompress → `mwparserfromhell` → context columns, for the selected subset only. The empty-subset fast path is written **first**, and tested first — it is the whole point of the ordering.
8. **Surface** (`v1.I`) — `stats`, `inspect`, scoring, `export` with evidence columns + licence header.
9. **Soak** (`v1.J`) — first full bounded run; measure §19.18; fix what the real data breaks. Then the queue is open — operator soak-tests, bugs surface, v2 gets scoped from them.

## 24. References

- Project templates: `/home/vijo/work/projects/crawlers/threadradar` (repo shape, Docker/uv/Makefile wiring, preflight, event markers) · `/home/vijo/work/projects/crawlers/csi` (one-shot crawler shape).
- Central builder: `/home/vijo/work/projects/builder` (Makefile include, `Dockerfile.python`, `Makefile.python`, `dev_container.sh`).
- Wikimedia dumps: <https://dumps.wikimedia.org/enwiki/> · licence terms <https://dumps.wikimedia.org/legal.html>
- `externallinks` schema: <https://www.mediawiki.org/wiki/Manual:Externallinks_table>
- Multistream format + index: <https://en.wikipedia.org/wiki/Wikipedia:Database_download>
- API: <https://www.mediawiki.org/wiki/API:Exturlusage> · rate limits <https://www.mediawiki.org/wiki/Wikimedia_APIs/Rate_limits>
- Enterprise HTML dump archive (discontinued mirror): <https://dumps.wikimedia.org/other/enterprise_html/> · Enterprise free tier <https://enterprise.wikimedia.com/blog/enhanced-free-api/>
- Operator rule reference: `sites/portfolio/docs/for-future-projects.md`.

## Spec discipline

All canonical doc surfaces (this file + `architecture.md` + `shipping-history.md` + any ADRs + `CLAUDE.md`) must match reality and code. Drift is a conformance failure fixed in the same commit as the change that made it stale — not a backlog item.

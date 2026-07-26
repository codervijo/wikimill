# v1.J — soak report

Measured 2026-07-26 against `enwiki-20260701`, page-ID slice `p1p41242`.
Every figure here is measured. Where something could not be measured, it says so
rather than carrying an estimate.

## 1. Acceptance criteria

Criteria 1, 2, 4, 12, 13, 16, 19–22 are covered by the 441-test suite and by the
live verification recorded in `prd.md` §7 design notes. The criteria below need a
real corpus, and were re-verified by a harness over the live database.

| # | Criterion | Result |
|---|---|---|
| 3 | URL reconstruction correct across real shapes | **pass** — 13 real-shape fixtures (www, multi-label, IDN, port, path-less, `V4.` IP, opaque scheme) |
| 5 | Re-running `ingest` adds zero rows | **pass** — second run inserted 0, reported `↷ already ingested` |
| 6 | All eleven classifications reachable | **pass, with documented gaps** — 8 of 11 observed. Not observed: `dns_failure` (reachable but unexercised — the two NXDOMAIN domains were found by the *domain* check, so their URLs were never crawled), `tls_failure` (no TLS-broken host in the 440-URL sample), `parked` (no parking-provider page; the one `for_sale` was found by sale phrasing alone) |
| 7 | `unregistered` gated on 2 resolvers **and** RDAP 404 | **pass** — 0 violations across all classifications |
| 8 | Archive URLs unwrapped, none queued | **pass** — 0 archive hosts in the queue, out of **428,101 archive-wrapped links unwrapped at ingest** |
| 9 | `url_checks` append-only | **pass** |
| 10 | Recheck window holds | **pass** — 0 settled URLs re-due; 9 `temporarily_unavailable` correctly re-due on their deliberate 1-hour cadence |
| 11 | robots.txt honoured, blocked URLs never fetched | **pass** — 83 blocked, 0 of them fetched |
| 14 | Re-enrich is a no-op | **pass** (after a fix — see §4) |
| 15 | No blank field is ever guessed | **pass** — 0 `done` rows with no context |
| 17 | **Proof gate** | **pass** — see §3 |

## 2. Measurements (criterion 23)

### Ingest — the full 4.9 GB dump

| | |
|---|---|
| Dump | `enwiki-20260701-externallinks.sql.gz`, 4,917,848,204 bytes |
| **Wall time** | **3,708 s (61.8 min)** |
| **Peak RSS** | **28 MB** |
| Rows scanned | ~190,000,000 |
| Link occurrences inserted | 1,432,352 |
| Duplicate occurrences collapsed | 334,226 |
| Unique URLs | 1,326,045 |
| Unique registrable domains | 135,591 |
| **Database size** | **1.15 GB** |

**28 MB of memory to process 4.9 GB** is the headline. The streaming design holds
completely: memory is flat regardless of dump size, because statements are read
one at a time and tuples yielded as they are scanned.

### What normalization removed, and why it mattered

| Filter | Links | Note |
|---|---|---|
| Archive URLs unwrapped | **428,101** | ~30% of links were archive-wrapped. Without unwrapping, nearly a third of the corpus would have measured the Internet Archive's uptime rather than the cited domain's |
| `identifier_resolver` | **205,472** | ~14%. `doi.org`, `handle.net` and friends would otherwise have dominated the crawl queue exactly as §10 predicted |
| `wikimedia_internal` | 9,628 | |
| `not_crawlable_scheme` | 163 | plus `news` 4,212 · `ftp` 279 · `urn` 9 · `gopher` 6 · `irc` 2 · `sips` 1 · `telnet` 1 · `tel` 1 |

Both of the large filters were designed on reasoning, before any real data. The
measurements justify them: together they account for **44%** of raw links.

### Crawl throughput

| | |
|---|---|
| 400 URLs, concurrency 8 | 655 s → **0.61 URL/s** |
| Robots-blocked (never fetched) | 83 of 440 |
| Transient failures retried | 10 |
| Circuit breakers tripped | 0 |

Throughput is bounded by politeness, not by the code: one request per registrable
domain at a time, plus a per-host delay. Dead domains cost the most, because a
DNS timeout is paid before the fetch is abandoned.

### Enrichment

| | |
|---|---|
| 49 blocks → 4,900 pages decompressed | **12.2 s** |
| Empty-subset fast path | **0.0 s**, archive never opened |

Confirms the ~100-pages-per-block assumption the cheapest-first ordering rests on.

### Live/dead ratio

277 `live`+`redirect` against 69 dead (`hard_404`, `soft_404`, `for_sale`) in the
440-URL sample — **4.0 : 1**. This is the number that says how much the lazy
ordering saves: roughly four in five links never need context extraction.

### Not measured

- **Enrichment cost on HDD storage.** Only SSD-backed storage was available. The
  SSD figure (12.2 s / 49 blocks) is recorded; the HDD comparison criterion 23
  asks for is **unmeasured**, not estimated. Block batching was implemented on
  the reasoning in architecture.md §3, and remains unvalidated against a spinning
  disk.
- **Full-corpus crawl.** 440 of 1,326,045 URLs have been crawled (0.03%). Crawl
  and domain-check figures come from that sample and may not generalize.

## 3. Proof gate (criterion 17)

Two domains cited by English Wikipedia, confirmed available, and **independently
re-verified outside wikimill** with `dig` and `curl` (NXDOMAIN + RDAP HTTP 404):

| Domain | Cited by | Anchor |
|---|---|---|
| `tetris-today.com` | *Tetromino* §External links | "The Father of Tetris" |
| `radiopr740.com` | *Telecommunications in Puerto Rico* | "Radio Puerto Rico" |

Also `marygordon.org.uk` — `for_sale`, cited by *Electric boat* §Golden Age.

**Operator verdict: all three are duds**, for reasons the tool does not model:

- `tetris-today.com` — *Tetris* is a **trademark**. Confirmed available, and unusable.
- `radiopr740.com` — a **niche mismatch**; the operator has no interest or
  knowledge in Puerto Rican radio.

This is the most important finding in the soak. **The tool was factually correct
and useless on judgement.** Both domains really are available; neither is worth
having. That is a different failure from a miscalibrated weight, and no
reweighting would have caught either. Trademark screening and niche matching are
recorded as known gaps, deliberately **not** built (operator instruction).

## 4. Defects found by the soak

1. **`count_pending` did not mirror `select`.** The count omitted the
   `wiki_pages` join and the `ms_offset IS NOT NULL` guard that selection
   applies, so a link whose page had no offset was counted as pending but was
   never selectable: `enrich` would report work, open the archive for nothing —
   defeating the criterion-12 fast path — and leave the row pending on every
   later run. Fixed, with two regression tests.
2. **The soak harness itself was wrong twice**, and is worth recording because
   both were false alarms that could have prompted bad "fixes":
   - it flagged 9 URLs as wrongly re-due when they were `temporarily_unavailable`
     on their intended 1-hour cadence;
   - it treated unobserved classifications as failures, when criterion 6
     explicitly permits documenting them with a reason.

## 5. What the scale change revealed

The 4 MB sample and the full dump disagree about the most important thing.

| | 4 MB sample | Full dump |
|---|---|---|
| Domains | 808 | 135,591 |
| Cited by exactly one page | 89% | **71%** |
| Cited by 3+ pages | 4.3% (35) | **16.5% (22,328)** |
| Cited by 10+ pages | 0.5% (4) | **4.5% (6,110)** |

On the small sample, citation count looked useless as a quality signal — nearly
everything was cited once. **At real scale it discriminates.** There are 22,328
domains with three or more citing articles and 6,110 with ten or more, so
`--min-pages` becomes a real lever rather than a way to empty the output.

The most-cited domains in the slice are `google.com` (14,025 citing pages),
`loc.gov` (10,212), `archive.org` (10,073), `d-nb.info` (8,838), `jstor.org`
(7,885) — infrastructure and reference works, as expected, and precisely the
band where a genuinely valuable expired domain would be conspicuous.

**Consequence for scoring:** the earlier diagnosis — that availability (+50)
swamps citation weight (+3) — was drawn from a corpus where citation weight could
not vary. It can now. Re-tuning should wait for a crawl at this scale rather than
be redone against the same 440 URLs.

## 6. Open items

- **Crawl at scale.** 0.03% of the corpus is crawled. At 0.61 URL/s a full pass
  is impractical and unnecessary; a stratified sample (say, everything with 10+
  citing pages) would be both faster and far more likely to contain something
  worth having.
- **HDD enrichment measurement**, still outstanding.
- **Operator verdict capture.** All three finds were judged duds by hand, and
  nothing in the tool recorded that. Until verdicts are stored, precision cannot
  be measured and the weights stay guesswork — which is what v2's config tier is
  meant to enable.

"""Forward-only schema migrations.

Rules:
  * Migrations are append-only and never edited once shipped. To change
    something, add a new migration.
  * Each is a list of statements applied in one transaction, so a failure
    leaves the database at the previous version rather than half-migrated.
  * `url_checks` and `domain_checks` are APPEND-ONLY by contract (prd.md §20).
    Nothing in this codebase may UPDATE them — history is the product.

Schema mirrors docs/prd.md §9.
"""

from __future__ import annotations

from typing import Final

MIGRATION_1: Final[tuple[str, ...]] = (
    # -- Wikipedia side ----------------------------------------------------
    """
    CREATE TABLE wiki_pages (
        page_id      INTEGER NOT NULL,
        lang         TEXT    NOT NULL DEFAULT 'en',
        title        TEXT    NOT NULL,
        -- Byte offset into the multistream archive, from its index file. This
        -- is what makes enrichment a seek instead of a scan, and what lets
        -- `enrich` sort candidates by offset to batch block decompression.
        ms_offset    INTEGER,
        dump_run     TEXT    NOT NULL,
        ingested_at  TEXT    NOT NULL,
        PRIMARY KEY (page_id, lang, dump_run)
    )
    """,
    "CREATE INDEX idx_wiki_pages_title ON wiki_pages(title)",
    "CREATE INDEX idx_wiki_pages_offset ON wiki_pages(ms_offset)",
    """
    CREATE TABLE external_links (
        id            INTEGER PRIMARY KEY,
        page_id       INTEGER NOT NULL,
        lang          TEXT    NOT NULL DEFAULT 'en',
        url_raw       TEXT    NOT NULL,
        url_hash      TEXT    NOT NULL,
        dump_run      TEXT    NOT NULL,
        first_seen    TEXT    NOT NULL,
        last_seen     TEXT    NOT NULL,
        -- Context columns: NULL until `enrich` fills them. This is the
        -- cheapest-first design made physical — most links never get here.
        section           TEXT,
        section_level     INTEGER,
        anchor_text       TEXT,
        link_kind         TEXT,
        ref_name          TEXT,
        template_name     TEXT,
        context_excerpt   TEXT,
        dead_link_tagged  INTEGER NOT NULL DEFAULT 0,
        archive_url       TEXT,
        archive_date      TEXT,
        enriched_at       TEXT,
        enrich_dump_run   TEXT,
        enrich_status     TEXT NOT NULL DEFAULT 'pending',
        UNIQUE (page_id, url_hash, dump_run)
    )
    """,
    "CREATE INDEX idx_external_links_url ON external_links(url_hash)",
    "CREATE INDEX idx_external_links_page ON external_links(page_id)",
    "CREATE INDEX idx_external_links_enrich ON external_links(enrich_status)",
    # -- URL queue ---------------------------------------------------------
    """
    CREATE TABLE urls (
        url_hash            TEXT PRIMARY KEY,
        url_normalized      TEXT NOT NULL,
        -- Which normalization ruleset produced url_hash. Changing a rule
        -- changes the hash; without this column that would silently fork
        -- identity across the table (prd.md §8).
        normalizer_version  INTEGER NOT NULL,
        domain_id           INTEGER REFERENCES domains(domain_id),
        scheme              TEXT NOT NULL,
        state               TEXT NOT NULL DEFAULT 'pending',
        terminal            INTEGER NOT NULL DEFAULT 0,
        first_seen          TEXT NOT NULL,
        last_checked        TEXT,
        next_check_at       TEXT,
        check_count         INTEGER NOT NULL DEFAULT 0,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        cite_count          INTEGER NOT NULL DEFAULT 0,
        distinct_page_count INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX idx_urls_state ON urls(state)",
    "CREATE INDEX idx_urls_domain ON urls(domain_id)",
    # The scheduler's hot query: due, non-terminal, oldest first.
    "CREATE INDEX idx_urls_due ON urls(terminal, next_check_at)",
    """
    CREATE TABLE url_checks (
        id                   INTEGER PRIMARY KEY,
        url_hash             TEXT NOT NULL,
        checked_at           TEXT NOT NULL,
        http_status          INTEGER,
        final_url            TEXT,
        final_url_hash       TEXT,
        redirect_chain       TEXT,
        redirect_count       INTEGER NOT NULL DEFAULT 0,
        cross_domain_redirect INTEGER NOT NULL DEFAULT 0,
        content_type         TEXT,
        content_length       INTEGER,
        page_title           TEXT,
        body_sha256          TEXT,
        -- Bounded head of the response, stored only for non-`live` results —
        -- exactly the cases where an improved classifier changes the verdict.
        -- This is what makes offline re-classification possible with no refetch.
        evidence_blob        TEXT,
        latency_ms           INTEGER,
        classification       TEXT,
        classifier_version   INTEGER,
        classifier_reasons   TEXT,
        error_kind           TEXT,
        error_detail         TEXT,
        robots_decision      TEXT,
        crawler_version      TEXT
    )
    """,
    "CREATE INDEX idx_url_checks_url ON url_checks(url_hash, checked_at)",
    # -- Domain side -------------------------------------------------------
    """
    CREATE TABLE domains (
        domain_id             INTEGER PRIMARY KEY,
        registrable_domain    TEXT NOT NULL UNIQUE,
        public_suffix         TEXT,
        -- blogspot.com / github.io / wordpress.com: a subdomain here is a page,
        -- not an acquireable asset, and must never reach an export.
        is_user_content_suffix INTEGER NOT NULL DEFAULT 0,
        is_resolver           INTEGER NOT NULL DEFAULT 0,
        state                 TEXT NOT NULL DEFAULT 'unknown',
        terminal              INTEGER NOT NULL DEFAULT 0,
        first_seen            TEXT NOT NULL,
        last_checked          TEXT,
        next_check_at         TEXT,
        wiki_page_count       INTEGER NOT NULL DEFAULT 0,
        wiki_link_count       INTEGER NOT NULL DEFAULT 0,
        url_count             INTEGER NOT NULL DEFAULT 0,
        candidate_score       REAL,
        score_explanation     TEXT
    )
    """,
    "CREATE INDEX idx_domains_state ON domains(state)",
    "CREATE INDEX idx_domains_due ON domains(terminal, next_check_at)",
    """
    CREATE TABLE domain_checks (
        id                 INTEGER PRIMARY KEY,
        domain_id          INTEGER NOT NULL,
        checked_at         TEXT NOT NULL,
        dns_status         TEXT,
        a_records          TEXT,
        ns_records         TEXT,
        -- False here means an `unregistered` verdict is NOT permitted: a
        -- single-resolver NXDOMAIN would fabricate an available domain, the
        -- most expensive error this tool can make (prd.md §13).
        resolvers_agreed   INTEGER NOT NULL DEFAULT 0,
        rdap_status        TEXT,
        rdap_raw           TEXT,
        registrar          TEXT,
        registration_expiry TEXT,
        domain_statuses    TEXT,
        classification     TEXT,
        classifier_version INTEGER,
        latency_ms         INTEGER,
        error_kind         TEXT
    )
    """,
    "CREATE INDEX idx_domain_checks_domain ON domain_checks(domain_id, checked_at)",
    # -- Crawl support -----------------------------------------------------
    """
    CREATE TABLE robots_cache (
        origin      TEXT PRIMARY KEY,
        fetched_at  TEXT NOT NULL,
        expires_at  TEXT NOT NULL,
        http_status INTEGER,
        body        TEXT,
        crawl_delay REAL
    )
    """,
    """
    CREATE TABLE crawl_runs (
        run_id          TEXT PRIMARY KEY,
        kind            TEXT NOT NULL,
        started_at      TEXT NOT NULL,
        ended_at        TEXT,
        -- Secrets are redacted before anything is written here (config.redact).
        args            TEXT,
        counts          TEXT,
        config_hash     TEXT,
        crawler_version TEXT,
        outcome         TEXT
    )
    """,
    "CREATE INDEX idx_crawl_runs_kind ON crawl_runs(kind, started_at)",
    """
    CREATE TABLE exports (
        export_id  INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        filter     TEXT,
        row_count  INTEGER NOT NULL DEFAULT 0,
        path       TEXT,
        -- Export is deterministic: same filter + same state => same sha256.
        -- Two exports therefore diff meaningfully, with no extra feature.
        sha256     TEXT
    )
    """,
)

# v1.D. `is_user_content_suffix` overclaimed: it was populated from the PSL's
# *private* section, which is a superset of user-content platforms. Real data
# showed it flagging `wbc.poznan.pl`, `spb.org.ru`, `pdmi.ras.ru` — regional and
# institutional registries, not platforms, and some of them genuinely
# registrable. The column is renamed to say what it actually measures; whether a
# private-suffix domain is acquireable is a scoring question (v1.I), not
# something the PSL can answer.
MIGRATION_2: Final[tuple[str, ...]] = (
    "ALTER TABLE domains RENAME COLUMN is_user_content_suffix TO is_private_suffix",
)

# v1.F. The PRD put `classification` on `url_checks`, but §20 forbids ever
# UPDATE-ing that table — and re-judging stored evidence with an improved
# classifier is the entire point of the design. Those two cannot both hold.
#
# Resolved in favour of the invariant: `url_checks` stays a pure, immutable
# record of what was *observed*, and verdicts move to their own append-only
# table keyed by (check, classifier_version). Re-classification appends rather
# than overwrites, which also makes classifier disagreement auditable — you can
# see exactly which verdicts a rule change flipped, and when.
MIGRATION_3: Final[tuple[str, ...]] = (
    """
    CREATE TABLE url_classifications (
        id                 INTEGER PRIMARY KEY,
        check_id           INTEGER NOT NULL REFERENCES url_checks(id),
        url_hash           TEXT    NOT NULL,
        classified_at      TEXT    NOT NULL,
        classifier_version INTEGER NOT NULL,
        classification     TEXT    NOT NULL,
        reasons            TEXT,
        -- Kept so a later rule change can be evaluated against the evidence
        -- that produced a borderline call, not just its outcome.
        confidence         REAL,
        UNIQUE (check_id, classifier_version)
    )
    """,
    "CREATE INDEX idx_url_class_url ON url_classifications(url_hash, classified_at)",
    "CREATE INDEX idx_url_class_value ON url_classifications(classification)",
    # The observation table no longer carries a verdict.
    "ALTER TABLE url_checks DROP COLUMN classification",
    "ALTER TABLE url_checks DROP COLUMN classifier_version",
    "ALTER TABLE url_checks DROP COLUMN classifier_reasons",
)

# v1.G. The same conflict MIGRATION_3 resolved for URLs applies to domains:
# §20 forbids UPDATE-ing `domain_checks`, so a verdict column there could never
# be revised. Same resolution, for the same reason — symmetry here is not
# tidiness, it is what lets `--reclassify` work uniformly across both halves of
# the pipeline.
MIGRATION_4: Final[tuple[str, ...]] = (
    """
    CREATE TABLE domain_classifications (
        id                 INTEGER PRIMARY KEY,
        check_id           INTEGER NOT NULL REFERENCES domain_checks(id),
        domain_id          INTEGER NOT NULL,
        classified_at      TEXT    NOT NULL,
        classifier_version INTEGER NOT NULL,
        state              TEXT    NOT NULL,
        reasons            TEXT,
        confidence         REAL,
        UNIQUE (check_id, classifier_version)
    )
    """,
    "CREATE INDEX idx_domain_class_domain ON domain_classifications(domain_id, classified_at)",
    "CREATE INDEX idx_domain_class_state ON domain_classifications(state)",
    "ALTER TABLE domain_checks DROP COLUMN classification",
    "ALTER TABLE domain_checks DROP COLUMN classifier_version",
)

# v2.G. Cross-dump-run link transitions. Append-only, like every other
# observation table: a diff is a thing we *saw* between two runs, and
# recomputing it later must never silently rewrite what an earlier comparison
# concluded.
#
# `page_deleted` is deliberately NOT a transition here. A page absent from the
# newer run is indistinguishable from a page that run simply never ingested —
# this tool ingests slices — and the two mean opposite things. Only pages
# present in *both* runs are compared; the rest are counted as not-comparable
# rather than dressed up as a signal.
MIGRATION_5: Final[tuple[str, ...]] = (
    """
    CREATE TABLE link_diffs (
        id          INTEGER PRIMARY KEY,
        url_hash    TEXT    NOT NULL,
        page_id     INTEGER NOT NULL,
        lang        TEXT    NOT NULL DEFAULT 'en',
        from_run    TEXT    NOT NULL,
        to_run      TEXT    NOT NULL,
        -- 'removed' | 'added'
        transition  TEXT    NOT NULL,
        observed_at TEXT    NOT NULL,
        UNIQUE (url_hash, page_id, lang, from_run, to_run, transition)
    )
    """,
    "CREATE INDEX idx_link_diffs_url ON link_diffs(url_hash, transition)",
    "CREATE INDEX idx_link_diffs_runs ON link_diffs(from_run, to_run)",
)

# v2.H. Wikitext of pages enrichment has already decompressed.
#
# Keyed on `(dump_run, page_id, lang)` and never on `ms_offset`. Offset X in one
# run's archive is a different block from offset X in another's, so an
# offset-keyed cache would serve one revision's wikitext for a link recorded
# against another — the exact error `check_dump_runs_agree` exists to prevent,
# except silent. The dump run is part of the identity, not metadata about it.
#
# This is derived, disposable data: every row can be regenerated from the
# archive. `content_bytes` is stored so eviction can bound the table without
# re-measuring, and `last_used` makes that eviction LRU.
MIGRATION_6: Final[tuple[str, ...]] = (
    """
    CREATE TABLE page_cache (
        page_id       INTEGER NOT NULL,
        lang          TEXT    NOT NULL DEFAULT 'en',
        dump_run      TEXT    NOT NULL,
        title         TEXT    NOT NULL,
        wikitext      TEXT    NOT NULL,
        content_bytes INTEGER NOT NULL,
        cached_at     TEXT    NOT NULL,
        last_used     TEXT    NOT NULL,
        PRIMARY KEY (page_id, lang, dump_run)
    )
    """,
    "CREATE INDEX idx_page_cache_lru ON page_cache(last_used)",
)

MIGRATIONS: Final[tuple[tuple[str, ...], ...]] = (
    MIGRATION_1,
    MIGRATION_2,
    MIGRATION_3,
    MIGRATION_4,
    MIGRATION_5,
    MIGRATION_6,
)

LATEST_VERSION: Final = len(MIGRATIONS)

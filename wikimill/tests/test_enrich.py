"""Lazy context enrichment.

The first test in this file is the most important one in the project: on a
subset with no dead links, `enrich` must open neither the archive nor the index
(acceptance criterion 12). Everything else about the cheapest-first ordering is
commentary if that does not hold.
"""

from __future__ import annotations

import bz2
from pathlib import Path

import pytest

from wikimill.config import load
from wikimill.constants import EnrichStatus, UrlState
from wikimill.enrich import runner as enrich_runner
from wikimill.enrich.seek import parse_pages, pages_at, read_block
from wikimill.enrich.select import count_pending, group_by_block, parse_states
from wikimill.enrich.wikitext import LEAD_SECTION, extract, find_link, section_at
from wikimill.errors import DumpError
from wikimill.logging import RunLog
from wikimill.storage import open_db

RUN = "20260701"


def seed(conn, *, url_state: str, url="http://gone.example/a", page_id=10):
    conn.execute(
        "INSERT INTO wiki_pages (page_id, lang, title, ms_offset, dump_run, ingested_at)"
        " VALUES (?,?,?,?,?,?)",
        (page_id, "en", "Anarchism", 570, RUN, "2026-07-25T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO urls (url_hash, url_normalized, normalizer_version, scheme, state,"
        " first_seen) VALUES (?,?,?,?,?,?)",
        ("h1", url, 1, "http", url_state, "2026-07-25T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO external_links (page_id, lang, url_raw, url_hash, dump_run,"
        " first_seen, last_seen) VALUES (?,?,?,?,?,?,?)",
        (page_id, "en", url, "h1", RUN, "2026-07-25T00:00:00+00:00",
         "2026-07-25T00:00:00+00:00"),
    )


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKIMILL_CONTACT", "ops@example.org")
    monkeypatch.delenv("WIKIMILL_DUMPS_DIR", raising=False)
    return load(tmp_path)


@pytest.fixture
def log(tmp_path):
    return RunLog("enrich", tmp_path / "logs", quiet=True)


# -- acceptance criterion 12 ------------------------------------------------


def test_no_dead_links_opens_neither_archive_nor_index(cfg, log, monkeypatch):
    """THE criterion. A healthy corpus must cost one indexed count and nothing
    else — no archive, no index, no decompression."""
    with open_db(cfg.db_path) as conn:
        seed(conn, url_state=UrlState.LIVE)

    def explode(*_a, **_kw):
        raise AssertionError("enrich touched a dump file with nothing to enrich")

    monkeypatch.setattr("wikimill.enrich.seek.find_archive", explode)
    monkeypatch.setattr("wikimill.enrich.seek.read_block", explode)

    stats = enrich_runner.run(cfg, log)
    assert stats.pending == 0
    assert stats.blocks_read == 0
    assert stats.enriched == 0


def test_live_and_healthy_redirects_are_not_trigger_states():
    """Paying to extract context for a working link is precisely the work this
    ordering exists to avoid."""
    defaults = parse_states(None)
    assert UrlState.LIVE not in defaults
    assert UrlState.REDIRECT not in defaults
    assert UrlState.TEMPORARILY_UNAVAILABLE not in defaults


@pytest.mark.parametrize(
    "state",
    [UrlState.HARD_404, UrlState.DNS_FAILURE, UrlState.PARKED, UrlState.FOR_SALE,
     UrlState.UNREGISTERED, UrlState.SOFT_404, UrlState.TLS_FAILURE],
)
def test_interesting_states_do_trigger(state):
    assert state in parse_states(None)


def test_dead_link_makes_work_pending(cfg):
    with open_db(cfg.db_path) as conn:
        seed(conn, url_state=UrlState.HARD_404)
        assert count_pending(conn, parse_states(None)) == 1


def test_explicit_state_list_overrides(cfg):
    with open_db(cfg.db_path) as conn:
        seed(conn, url_state=UrlState.LIVE)
        assert count_pending(conn, ["live"]) == 1


# -- block batching ---------------------------------------------------------


def test_candidates_sharing_a_block_are_grouped():
    """One seek and one decompress must serve every candidate in a block — the
    difference between minutes and hours on a spinning drive."""
    from wikimill.enrich.select import Candidate

    made = [
        Candidate(1, 10, 570, "A", "u", "hard_404"),
        Candidate(2, 12, 570, "B", "u", "hard_404"),
        Candidate(3, 99, 98765, "C", "u", "hard_404"),
    ]
    blocks = group_by_block(made)
    assert len(blocks) == 2
    assert blocks[0][0] == 570 and len(blocks[0][1]) == 2


def test_blocks_are_returned_in_offset_order():
    from wikimill.enrich.select import Candidate

    made = [
        Candidate(1, 1, 900, "C", "u", "hard_404"),
        Candidate(2, 2, 100, "A", "u", "hard_404"),
    ]
    assert [offset for offset, _ in group_by_block(made)] == [100, 900]


# -- multistream seeking ----------------------------------------------------


def make_multistream(path: Path, blocks: list[str]) -> list[int]:
    """Write concatenated bz2 streams; return each stream's byte offset."""
    offsets, data = [], b""
    for block in blocks:
        offsets.append(len(data))
        data += bz2.compress(block.encode("utf-8"))
    path.write_bytes(data)
    return offsets


PAGE_A = (
    "<page><title>Anarchism</title><ns>0</ns><id>12</id>"
    "<revision><text xml:space=\"preserve\">"
    "Intro text [http://gone.example/a Anarchy Archives] more."
    "</text></revision></page>"
)
PAGE_B = (
    "<page><title>Autism</title><ns>0</ns><id>25</id>"
    "<revision><text xml:space=\"preserve\">Other page.</text></revision></page>"
)


def test_reads_only_the_addressed_block(tmp_path):
    archive = tmp_path / "multi.bz2"
    offsets = make_multistream(archive, [PAGE_A, PAGE_B])
    first = read_block(archive, offsets[0]).decode()
    assert "Anarchism" in first
    assert "Autism" not in first  # the following block must not bleed in


def test_second_block_addressable(tmp_path):
    archive = tmp_path / "multi.bz2"
    offsets = make_multistream(archive, [PAGE_A, PAGE_B])
    assert "Autism" in read_block(archive, offsets[1]).decode()


def test_pages_parsed_from_a_block(tmp_path):
    archive = tmp_path / "multi.bz2"
    offsets = make_multistream(archive, [PAGE_A + PAGE_B])
    pages = pages_at(archive, offsets[0])
    assert set(pages) == {12, 25}
    assert pages[12].title == "Anarchism"


def test_offset_past_end_is_a_typed_error(tmp_path):
    archive = tmp_path / "multi.bz2"
    make_multistream(archive, [PAGE_A])
    with pytest.raises(DumpError) as exc:
        read_block(archive, 10_000_000)
    assert exc.value.remediation


def test_offset_not_at_a_stream_boundary_fails_cleanly(tmp_path):
    """A mismatched index and archive must error, not decompress garbage."""
    archive = tmp_path / "multi.bz2"
    make_multistream(archive, [PAGE_A, PAGE_B])
    with pytest.raises(DumpError):
        read_block(archive, 3)


def test_xml_entities_unescaped():
    raw = (
        "<page><title>A&amp;B</title><id>1</id>"
        "<text>x &lt;ref&gt; &amp;amp; y</text></page>"
    ).encode()
    page = next(parse_pages(raw))
    assert page.title == "A&B"
    assert "<ref>" in page.wikitext
    assert "&amp;" in page.wikitext  # an escaped ampersand must not double-decode


# -- wikitext extraction ----------------------------------------------------

ARTICLE = """Lead paragraph with [http://example.com/lead Lead Link].

== History ==
Some history.<ref name="hist">{{cite web|url=http://gone.example/a|title=Gone Page|archive-url=https://web.archive.org/web/2019/http://gone.example/a|archive-date=2019-03-01}}</ref>
That link is {{dead link|date=January 2020}}.

=== Details ===
Deeper text with [http://example.com/deep Deep Link].

== External links ==
* [http://example.com/ext Official site]
"""


def test_section_and_anchor_from_a_bracketed_link():
    ctx = extract(ARTICLE, "http://example.com/deep")
    assert ctx.found
    assert ctx.section == "Details"
    assert ctx.section_level == 3
    assert ctx.anchor_text == "Deep Link"


def test_lead_section_named_not_blank():
    ctx = extract(ARTICLE, "http://example.com/lead")
    assert ctx.section == LEAD_SECTION


def test_citation_inside_a_ref_is_classified_and_named():
    ctx = extract(ARTICLE, "http://gone.example/a")
    assert ctx.link_kind == "citation"
    assert ctx.ref_name == "hist"
    assert ctx.template_name == "cite web"


def test_dead_link_template_detected():
    """Wikipedia's own editors already flagged it — free corroboration."""
    assert extract(ARTICLE, "http://gone.example/a").dead_link_tagged


def test_archive_url_and_date_captured():
    ctx = extract(ARTICLE, "http://gone.example/a")
    assert ctx.archive_url.startswith("https://web.archive.org/")
    assert ctx.archive_date == "2019-03-01"


def test_external_links_section_classified():
    assert extract(ARTICLE, "http://example.com/ext").link_kind == "external_links_section"


def test_context_excerpt_is_bounded():
    ctx = extract(ARTICLE, "http://gone.example/a")
    assert 0 < len(ctx.context_excerpt) <= 300


def test_template_expanded_link_is_not_found_and_that_is_information():
    """No literal occurrence means the URL came from template expansion. The
    link is real; only its surface context is absent."""
    ctx = extract("An article with no such link.", "http://gone.example/a")
    assert not ctx.found
    assert ctx.anchor_text is None


def test_url_matching_tolerates_normalization_differences():
    """The stored URL has been normalized; the wikitext holds whatever an editor
    typed. Matching must survive www/scheme/trailing-slash drift."""
    text = "See [http://www.example.com/page/ Example]."
    assert find_link(text, "https://example.com/page") is not None


def test_section_at_returns_lead_before_any_heading():
    assert section_at([(100, 2, "History")], 5)[0] == LEAD_SECTION


# -- end to end -------------------------------------------------------------


def test_enrich_fills_context_for_a_dead_link(cfg, log, tmp_path):
    archive = cfg.dumps_dir
    archive.mkdir(parents=True, exist_ok=True)
    page = (
        "<page><title>Anarchism</title><id>10</id><revision><text>"
        "== History ==\nText.<ref name=\"a\">{{cite web|url=http://gone.example/a"
        "|title=T}}</ref>"
        "</text></revision></page>"
    )
    offsets = make_multistream(
        archive / f"enwiki-{RUN}-pages-articles-multistream.xml.bz2", [page]
    )
    with open_db(cfg.db_path) as conn:
        seed(conn, url_state=UrlState.HARD_404)
        conn.execute("UPDATE wiki_pages SET ms_offset=?", (offsets[0],))

    stats = enrich_runner.run(cfg, log)
    assert stats.enriched == 1
    with open_db(cfg.db_path) as conn:
        row = conn.execute(
            "SELECT section, link_kind, ref_name, enrich_status FROM external_links"
        ).fetchone()
    assert row["section"] == "History"
    assert row["link_kind"] == "citation"
    assert row["ref_name"] == "a"
    assert row["enrich_status"] == EnrichStatus.DONE


def test_reenrich_is_a_noop(cfg, log, tmp_path):
    archive = cfg.dumps_dir
    archive.mkdir(parents=True, exist_ok=True)
    page = "<page><title>A</title><id>10</id><revision><text>[http://gone.example/a X]</text></revision></page>"
    offsets = make_multistream(
        archive / f"enwiki-{RUN}-pages-articles-multistream.xml.bz2", [page]
    )
    with open_db(cfg.db_path) as conn:
        seed(conn, url_state=UrlState.HARD_404)
        conn.execute("UPDATE wiki_pages SET ms_offset=?", (offsets[0],))

    first = enrich_runner.run(cfg, log)
    second = enrich_runner.run(cfg, log)
    assert first.enriched == 1
    assert second.pending == 0
    assert second.blocks_read == 0  # nothing re-parsed


def test_dry_run_reads_and_writes_nothing(cfg, log):
    archive = cfg.dumps_dir
    archive.mkdir(parents=True, exist_ok=True)
    make_multistream(
        archive / f"enwiki-{RUN}-pages-articles-multistream.xml.bz2", [PAGE_A]
    )
    with open_db(cfg.db_path) as conn:
        seed(conn, url_state=UrlState.HARD_404)
    stats = enrich_runner.run(cfg, log, dry_run=True)
    assert stats.candidates == 1
    assert stats.blocks_read == 0
    with open_db(cfg.db_path) as conn:
        assert conn.execute(
            "SELECT enrich_status FROM external_links"
        ).fetchone()["enrich_status"] == EnrichStatus.PENDING


def test_count_pending_matches_what_select_can_actually_reach(cfg):
    """Regression: `count_pending` omitted the wiki_pages join and the
    `ms_offset` guard that `select()` applies, so a link whose page has no
    offset was counted as pending but never selectable — enrich would open the
    archive for nothing and leave the row pending forever."""
    with open_db(cfg.db_path) as conn:
        seed(conn, url_state=UrlState.HARD_404)
        conn.execute("UPDATE wiki_pages SET ms_offset=NULL")
        states = parse_states(None)
        from wikimill.enrich.select import select as select_candidates
        assert count_pending(conn, states) == len(select_candidates(conn, states))


def test_unreachable_link_does_not_defeat_the_fast_path(cfg, log, monkeypatch):
    """With nothing *reachable* to enrich, the archive must still stay closed."""
    with open_db(cfg.db_path) as conn:
        seed(conn, url_state=UrlState.HARD_404)
        conn.execute("UPDATE wiki_pages SET ms_offset=NULL")

    def explode(*_a, **_kw):
        raise AssertionError("opened the archive for work it cannot do")

    monkeypatch.setattr("wikimill.enrich.seek.find_archive", explode)
    stats = enrich_runner.run(cfg, log)
    assert stats.pending == 0
    assert stats.blocks_read == 0

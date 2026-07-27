"""Heartbeat and report (v3.B/C).

The requirement these serve is narrow: during a long run the operator wants to
know it is moving, and if it is not, what it stopped on. A crawler being polite
and a crawler being wedged produce identical output — nothing — so the only
thing separating them is whether the process says so.

The tests that matter most are therefore the negative ones: a stage that has
stopped moving must read as **stalled**, a stage that finished must never read
as stalled no matter how old it is, and a crash must leave a row saying it
crashed rather than a row that simply went quiet.
"""

from __future__ import annotations

import contextlib

from wikimill import progress as progress_mod
from wikimill import report as report_mod
from wikimill.constants import DomainState
from wikimill.progress import Heartbeat, open_progress_db
from wikimill.storage import open_db

T0 = "2026-07-25T12:00:00+00:00"


def rows(conn):
    return conn.execute("SELECT * FROM run_progress").fetchall()


def beats(tmp_path):
    """The progress file — deliberately separate from the work database."""
    return open_progress_db(tmp_path)


# -- writing ----------------------------------------------------------------


def test_a_stage_announces_itself_before_doing_any_work(tmp_path):
    """The first write is forced. A stage that only appears once it has finished
    an item is invisible for exactly as long as the first item is slow — which
    is when the operator is most likely to be watching."""
    with contextlib.closing(beats(tmp_path)) as conn:
        Heartbeat(conn, "run-1", "crawl", total=100)
        assert len(rows(conn)) == 1
        assert rows(conn)[0]["done"] == 0


def test_progress_accumulates(tmp_path):
    with contextlib.closing(beats(tmp_path)) as conn:
        beat = Heartbeat(conn, "run-1", "crawl", total=10)
        for i in range(4):
            beat.advance(current_item=f"http://x{i}.example/")
        beat.finish()
        row = rows(conn)[0]
        assert row["done"] == 4 and row["total"] == 10


def test_writes_are_throttled(tmp_path):
    """A heartbeat on every item turns an I/O-bound loop into a database-bound
    one. Only the forced writes should reach SQLite in a tight loop."""
    with contextlib.closing(beats(tmp_path)) as conn:
        beat = Heartbeat(conn, "run-1", "crawl", total=10_000)
        before = conn.total_changes
        for _ in range(5_000):
            beat.advance()
        assert conn.total_changes - before == 0, "throttling is not working"


def test_the_final_state_is_never_lost_to_throttling(tmp_path):
    with contextlib.closing(beats(tmp_path)) as conn:
        beat = Heartbeat(conn, "run-1", "crawl", total=3)
        beat.advance(3)          # throttled away
        beat.finish()            # forced
        row = rows(conn)[0]
        assert row["done"] == 3
        assert row["finished_at"] is not None


def test_one_row_per_run_and_stage(tmp_path):
    """Upsert, not append: this table answers a question about *now*."""
    with contextlib.closing(beats(tmp_path)) as conn:
        beat = Heartbeat(conn, "run-1", "crawl", total=5)
        for _ in range(3):
            beat.touch()
            beat._last_write = 0    # defeat throttling
        beat.finish()
        assert len(rows(conn)) == 1


def test_bookkeeping_failure_never_kills_the_work(tmp_path):
    """The crawl matters; its progress row does not. A broken heartbeat must not
    take down a run that is otherwise working."""
    with contextlib.closing(beats(tmp_path)) as conn:
        beat = Heartbeat(conn, "run-1", "crawl", total=5)
        conn.execute("DROP TABLE run_progress")
        beat.advance(current_item="x")   # must not raise
        beat.finish()


# -- liveness ---------------------------------------------------------------


def make(conn, *, stage="crawl", done=5, total=10, updated, finished=None,
         outcome=None, current="http://slow.example/"):
    conn.execute(
        "INSERT INTO run_progress (run_id, stage, phase, done, total, current_item, "
        " started_at, updated_at, finished_at, outcome) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("run-1", stage, "crawling", done, total, current, T0, updated, finished,
         outcome),
    )


def test_a_stage_that_stopped_moving_reads_as_stalled(tmp_path):
    """The state this whole module exists to expose."""
    with contextlib.closing(beats(tmp_path)) as conn:
        make(conn, updated=T0)
        view = progress_mod.snapshot(conn, "2026-07-25T12:10:00+00:00")[0]
        assert view.stalled and view.status == "stalled"
        assert view.current_item == "http://slow.example/"


def test_a_recently_active_stage_is_running_not_stalled(tmp_path):
    with contextlib.closing(beats(tmp_path)) as conn:
        make(conn, updated="2026-07-25T12:00:50+00:00")
        view = progress_mod.snapshot(conn, "2026-07-25T12:01:00+00:00")[0]
        assert view.running and not view.stalled
        assert view.status == "running"


def test_a_finished_stage_is_never_stalled_however_old(tmp_path):
    """Otherwise every completed run in the database eventually turns red and
    the signal becomes noise the operator learns to ignore."""
    with contextlib.closing(beats(tmp_path)) as conn:
        make(conn, updated=T0, finished=T0, outcome="ok")
        view = progress_mod.snapshot(conn, "2027-01-01T00:00:00+00:00")[0]
        assert not view.stalled and not view.running
        assert view.status == "ok"


def test_a_crash_leaves_a_row_saying_it_crashed(tmp_path):
    """Not a row that merely stopped moving and has to be diagnosed by silence."""
    with contextlib.closing(beats(tmp_path)) as conn:
        try:
            with Heartbeat(conn, "run-1", "crawl", total=10) as beat:
                beat.advance()
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        row = rows(conn)[0]
        assert row["outcome"] == "failed"
        assert row["finished_at"] is not None
        assert "boom" in row["note"]


def test_live_lists_only_unfinished_stages(tmp_path):
    with contextlib.closing(beats(tmp_path)) as conn:
        make(conn, stage="crawl", updated=T0)
        make(conn, stage="check", updated=T0, finished=T0, outcome="ok")
        assert [v.stage for v in progress_mod.live(conn, T0)] == ["crawl"]


# -- derived numbers --------------------------------------------------------


def test_percent_rate_and_eta_come_from_real_elapsed_time(tmp_path):
    with contextlib.closing(beats(tmp_path)) as conn:
        # 25 of 100 done over 50 seconds -> 0.5/s -> 150s remaining.
        make(conn, done=25, total=100, updated="2026-07-25T12:00:50+00:00")
        view = progress_mod.snapshot(conn, "2026-07-25T12:00:55+00:00")[0]
        assert view.percent == 25.0
        assert abs(view.rate_per_second - 0.5) < 0.01
        assert abs(view.eta_seconds - 150.0) < 1.0


def test_an_unknown_total_yields_no_percent_or_eta(tmp_path):
    """Better to show nothing than to invent a denominator."""
    with contextlib.closing(beats(tmp_path)) as conn:
        make(conn, done=5, total=None, updated=T0)
        view = progress_mod.snapshot(conn, T0)[0]
        assert view.percent is None and view.eta_seconds is None


# -- the report page --------------------------------------------------------


def render(conn, **kwargs):
    return report_mod.render(report_mod.collect(conn, **kwargs))


def test_an_empty_database_still_renders(tmp_path):
    """A fresh checkout must produce a readable page, not a traceback."""
    with open_db(tmp_path / "w.db") as conn:
        page = render(conn)
    assert "<!doctype html>" in page
    assert "No candidates yet" in page


def test_the_page_loads_nothing_from_the_network(tmp_path):
    """The hard constraint: it opens from outputs/ with the wifi off, and still
    works in five years when today's CDN is gone."""
    with open_db(tmp_path / "w.db") as conn:
        page = render(conn)
    for tag in ("<script src", "<link ", "<img ", "@import", "//cdn", "fonts.g"):
        assert tag not in page, f"page references {tag!r}"
    assert "<style>" in page and "<script>" in page


def test_hostile_text_from_the_corpus_is_escaped(tmp_path):
    """Domain names and article titles come from Wikipedia, which anyone may
    edit. They are untrusted text and must never become markup."""
    with open_db(tmp_path / "w.db") as conn:
        conn.execute(
            "INSERT INTO domains (registrable_domain, public_suffix, state, "
            "first_seen, wiki_page_count, wiki_link_count, url_count) "
            "VALUES (?,?,?,?,?,?,?)",
            ("<script>alert(1)</script>.com", "com", DomainState.UNREGISTERED,
             T0, 1, 1, 1),
        )
        page = render(conn)
    assert "<script>alert(1)</script>.com" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_candidates_carry_filter_attributes(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        conn.execute(
            "INSERT INTO domains (registrable_domain, public_suffix, state, "
            "first_seen, wiki_page_count, wiki_link_count, url_count, "
            "candidate_score) VALUES (?,?,?,?,?,?,?,?)",
            ("gone.example", "example", DomainState.UNREGISTERED, T0, 4, 4, 1, 61.0),
        )
        page = render(conn)
    assert "data-state='unregistered'" in page
    assert "data-domain='gone.example'" in page
    assert "data-search=" in page


def test_a_stalled_stage_is_visible_on_the_page(tmp_path):
    """The operator's actual question, answered without reading a log."""
    with contextlib.closing(beats(tmp_path)) as beat_conn:
        make(beat_conn, updated=T0, current="http://hanging.example/x")
        stages = progress_mod.snapshot(beat_conn, "2026-07-25T12:30:00+00:00")
    with open_db(tmp_path / "w.db") as conn:
        data = report_mod.collect(conn)
    data.stages = stages
    page = report_mod.render(data)
    assert "stalled" in page
    assert "no progress for" in page
    assert "hanging.example" in page


def test_the_page_only_auto_refreshes_while_something_runs(tmp_path):
    """A page that reloads forever fights the operator trying to read it."""
    with open_db(tmp_path / "w.db") as conn:
        data = report_mod.collect(conn)

    with contextlib.closing(beats(tmp_path)) as beat_conn:
        make(beat_conn, updated=T0, finished=T0, outcome="ok")
        data.stages = progress_mod.snapshot(beat_conn, T0)
        assert "http-equiv='refresh'" not in report_mod.render(data, refresh_seconds=10)

        beat_conn.execute("DELETE FROM run_progress")
        make(beat_conn, updated=T0)
        data.stages = progress_mod.snapshot(beat_conn, T0)
        assert "http-equiv='refresh'" in report_mod.render(data, refresh_seconds=10)


def test_the_report_survives_a_missing_progress_file(tmp_path):
    """A fresh checkout has never run a stage. That is not an error, and the
    page must still render rather than blaming the operator for it."""
    with open_db(tmp_path / "w.db") as conn:
        page = render(conn)
    assert "No stage has reported progress yet" in page


def test_truncation_is_stated_rather_than_silent(tmp_path):
    """A page showing 3,000 of 50,000 rows without saying so reads as complete."""
    with open_db(tmp_path / "w.db") as conn:
        for i in range(5):
            conn.execute(
                "INSERT INTO domains (registrable_domain, public_suffix, state, "
                "first_seen, wiki_page_count, wiki_link_count, url_count) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"d{i}.example", "example", DomainState.UNREGISTERED, T0, 1, 1, 1),
            )
        page = report_mod.render(report_mod.collect(conn, limit=2))
    assert "showing the top 2" in page
    assert "not silently dropped" in page


def test_the_licence_notice_travels_with_the_page(tmp_path):
    """Anchor text and article titles are Wikipedia excerpts here exactly as in
    the CSV, so the same obligation applies (prd.md §17)."""
    with open_db(tmp_path / "w.db") as conn:
        page = render(conn)
    assert "CC BY-SA" in page
    assert "creativecommons.org/licenses/by-sa/4.0" in page


def test_the_funnel_reports_every_stage(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        page = render(conn)
    for label in ("external links", "distinct URLs", "registrable domains",
                  "URLs crawled", "candidates"):
        assert label in page

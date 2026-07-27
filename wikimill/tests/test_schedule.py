"""The recheck scheduler (v2.E, prd.md §12).

Three properties, in descending order of how much damage getting them wrong
does:

1. **Escalation protects the far end.** A host that is down stays down; retrying
   it hourly forever is a politeness failure aimed at the site least able to
   absorb it.
2. **Ordering is the scheduler.** At 1.3M URLs every run is `--limit`-capped, so
   what comes *first* decides what is ever seen at all.
3. **The queue is observable without running it.** Asking "is there anything to
   do?" must not cost requests to real hosts.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from wikimill import schedule as schedule_mod
from wikimill import score as score_mod
from wikimill.classify import state as classify_state
from wikimill.cli import app
from wikimill.constants import DomainState, UrlState
from wikimill.crawl.runner import select_due
from wikimill.domain.runner import select_domains
from wikimill.policy import POLICY_FILENAME, load
from wikimill.storage import open_db

NOW = "2026-07-25T00:00:00+00:00"
HOUR = 3_600
DAY = 86_400
runner = CliRunner()


def put_url(conn, *, url_hash, state, next_check_at=NOW, terminal=0, last_checked=NOW,
            domain_id=None):
    conn.execute(
        "INSERT INTO urls (url_hash, url_normalized, normalizer_version, domain_id, "
        "scheme, state, first_seen, last_checked, next_check_at, terminal) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (url_hash, f"http://{url_hash}.example/", 1, domain_id, "http", state, NOW,
         last_checked, next_check_at, terminal),
    )


def put_domain(conn, *, domain, state, pages=1, next_check_at=NOW, terminal=0,
               last_checked=NOW):
    conn.execute(
        "INSERT INTO domains (registrable_domain, public_suffix, state, first_seen, "
        "last_checked, next_check_at, terminal, wiki_page_count, wiki_link_count, "
        "url_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (domain, "com", state, NOW, last_checked, next_check_at, terminal, pages,
         pages, 1),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


# -- 1. escalation ----------------------------------------------------------


def test_transient_backoff_doubles_from_one_hour():
    seconds = [
        classify_state.recheck_seconds(UrlState.TEMPORARILY_UNAVAILABLE, repeats=n)
        for n in range(5)
    ]
    assert seconds[:5] == [HOUR, 2 * HOUR, 4 * HOUR, 8 * HOUR, 16 * HOUR]


def test_transient_requeues_weekly_instead_of_retrying_at_the_cap():
    """The step that matters: past the cap it does NOT settle at 24h and keep
    knocking daily — it leaves the fast lane entirely.

    A host down for a week would otherwise collect 168 requests from us.
    """
    # repeats=5 would double to 32h, past the 24h cap.
    assert classify_state.recheck_seconds(
        UrlState.TEMPORARILY_UNAVAILABLE, repeats=5
    ) == 7 * DAY
    # And it stays there, rather than growing without bound.
    assert classify_state.recheck_seconds(
        UrlState.TEMPORARILY_UNAVAILABLE, repeats=20
    ) == 7 * DAY


def test_hard_404_backoff_is_capped():
    assert classify_state.recheck_seconds(UrlState.HARD_404, repeats=30) == 180 * DAY


def test_a_fresh_verdict_never_escalates():
    """`repeats=0` is the first sighting — it must use the plain cadence."""
    for state in (UrlState.HARD_404, UrlState.TEMPORARILY_UNAVAILABLE, UrlState.PARKED):
        assert classify_state.recheck_seconds(state, repeats=0) == \
               classify_state.recheck_seconds(state)


def test_valuable_states_do_not_back_off(tmp_path):
    """`unregistered` and `for_sale` are worth *more* when seen repeatedly, so
    escalation must not touch them — the freshness is the product."""
    for state in (UrlState.UNREGISTERED, UrlState.FOR_SALE, UrlState.PARKED):
        assert classify_state.recheck_seconds(state, repeats=6) == \
               classify_state.recheck_seconds(state, repeats=0)


def test_escalation_ceilings_are_tunable(tmp_path):
    (tmp_path / POLICY_FILENAME).write_text(
        "[classify]\ntransient_cap_seconds = 7200\ntransient_requeue_seconds = 99\n",
        encoding="utf-8",
    )
    policy = load(tmp_path)
    assert classify_state.recheck_seconds(
        UrlState.TEMPORARILY_UNAVAILABLE, repeats=3, policy=policy
    ) == 99


# -- 2. ordering ------------------------------------------------------------


def test_due_urls_are_ordered_by_what_a_recheck_is_worth(tmp_path):
    """With a limit of 1, the `for_sale` record must win — even though the
    `live` one has been waiting longer."""
    with open_db(tmp_path / "w.db") as conn:
        put_url(conn, url_hash="live", state=UrlState.LIVE,
                last_checked="2020-01-01T00:00:00+00:00")
        put_url(conn, url_hash="sale", state=UrlState.FOR_SALE,
                last_checked="2026-07-24T00:00:00+00:00")

        picked = select_due(conn, 1, False, load(tmp_path))
        assert [t.url_hash for t in picked] == ["sale"]


def test_never_checked_urls_still_lead(tmp_path):
    """A URL with no observation at all is the cheapest information available,
    so it outranks even a high-value recheck."""
    with open_db(tmp_path / "w.db") as conn:
        put_url(conn, url_hash="sale", state=UrlState.FOR_SALE)
        put_url(conn, url_hash="fresh", state=UrlState.UNCLASSIFIED,
                last_checked=None, next_check_at=None)

        picked = select_due(conn, 1, False, load(tmp_path))
        assert [t.url_hash for t in picked] == ["fresh"]


def test_url_priority_follows_the_operators_weights(tmp_path):
    """Re-weight `[scoring]` and the queue order follows — one ranking, not two."""
    with open_db(tmp_path / "w.db") as conn:
        put_url(conn, url_hash="live", state=UrlState.LIVE)
        put_url(conn, url_hash="sale", state=UrlState.FOR_SALE)

        (tmp_path / POLICY_FILENAME).write_text(
            "[scoring.url_death_points]\nlive = 99\nfor_sale = 1\n", encoding="utf-8"
        )
        picked = select_due(conn, 1, False, load(tmp_path))
        assert [t.url_hash for t in picked] == ["live"]


def test_expiring_domains_outrank_merely_popular_ones(tmp_path):
    """The bug this ordering fixes: `wiki_page_count DESC` alone let a
    heavily-cited `unknown` domain bury the one state whose whole point is that
    its window is closing."""
    with open_db(tmp_path / "w.db") as conn:
        put_domain(conn, domain="popular.com", state=DomainState.UNKNOWN, pages=5_000)
        put_domain(conn, domain="expiring.com", state=DomainState.EXPIRING, pages=1)

        # Explicit states: the default branch additionally requires an
        # interesting URL, which is a selection question, not an ordering one.
        picked = select_domains(
            conn, limit=1, states=[DomainState.UNKNOWN, DomainState.EXPIRING],
            force=False, policy=load(tmp_path),
        )
        assert [t.domain for t in picked] == ["expiring.com"]


def test_citation_count_still_breaks_ties_within_a_state(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        put_domain(conn, domain="quiet.com", state=DomainState.PARKED, pages=2)
        put_domain(conn, domain="loud.com", state=DomainState.PARKED, pages=900)

        picked = select_domains(
            conn, limit=1, states=[DomainState.PARKED], force=False,
            policy=load(tmp_path),
        )
        assert [t.domain for t in picked] == ["loud.com"]


def test_priority_case_binds_every_value():
    """The column name is interpolated; the weights must never be."""
    sql, params = score_mod.priority_case("u.state", {"a": 1, "b": 2})
    assert sql.count("?") == len(params) == 4
    assert "1" not in sql and "2" not in sql


def test_priority_case_survives_empty_weights():
    sql, params = score_mod.priority_case("u.state", {})
    assert sql == "0" and params == []


# -- terminal protection ----------------------------------------------------


def test_terminal_urls_are_excluded_until_forced(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        put_url(conn, url_hash="dead", state=UrlState.HARD_404, terminal=1)
        assert select_due(conn, None, False, load(tmp_path)) == []
        assert len(select_due(conn, None, True, load(tmp_path))) == 1


def test_not_yet_due_urls_are_excluded_until_forced(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        put_url(conn, url_hash="later", state=UrlState.LIVE,
                next_check_at="2099-01-01T00:00:00+00:00")
        assert select_due(conn, None, False, load(tmp_path)) == []
        assert len(select_due(conn, None, True, load(tmp_path))) == 1


# -- 3. observability -------------------------------------------------------


def test_snapshot_buckets_are_disjoint_and_total(tmp_path):
    """Every record lands in exactly one bucket — otherwise the numbers are
    decoration, and an operator cannot reason from them."""
    with open_db(tmp_path / "w.db") as conn:
        put_url(conn, url_hash="never", state=UrlState.UNCLASSIFIED, next_check_at=None)
        put_url(conn, url_hash="due", state=UrlState.PARKED,
                next_check_at="2020-01-01T00:00:00+00:00")
        put_url(conn, url_hash="soon", state=UrlState.LIVE,
                next_check_at="2026-07-27T00:00:00+00:00")
        put_url(conn, url_hash="later", state=UrlState.LIVE,
                next_check_at="2099-01-01T00:00:00+00:00")
        put_url(conn, url_hash="done", state=UrlState.HARD_404, terminal=1)

        b = schedule_mod.snapshot(conn, NOW)["urls"]
        assert (b.never, b.due, b.soon, b.later, b.terminal) == (1, 1, 1, 1, 1)
        assert b.total == 5
        assert b.actionable == 2


def test_snapshot_names_which_states_are_due(tmp_path):
    """A bare count is not actionable: three due `for_sale` records justify a
    run that a hundred thousand due `live` ones do not."""
    with open_db(tmp_path / "w.db") as conn:
        put_url(conn, url_hash="a", state=UrlState.FOR_SALE,
                next_check_at="2020-01-01T00:00:00+00:00")
        put_url(conn, url_hash="b", state=UrlState.LIVE,
                next_check_at="2020-01-01T00:00:00+00:00")

        b = schedule_mod.snapshot(conn, NOW)["urls"]
        assert b.due_by_state == {UrlState.FOR_SALE: 1, UrlState.LIVE: 1}


def test_snapshot_excludes_terminal_records_from_due(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        put_url(conn, url_hash="dead", state=UrlState.HARD_404,
                next_check_at="2020-01-01T00:00:00+00:00", terminal=1)
        b = schedule_mod.snapshot(conn, NOW)["urls"]
        assert b.due == 0 and b.terminal == 1 and b.due_by_state == {}


def test_snapshot_covers_both_queues(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        assert set(schedule_mod.snapshot(conn, NOW)) == {"urls", "domains"}


# -- the CLI surface --------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKIMILL_CONTACT", "ops@example.org")
    monkeypatch.setattr("wikimill.config.repo_root", lambda: tmp_path)


def test_stats_due_reports_the_schedule(tmp_path):
    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        put_url(conn, url_hash="due", state=UrlState.PARKED,
                next_check_at="2020-01-01T00:00:00+00:00")

    result = runner.invoke(app, ["stats", "--due"])
    assert result.exit_code == 0
    assert "recheck schedule" in result.stdout
    assert "due now" in result.stdout
    assert UrlState.PARKED in result.stdout


def test_stats_without_due_says_nothing_about_the_schedule(tmp_path):
    """`--due` is opt-in — plain `stats` must not grow a new section."""
    result = runner.invoke(app, ["stats"])
    assert result.exit_code == 0
    assert "recheck schedule" not in result.stdout


def test_stats_due_json_is_machine_readable(tmp_path):
    with open_db(tmp_path / "state" / "wikimill.db") as conn:
        put_url(conn, url_hash="due", state=UrlState.PARKED,
                next_check_at="2020-01-01T00:00:00+00:00")

    result = runner.invoke(app, ["stats", "--due", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schedule"]["urls"]["due_now"] == 1
    assert payload["schedule"]["urls"]["due_by_state"] == {UrlState.PARKED: 1}
    assert "domains" in payload["schedule"]


def test_stats_json_omits_schedule_unless_asked(tmp_path):
    payload = json.loads(runner.invoke(app, ["stats", "--json"]).stdout)
    assert "schedule" not in payload

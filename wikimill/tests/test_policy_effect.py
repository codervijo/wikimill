"""Does editing `wikimill.toml` actually change what the tool does?

`test_policy.py` proves the file *parses* into a `Policy`. That is a weaker
claim than it looks. A config that loads perfectly and is then ignored by every
consumer would pass every test in that file — and the operator would edit a
value, re-run, see no difference, and have no way to tell whether the tuning was
wrong or simply unread. That is the failure this file exists to catch.

So each test here writes a real toml to a real root, calls `load()`, hands the
result to the real consumer, and asserts the **outcome** differs from the
default one. No test asserts a field's value; that is `test_policy.py`'s job.
"""

from __future__ import annotations

import sqlite3

import pytest
from typer.testing import CliRunner

from wikimill import export as export_mod
from wikimill import score as score_mod
from wikimill.classify import rules as classify_rules
from wikimill.classify import state as classify_state
from wikimill.cli import app
from wikimill.constants import DomainState, UrlState
from wikimill.domain import runner as domain_runner
from wikimill.enrich.select import count_pending, parse_states
from wikimill.policy import POLICY_FILENAME, load
from wikimill.storage import open_db

NOW = "2026-07-25T00:00:00+00:00"
runner = CliRunner()


def tuned(root, body: str):
    """Write a policy file and load it the way the CLI does."""
    (root / POLICY_FILENAME).write_text(body, encoding="utf-8")
    return load(root)


def seed(conn, *, domain, state=DomainState.UNREGISTERED, pages=3,
         url_state=UrlState.DNS_FAILURE, kind="citation", enriched=True):
    """One domain with one URL and `pages` citing articles."""
    conn.execute(
        "INSERT INTO domains (registrable_domain, public_suffix, is_private_suffix, "
        "state, first_seen, last_checked, wiki_page_count, wiki_link_count, url_count) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (domain, "com", 0, state, NOW, NOW, pages, pages, 1),
    )
    domain_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO urls (url_hash, url_normalized, normalizer_version, domain_id, "
        "scheme, state, first_seen) VALUES (?,?,?,?,?,?,?)",
        (f"h-{domain}", f"http://{domain}/a", 1, domain_id, "http", url_state, NOW),
    )
    page_id = abs(hash(domain)) % 100_000
    conn.execute(
        "INSERT OR IGNORE INTO wiki_pages (page_id, lang, title, ms_offset, dump_run, "
        "ingested_at) VALUES (?,?,?,?,?,?)",
        (page_id, "en", f"Article about {domain}", 570, "20260701", NOW),
    )
    conn.execute(
        "INSERT INTO external_links (page_id, lang, url_raw, url_hash, dump_run, "
        "first_seen, last_seen, anchor_text, link_kind, enrich_status, "
        "dead_link_tagged) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (page_id, "en", f"http://{domain}/a", f"h-{domain}", "20260701", NOW, NOW,
         "The Source" if enriched else None, kind if enriched else None,
         "done" if enriched else "pending", 0),
    )
    return domain_id


# -- scoring ----------------------------------------------------------------


def score_of(conn, domain, policy):
    row = conn.execute(
        "SELECT * FROM domains WHERE registrable_domain=?", (domain,)
    ).fetchone()
    states, kinds = score_mod.evidence_for(conn, row["domain_id"])
    return score_mod.score_domain(row, states, kinds, policy).total


def test_scoring_weights_from_toml_change_the_rank_order(tmp_path):
    """The strongest form of the claim: an edit reverses which domain wins.

    A weight change that only moves absolute totals proves nothing — every score
    shifting together leaves the export identical. Reversing the order is what
    the operator is actually buying.
    """
    with open_db(tmp_path / "w.db") as conn:
        seed(conn, domain="few-citations.com", state=DomainState.UNREGISTERED, pages=1)
        seed(conn, domain="many-citations.com", state=DomainState.PARKED, pages=8)

        default = load(tmp_path)
        assert score_of(conn, "few-citations.com", default) > score_of(
            conn, "many-citations.com", default
        ), "default weights should favour acquireability over popularity"

        # Now tell it popularity matters more than availability.
        policy = tuned(tmp_path, """
[scoring]
citation_points_per_page = 12
citation_points_cap = 200

[scoring.state_points]
unregistered = 1
""")
        assert score_of(conn, "many-citations.com", policy) > score_of(
            conn, "few-citations.com", policy
        ), "the toml edit did not reach score_domain"


def test_private_suffix_penalty_is_tunable(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        seed(conn, domain="blog.example.com")
        conn.execute("UPDATE domains SET is_private_suffix=1")
        default = load(tmp_path)
        harsh = tuned(tmp_path, "[scoring]\nprivate_suffix_penalty = -500\n")
        assert score_of(conn, "blog.example.com", harsh) < score_of(
            conn, "blog.example.com", default
        )


# -- markers ----------------------------------------------------------------


def test_a_new_parking_phrase_changes_the_verdict(tmp_path):
    """Marker lists are the tuning surface that drifts fastest — a provider can
    change its template between runs. Adding one phrase must be enough."""
    obs = classify_rules.Observation(
        url="http://x.example/",
        http_status=200,
        page_title="Welcome",
        evidence="this name is resting with acme registrar",
        content_length=4096,
    )
    assert classify_rules.classify(obs, load(tmp_path)).classification == UrlState.LIVE

    policy = tuned(tmp_path, """
[markers]
parking_phrases = ["this name is resting with"]
""")
    assert classify_rules.classify(obs, policy).classification == UrlState.PARKED


def test_removing_a_marker_stops_the_verdict(tmp_path):
    """Tightening must work as well as loosening — that is how a false positive
    the operator reports gets retired without a code change."""
    obs = classify_rules.Observation(
        url="http://x.example/",
        http_status=200,
        page_title="Welcome",
        evidence="this domain is parked",
        content_length=4096,
    )
    assert classify_rules.classify(obs, load(tmp_path)).classification == UrlState.PARKED

    policy = tuned(tmp_path, '[markers]\nparking_phrases = ["something else entirely"]\n')
    assert classify_rules.classify(obs, policy).classification == UrlState.LIVE


def test_thin_body_threshold_moves_the_soft_404_line(tmp_path):
    """A 200 with one weak title hit and a smallish body: soft-404 or not
    depends entirely on where the thin-body line sits."""
    obs = classify_rules.Observation(
        url="http://x.example/deep/page",
        http_status=200,
        page_title="Untitled",
        evidence="nothing interesting here",
        content_length=2048,
    )
    assert classify_rules.classify(obs, load(tmp_path)).classification == UrlState.LIVE

    policy = tuned(tmp_path, """
[classify]
thin_body_bytes = 4096

[markers]
soft_404_title_markers = ["untitled"]
""")
    assert classify_rules.classify(obs, policy).classification == UrlState.SOFT_404


# -- recheck cadence --------------------------------------------------------


def test_recheck_cadence_from_toml_is_used(tmp_path):
    default = load(tmp_path)
    policy = tuned(tmp_path, """
[classify.recheck_seconds]
parked = 60
""")
    assert classify_state.recheck_seconds(UrlState.PARKED, policy=policy) == 60
    assert classify_state.recheck_seconds(
        UrlState.PARKED, policy=default
    ) != 60


def test_hard_404_terminal_threshold_is_tunable(tmp_path, clean):
    """How many confirmations before a URL is retired for good."""
    policy = tuned(tmp_path, "[classify]\nhard_404_confirmations = 1\n")
    assert classify_state.is_terminal(UrlState.HARD_404, 1, policy)
    assert not classify_state.is_terminal(UrlState.HARD_404, 1, load(clean))


# -- stage selection --------------------------------------------------------


def test_enrich_trigger_states_come_from_toml(tmp_path, clean):
    policy = tuned(tmp_path, '[enrich]\nurl_trigger_states = ["parked"]\n')
    assert parse_states(None, policy) == ["parked"]
    assert parse_states(None, load(clean)) != ["parked"]


def test_enrich_selection_narrows_when_triggers_narrow(tmp_path):
    """`count_pending` is the fast path the whole pipeline ordering rests on, so
    it has to honour the same trigger set `select` does."""
    with open_db(tmp_path / "w.db") as conn:
        seed(conn, domain="dead.com", state=DomainState.UNKNOWN,
             url_state=UrlState.DNS_FAILURE, enriched=False)
        seed(conn, domain="parked.com", state=DomainState.UNKNOWN,
             url_state=UrlState.PARKED, enriched=False)

        wide = load(tmp_path)
        assert count_pending(
            conn, parse_states(None, wide), policy=wide
        ) == 2

        narrow = tuned(tmp_path, '[enrich]\nurl_trigger_states = ["parked"]\n')
        assert count_pending(
            conn, parse_states(None, narrow), policy=narrow
        ) == 1


def test_check_interesting_states_narrow_domain_selection(tmp_path):
    with open_db(tmp_path / "w.db") as conn:
        seed(conn, domain="dnsfail.com", state=DomainState.UNKNOWN,
             url_state=UrlState.DNS_FAILURE)
        seed(conn, domain="parked.com", state=DomainState.UNKNOWN,
             url_state=UrlState.PARKED)

        wide = load(tmp_path)
        assert len(domain_runner.select_domains(
            conn, limit=None, states=None, force=True, policy=wide
        )) == 2

        narrow = tuned(tmp_path, '[check]\ninteresting_url_states = ["dns_failure"]\n')
        picked = domain_runner.select_domains(
            conn, limit=None, states=None, force=True, policy=narrow
        )
        assert [t.domain for t in picked] == ["dnsfail.com"]


# -- export -----------------------------------------------------------------


@pytest.fixture
def clean(tmp_path):
    """A root with no policy file — the built-in defaults, uncontaminated."""
    root = tmp_path / "pristine"
    root.mkdir()
    return root


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKIMILL_CONTACT", "ops@example.org")
    monkeypatch.setattr("wikimill.config.repo_root", lambda: tmp_path)
    return tmp_path


def test_export_min_pages_from_toml_filters_rows(isolated):
    """End-to-end through the real command: no flag, only the file."""
    root = isolated
    with open_db(root / "state" / "wikimill.db") as conn:
        seed(conn, domain="cited-once.com", pages=1)
        seed(conn, domain="cited-often.com", pages=9)

    assert runner.invoke(app, ["export", "--format", "jsonl"]).exit_code == 0
    with open_db(root / "state" / "wikimill.db") as conn:
        assert export_mod.collect(conn, [DomainState.UNREGISTERED], 1).__len__() == 2

    (root / POLICY_FILENAME).write_text("[export]\nmin_pages = 5\n", encoding="utf-8")
    result = runner.invoke(app, ["export", "--format", "jsonl"])
    assert result.exit_code == 0
    lines = [
        line for line in
        (root / "outputs" / "candidates.jsonl").read_text().splitlines()
        if line.strip()
    ]
    domains = {line for line in lines if "cited-once.com" in line}
    assert not domains, "min_pages=5 in the toml did not filter the 1-citation domain"
    assert any("cited-often.com" in line for line in lines)


def test_export_candidate_states_from_toml_filters_rows(isolated):
    root = isolated
    with open_db(root / "state" / "wikimill.db") as conn:
        seed(conn, domain="gone.com", state=DomainState.UNREGISTERED)
        seed(conn, domain="onsale.com", state=DomainState.FOR_SALE)

    (root / POLICY_FILENAME).write_text(
        '[export]\ncandidate_states = ["for_sale"]\n', encoding="utf-8"
    )
    assert runner.invoke(app, ["export", "--format", "jsonl"]).exit_code == 0
    body = (root / "outputs" / "candidates.jsonl").read_text()
    assert "onsale.com" in body
    assert "gone.com" not in body


def test_a_cli_flag_still_beats_the_toml(isolated):
    """Precedence is CLI > env > toml > default (policy.py). The file must not
    become a way to silently override what the operator just typed."""
    root = isolated
    with open_db(root / "state" / "wikimill.db") as conn:
        seed(conn, domain="cited-once.com", pages=1)

    (root / POLICY_FILENAME).write_text("[export]\nmin_pages = 5\n", encoding="utf-8")
    assert runner.invoke(
        app, ["export", "--format", "jsonl", "--min-pages", "1"]
    ).exit_code == 0
    assert "cited-once.com" in (root / "outputs" / "candidates.jsonl").read_text()


# -- versioning -------------------------------------------------------------


def test_a_classification_edit_bumps_the_recorded_version(tmp_path):
    """The whole point of fingerprinting: nobody has to remember to bump it."""
    base = load(tmp_path).effective_classifier_version
    tightened = tuned(tmp_path, '[markers]\nfor_sale_phrases = ["buy this domain"]\n')
    assert tightened.effective_classifier_version != base


def test_a_presentation_edit_does_not_bump_the_version(tmp_path):
    """`min_pages` changes which rows are shown, not how any was judged. Bumping
    on it would invalidate stored verdicts that are still perfectly good."""
    base = load(tmp_path).effective_classifier_version
    assert tuned(
        tmp_path, "[export]\nmin_pages = 25\n"
    ).effective_classifier_version == base


def test_stored_verdicts_carry_the_tuned_version(tmp_path):
    """A verdict recorded under tuned markers must be attributable to them."""
    policy = tuned(tmp_path, '[markers]\nparking_phrases = ["resting here"]\n')
    with open_db(tmp_path / "w.db") as conn:
        seed(conn, domain="x.com")
        conn.execute(
            "INSERT INTO url_checks (url_hash, checked_at, http_status, redirect_count,"
            " cross_domain_redirect, crawler_version) VALUES (?,?,?,?,?,?)",
            ("h-x.com", NOW, 200, 0, 0, "test"),
        )
        check_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        verdict = classify_rules.Verdict(UrlState.PARKED, ["parking:resting here"])
        classify_state.record(
            conn, check_id=check_id, url_hash="h-x.com", verdict=verdict, policy=policy
        )
        stored = conn.execute(
            "SELECT classifier_version FROM url_classifications WHERE check_id=?",
            (check_id,),
        ).fetchone()
        assert stored["classifier_version"] == policy.effective_classifier_version

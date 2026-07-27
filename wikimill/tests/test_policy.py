"""Policy configuration — the operator's tuning surface.

Two properties carry this module: a typo must be loud rather than silently
ignored, and a change that affects a verdict must change the recorded classifier
version automatically.
"""

from __future__ import annotations

import pytest

from wikimill.errors import ConfigError
from wikimill.policy import POLICY_FILENAME, Policy, load


def write(root, body: str):
    (root / POLICY_FILENAME).write_text(body, encoding="utf-8")
    return root


# -- defaults ---------------------------------------------------------------


def test_missing_file_uses_defaults(tmp_path):
    """A fresh checkout must work with no config at all."""
    pol = load(tmp_path)
    assert pol.is_default
    assert pol.source == "built-in defaults"


def test_defaults_match_the_shipped_constants(tmp_path):
    from wikimill import score
    from wikimill.classify import signals

    pol = load(tmp_path)
    assert pol.scoring.citation_points_cap == score.CITATION_POINTS_CAP
    assert pol.scoring.state_points["unregistered"] == score.STATE_POINTS["unregistered"]
    assert pol.markers.parking_providers == list(signals.PARKING_PROVIDERS)


# -- loading ----------------------------------------------------------------


def test_scalar_override(tmp_path):
    pol = load(write(tmp_path, "[export]\nmin_pages = 5\n"))
    assert pol.export.min_pages == 5
    assert pol.scoring.citation_points_cap == Policy().scoring.citation_points_cap


def test_list_override(tmp_path):
    pol = load(write(tmp_path, '[export]\ncandidate_states = ["unregistered"]\n'))
    assert pol.export.candidate_states == ["unregistered"]


def test_table_override(tmp_path):
    pol = load(write(tmp_path, "[scoring.state_points]\nunregistered = 99\n"))
    assert pol.scoring.state_points["unregistered"] == 99


def test_marker_list_is_editable(tmp_path):
    """Parking signatures drift as providers change templates — editing them
    must not require a code change."""
    pol = load(write(tmp_path, '[markers]\nparking_providers = ["newpark.example"]\n'))
    assert pol.markers.parking_providers == ["newpark.example"]


# -- a typo must be loud ----------------------------------------------------


def test_unknown_key_is_rejected(tmp_path):
    """Silently ignoring a typo is worse than failing: the operator believes
    they changed a threshold and the tool carries on with the old one."""
    with pytest.raises(ConfigError) as exc:
        load(write(tmp_path, "[export]\nmin_page = 5\n"))
    assert "min_page" in str(exc.value)
    assert exc.value.remediation and "min_pages" in exc.value.remediation


def test_unknown_section_is_rejected(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load(write(tmp_path, "[scorring]\nx = 1\n"))
    assert exc.value.remediation


def test_wrong_type_is_rejected(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load(write(tmp_path, '[export]\nmin_pages = "three"\n'))
    assert "number" in str(exc.value)


def test_list_where_scalar_expected_is_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load(write(tmp_path, "[export]\nmin_pages = [1, 2]\n"))


def test_malformed_toml_names_the_fix(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load(write(tmp_path, "[export\nmin_pages = 1\n"))
    assert "valid TOML" in str(exc.value)
    assert "config validate" in exc.value.remediation


# -- provenance: the fingerprint --------------------------------------------


def test_classifying_change_shifts_the_version(tmp_path):
    """Editing a weight must make verdicts distinguishable from ones judged
    under the old rules — without anyone remembering to bump a constant."""
    base = Policy()
    tuned = load(write(tmp_path, "[scoring]\ncitation_points_per_page = 9\n"))
    assert tuned.fingerprint() != base.fingerprint()
    assert tuned.effective_classifier_version != base.effective_classifier_version


def test_marker_change_shifts_the_version(tmp_path):
    base = Policy()
    tuned = load(write(tmp_path, '[markers]\nfor_sale_phrases = ["buy it now"]\n'))
    assert tuned.fingerprint() != base.fingerprint()


def test_non_classifying_change_does_not_shift_the_version(tmp_path):
    """Crawl pacing changes throughput, not verdicts. Bumping the classifier
    version for it would make unrelated runs look incomparable."""
    base = Policy()
    tuned = load(write(tmp_path, "[crawl]\nconcurrency = 32\n"))
    assert tuned.crawl.concurrency == 32
    assert tuned.fingerprint() == base.fingerprint()


def test_fingerprint_is_stable_across_loads(tmp_path):
    body = "[scoring]\ncitation_points_cap = 40\n"
    assert load(write(tmp_path, body)).fingerprint() == load(tmp_path).fingerprint()


def test_fingerprint_ignores_key_order(tmp_path):
    a = load(write(tmp_path, "[export]\nmin_pages = 2\n\n[scoring]\ncitation_points_cap = 40\n"))
    b = load(write(tmp_path, "[scoring]\ncitation_points_cap = 40\n\n[export]\nmin_pages = 2\n"))
    assert a.fingerprint() == b.fingerprint()


# -- what must NOT be tunable -----------------------------------------------


@pytest.mark.parametrize(
    "forbidden",
    ["per_domain_concurrency", "max_redirects", "max_body_bytes",
     "evidence_blob_bytes", "robots", "resolvers_required"],
)
def test_safety_invariants_are_not_configurable(forbidden):
    """Politeness and safety guarantees are not preferences. Per-domain
    concurrency of 1, redirect and body caps, robots obedience and the
    two-resolver rule stay in code, where config cannot weaken them."""
    keys = {k for _s, k, _v in Policy().describe()}
    assert forbidden not in keys


def test_describe_covers_every_section():
    sections = {s for s, _k, _v in Policy().describe()}
    assert sections == {"scoring", "export", "enrich", "check", "classify",
                        "markers", "crawl"}

"""Config loading, precedence, and secret redaction."""

from __future__ import annotations

import pytest

from wikimill.config import Config, is_secret, load, redact
from wikimill.constants import REDACTED
from wikimill.errors import ConfigError


def write_env(root, body: str) -> None:
    (root / "wikimill.env").write_text(body, encoding="utf-8")


def test_env_file_is_loaded(tmp_path, monkeypatch):
    monkeypatch.delenv("WIKIMILL_CONTACT", raising=False)
    write_env(tmp_path, "WIKIMILL_CONTACT=ops@example.org\n")
    cfg = load(tmp_path)
    assert cfg.env_file_found
    assert cfg.contact == "ops@example.org"


def test_process_env_beats_file(tmp_path, monkeypatch):
    write_env(tmp_path, "WIKIMILL_CONTACT=file@example.org\n")
    monkeypatch.setenv("WIKIMILL_CONTACT", "env@example.org")
    cfg = load(tmp_path)
    assert cfg.contact == "env@example.org"


def test_missing_env_file_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("WIKIMILL_CONTACT", raising=False)
    cfg = load(tmp_path)
    assert not cfg.env_file_found
    assert cfg.contact is None


def test_comments_and_blank_lines_ignored(tmp_path, monkeypatch):
    monkeypatch.delenv("WIKIMILL_CONTACT", raising=False)
    write_env(tmp_path, "# a comment\n\nWIKIMILL_CONTACT=x@example.org\n")
    assert load(tmp_path).contact == "x@example.org"


def test_quotes_are_stripped(tmp_path, monkeypatch):
    monkeypatch.delenv("WIKIMILL_CONTACT", raising=False)
    write_env(tmp_path, 'WIKIMILL_CONTACT="q@example.org"\n')
    assert load(tmp_path).contact == "q@example.org"


# -- crawler identity ------------------------------------------------------


def test_user_agent_built_from_contact(tmp_path, monkeypatch):
    monkeypatch.delenv("WIKIMILL_USER_AGENT", raising=False)
    monkeypatch.setenv("WIKIMILL_CONTACT", "ops@example.org")
    ua = load(tmp_path).user_agent
    assert "wikimill/" in ua
    assert "ops@example.org" in ua


def test_user_agent_placeholder_is_substituted(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKIMILL_CONTACT", "ops@example.org")
    monkeypatch.setenv("WIKIMILL_USER_AGENT", "wikimill/0.1.0 (+CONTACT)")
    assert load(tmp_path).user_agent == "wikimill/0.1.0 (+ops@example.org)"


def test_user_agent_requires_contact(tmp_path, monkeypatch):
    """An unsubstituted placeholder must fail here rather than reach a server."""
    monkeypatch.delenv("WIKIMILL_CONTACT", raising=False)
    monkeypatch.setenv("WIKIMILL_USER_AGENT", "wikimill/0.1.0 (+CONTACT)")
    with pytest.raises(ConfigError) as exc:
        _ = load(tmp_path).user_agent
    assert exc.value.remediation  # every error names its fix


def test_explicit_user_agent_without_placeholder_is_used_as_is(tmp_path, monkeypatch):
    monkeypatch.delenv("WIKIMILL_CONTACT", raising=False)
    monkeypatch.setenv("WIKIMILL_USER_AGENT", "custom-agent/2.0 (+https://x.example)")
    assert load(tmp_path).user_agent == "custom-agent/2.0 (+https://x.example)"


# -- secrets ---------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["WIKIMILL_ENTERPRISE_TOKEN", "SOME_API_KEY", "X_SECRET", "DB_PASSWORD"],
)
def test_secret_names_detected(name):
    assert is_secret(name)


@pytest.mark.parametrize("name", ["WIKIMILL_CONTACT", "WIKIMILL_DUMPS_DIR"])
def test_non_secret_names_not_detected(name):
    assert not is_secret(name)


def test_redaction_hides_value():
    assert redact("WIKIMILL_ENTERPRISE_TOKEN", "hunter2") == REDACTED
    assert redact("WIKIMILL_CONTACT", "ops@example.org") == "ops@example.org"


def test_unset_secret_is_not_shown_as_redacted():
    """`<redacted>` for an unset variable would hide a real misconfiguration."""
    assert redact("SOME_API_KEY", "") == ""
    assert redact("SOME_API_KEY", None) is None


def test_describe_redacts_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKIMILL_CONTACT", "ops@example.org")
    monkeypatch.setenv("WIKIMILL_ENTERPRISE_TOKEN", "hunter2")
    rows = load(tmp_path).describe()
    values = {name: value for name, value, _ in rows}
    assert "hunter2" not in " ".join(values.values())
    assert values["WIKIMILL_ENTERPRISE_TOKEN"] == REDACTED


def test_describe_reports_source(tmp_path, monkeypatch):
    monkeypatch.delenv("WIKIMILL_CONCURRENCY", raising=False)
    write_env(tmp_path, "WIKIMILL_CONTACT=file@example.org\n")
    monkeypatch.delenv("WIKIMILL_CONTACT", raising=False)
    sources = {n: s for n, _, s in load(tmp_path).describe()}
    assert sources["WIKIMILL_CONTACT"] == "wikimill.env"
    assert sources["WIKIMILL_CONCURRENCY"] == "default"


# -- typed accessors -------------------------------------------------------


def test_get_int_rejects_garbage(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKIMILL_CONCURRENCY", "lots")
    with pytest.raises(ConfigError):
        load(tmp_path).concurrency


def test_dns_resolvers_default_has_two(tmp_path, monkeypatch):
    """An `unregistered` verdict needs two independent resolvers to agree."""
    monkeypatch.delenv("WIKIMILL_DNS_RESOLVERS", raising=False)
    assert len(load(tmp_path).dns_resolvers) >= 2


def test_dumps_dir_override(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKIMILL_DUMPS_DIR", "/mnt/external/dumps")
    cfg: Config = load(tmp_path)
    assert cfg.dumps_dir.as_posix() == "/mnt/external/dumps"
    # The database never follows the dumps onto external media.
    assert cfg.db_path.parent == cfg.state_dir


def test_dumps_dir_defaults_under_state(tmp_path, monkeypatch):
    monkeypatch.delenv("WIKIMILL_DUMPS_DIR", raising=False)
    cfg = load(tmp_path)
    assert cfg.dumps_dir == cfg.state_dir / "dumps"

"""CLI surface: the eight commands, exit codes, and honest stubs."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from wikimill import __version__
from wikimill.cli import app
from wikimill.errors import NotImplementedYetError

runner = CliRunner()

EXPECTED_COMMANDS = {
    "preflight",
    "stats",
    "ingest",
    "namespaces",
    "crawl",
    "check",
    "enrich",
    "inspect",
    "export",
}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Run every CLI test against a throwaway root, never the real state/."""
    monkeypatch.setenv("WIKIMILL_CONTACT", "ops@example.org")
    monkeypatch.setattr("wikimill.config.repo_root", lambda: tmp_path)


def test_help_lists_exactly_the_agreed_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in EXPECTED_COMMANDS:
        assert name in result.stdout


def test_no_command_shows_help():
    assert runner.invoke(app, []).exit_code == 0


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_preflight_runs():
    result = runner.invoke(app, ["preflight"])
    assert result.exit_code == 0


def test_preflight_json_is_valid_json():
    result = runner.invoke(app, ["preflight", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["version"] == __version__
    assert {c["step"] for c in payload["checks"]}


def test_preflight_json_redacts_secrets(monkeypatch):
    monkeypatch.setenv("WIKIMILL_ENTERPRISE_TOKEN", "hunter2")
    result = runner.invoke(app, ["preflight", "--json"])
    assert "hunter2" not in result.stdout


def test_preflight_exit_2_without_identity(monkeypatch):
    monkeypatch.delenv("WIKIMILL_CONTACT", raising=False)
    monkeypatch.delenv("WIKIMILL_USER_AGENT", raising=False)
    assert runner.invoke(app, ["preflight"]).exit_code == 2


def test_stats_on_empty_db():
    result = runner.invoke(app, ["stats"])
    assert result.exit_code == 0
    assert "schema v" in result.stdout


def test_stats_json():
    result = runner.invoke(app, ["stats", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["counts"]["urls"] == 0


@pytest.mark.parametrize(
    ("command", "phase"),
    [
        (["inspect", "example.com"], "v1.I"),
        (["export"], "v1.I"),
    ],
)
def test_unshipped_commands_name_their_phase(command, phase):
    """A stub that names its phase beats a missing command or a stack trace."""
    result = runner.invoke(app, command)
    assert isinstance(result.exception, NotImplementedYetError)
    assert result.exception.phase == phase

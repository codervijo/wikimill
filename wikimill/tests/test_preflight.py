"""Preflight checks, markers, and the hard gate."""

from __future__ import annotations

import pytest

from wikimill.config import load
from wikimill.constants import Marker
from wikimill.errors import PreflightError
from wikimill.logging import RunLog
from wikimill.preflight import (
    check_database,
    check_dumps,
    check_env_file,
    check_identity,
    check_state_dir,
    gate,
    preflight,
    run_checks,
)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKIMILL_CONTACT", "ops@example.org")
    monkeypatch.delenv("WIKIMILL_DUMPS_DIR", raising=False)
    return load(tmp_path)


@pytest.fixture
def log(tmp_path):
    return RunLog("test", tmp_path / "logs", quiet=True)


def test_identity_blocks_when_contact_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("WIKIMILL_CONTACT", raising=False)
    monkeypatch.delenv("WIKIMILL_USER_AGENT", raising=False)
    result = check_identity(load(tmp_path))
    assert result.marker is Marker.FAIL
    assert result.remediation


def test_identity_passes_with_contact(cfg):
    assert check_identity(cfg).marker is Marker.OK


def test_env_file_absent_warns_not_fails(cfg):
    """A missing env file is a ↷, never a ✗ — defaults are legitimate."""
    assert check_env_file(cfg).marker is Marker.WARN


def test_state_dir_created(cfg):
    assert check_state_dir(cfg).marker is Marker.OK
    assert cfg.state_dir.is_dir()
    assert cfg.logs_dir.is_dir()
    assert cfg.outputs_dir.is_dir()


def test_database_check_migrates(cfg):
    check_state_dir(cfg)
    result = check_database(cfg)
    assert result.marker is Marker.OK
    assert cfg.db_path.exists()


def test_missing_dumps_warns_not_fails(cfg):
    """v1.B must be runnable with no 32 GB of dumps on disk."""
    result = check_dumps(cfg)
    assert result.marker is Marker.WARN
    assert not result.blocking


def test_dumps_present_detected(cfg):
    cfg.dumps_dir.mkdir(parents=True, exist_ok=True)
    (cfg.dumps_dir / "enwiki-20260701-externallinks.sql.gz").write_bytes(b"x")
    result = check_dumps(cfg)
    assert result.marker is Marker.WARN  # index + article dump still missing
    assert "1/3" in result.detail


def test_all_dumps_present_is_ok(cfg):
    cfg.dumps_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "enwiki-20260701-externallinks.sql.gz",
        "enwiki-20260701-pages-articles-multistream-index.txt.bz2",
        "enwiki-20260701-pages-articles-multistream.xml.bz2",
    ):
        (cfg.dumps_dir / name).write_bytes(b"x")
    assert check_dumps(cfg).marker is Marker.OK


def test_preflight_passes_with_contact_set(cfg, log):
    assert preflight(cfg, log, show_config=False) is True


def test_preflight_fails_without_contact(tmp_path, monkeypatch, log):
    monkeypatch.delenv("WIKIMILL_CONTACT", raising=False)
    monkeypatch.delenv("WIKIMILL_USER_AGENT", raising=False)
    assert preflight(load(tmp_path), log, show_config=False) is False


def test_gate_raises_on_blocking_failure(tmp_path, monkeypatch, log):
    monkeypatch.delenv("WIKIMILL_CONTACT", raising=False)
    monkeypatch.delenv("WIKIMILL_USER_AGENT", raising=False)
    with pytest.raises(PreflightError) as exc:
        gate(load(tmp_path), log)
    assert exc.value.exit_code == 2


def test_gate_silent_on_success(cfg, log):
    gate(cfg, log)  # must not raise


def test_every_check_names_a_fix_when_it_fails(tmp_path, monkeypatch):
    """A ✗ the operator cannot act on is a bug."""
    monkeypatch.delenv("WIKIMILL_CONTACT", raising=False)
    monkeypatch.delenv("WIKIMILL_USER_AGENT", raising=False)
    for result in run_checks(load(tmp_path)):
        if result.marker is Marker.FAIL:
            assert result.remediation, f"{result.step} failed without a remediation"

"""The host launcher, tested hermetically — no Docker required.

This is what WIKIMILL_DRY_RUN exists for: the launcher decides mounts and
environment before any container exists, so it needs to be verifiable without
one. These tests never invoke Docker.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "bin" / "wikimill"
INSTALLER = REPO / "bin" / "install"


def run(script: Path, *args: str, env: dict[str, str] | None = None):
    base = {**os.environ, "WIKIMILL_DRY_RUN": "1", "DOCKER_CMD": "true"}
    base.update(env or {})
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        env=base,
        timeout=30,
    )


def test_launcher_exists_and_is_executable():
    assert LAUNCHER.is_file()
    assert os.access(LAUNCHER, os.X_OK)


def test_dry_run_starts_no_container():
    result = run(LAUNCHER, "preflight")
    assert result.returncode == 0
    assert "dry run" in result.stderr
    assert "nothing was executed" in result.stderr


def test_dry_run_shows_both_mounts():
    result = run(LAUNCHER, "preflight")
    assert "/usr/src/app" in result.stderr
    assert "/usr/src/app/state/dumps" in result.stderr


def test_dry_run_reports_custom_dumps_dir(tmp_path):
    external = tmp_path / "external-drive"
    external.mkdir()
    result = run(LAUNCHER, "preflight", env={"WIKIMILL_DUMPS_DIR": str(external)})
    assert result.returncode == 0
    assert str(external) in result.stderr


def test_missing_dumps_dir_fails_cleanly(tmp_path):
    """An unplugged external drive must be a clean ✗ before anything starts."""
    missing = tmp_path / "unplugged"
    result = run(LAUNCHER, "preflight", env={"WIKIMILL_DUMPS_DIR": str(missing)})
    assert result.returncode != 0
    assert "does not exist" in result.stderr
    assert "mounted" in result.stderr  # names the likely cause and the fix


def test_app_env_vars_are_forwarded_into_the_container():
    """Precedence is `process environment > wikimill.env > default`, so an inline
    override must actually cross into the container — not stop at the host."""
    result = run(LAUNCHER, "crawl", env={"WIKIMILL_CONCURRENCY": "3"})
    assert "WIKIMILL_CONCURRENCY=3" in result.stderr


def test_host_dumps_dir_is_not_leaked_into_the_container(tmp_path):
    """The host path is meaningless inside; the mount is always at the default
    location, so forwarding it would misdirect every resolved path."""
    external = tmp_path / "drive"
    external.mkdir()
    result = run(LAUNCHER, "preflight", env={"WIKIMILL_DUMPS_DIR": str(external)})
    assert f"-e WIKIMILL_DUMPS_DIR={external}" not in result.stderr
    assert str(external) in result.stderr  # still visible as the mount source


def test_shell_subcommand_runs_bash():
    result = run(LAUNCHER, "shell")
    assert "bash" in result.stderr


def test_command_args_are_forwarded():
    result = run(LAUNCHER, "crawl", "--limit", "5")
    assert "python -m wikimill" in result.stderr
    assert "crawl" in result.stderr
    assert "--limit" in result.stderr


def test_launcher_works_through_a_symlink(tmp_path):
    """bin/install links ~/.local/bin/wikimill here; the repo root must still resolve."""
    link = tmp_path / "wikimill-link"
    link.symlink_to(LAUNCHER)
    result = run(link, "preflight")
    assert result.returncode == 0
    assert str(REPO) in result.stderr


# -- installer -------------------------------------------------------------


def test_installer_dry_run(tmp_path):
    result = subprocess.run(
        ["bash", str(INSTALLER)],
        capture_output=True,
        text=True,
        env={**os.environ, "DRY_RUN": "1", "BIN_DIR": str(tmp_path / "bin")},
        timeout=30,
    )
    assert result.returncode == 0
    assert "would link" in result.stdout


def test_installer_creates_symlink(tmp_path):
    bindir = tmp_path / "bin"
    result = subprocess.run(
        ["bash", str(INSTALLER)],
        capture_output=True,
        text=True,
        env={**os.environ, "BIN_DIR": str(bindir)},
        timeout=30,
    )
    assert result.returncode == 0
    link = bindir / "wikimill"
    assert link.is_symlink()
    assert Path(os.readlink(link)) == LAUNCHER


def test_installer_is_idempotent(tmp_path):
    bindir = tmp_path / "bin"
    env = {**os.environ, "BIN_DIR": str(bindir)}
    subprocess.run(["bash", str(INSTALLER)], capture_output=True, env=env, timeout=30)
    second = subprocess.run(
        ["bash", str(INSTALLER)], capture_output=True, text=True, env=env, timeout=30
    )
    assert second.returncode == 0
    assert "already linked" in second.stdout


def test_installer_refuses_to_clobber_a_real_file(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "wikimill").write_text("precious", encoding="utf-8")
    result = subprocess.run(
        ["bash", str(INSTALLER)],
        capture_output=True,
        text=True,
        env={**os.environ, "BIN_DIR": str(bindir)},
        timeout=30,
    )
    assert result.returncode != 0
    assert (bindir / "wikimill").read_text(encoding="utf-8") == "precious"

"""The preflight gate.

Runs before every state-touching command and aborts on ✗ before any work, any
network request, and any dump read. The point is to fail fast and cheap with an
exact remediation, rather than half-way through a two-hour crawl.

Checks are a registry of small functions, each returning a CheckResult. Later
phases append their own (robots reachability at v1.E, RDAP at v1.G) without
touching the runner.

Marker semantics here match the rest of the tool:
  ✓ satisfied
  ↷ not satisfied but not blocking *yet* — typically a thing a later phase needs
  ✗ blocking; the command must not proceed
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .constants import Marker
from .errors import ConfigError
from .logging import RunLog
from .storage import LATEST_VERSION, open_db, user_version

# Files ingest needs, and the phase that first requires each. Absent files are
# ↷ (not ✗) until their phase ships — v1.B must be runnable with no 32 GB of
# dumps on disk.
DUMP_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("*-externallinks.sql.gz", "externallinks SQL dump", "v1.C"),
    ("*-pages-articles-multistream-index*.txt.bz2", "multistream index", "v1.C"),
    ("*-pages-articles-multistream*.xml.bz2", "multistream article dump", "v1.H"),
)


@dataclass(frozen=True)
class CheckResult:
    marker: Marker
    step: str
    detail: str = ""
    remediation: str | None = None

    @property
    def blocking(self) -> bool:
        return self.marker is Marker.FAIL


Check = Callable[[Config], CheckResult]


# --------------------------------------------------------------------------
# Individual checks
# --------------------------------------------------------------------------


def check_docker(cfg: Config) -> CheckResult:
    """Crawlers run inside Docker, never on the host (operator hard rule).

    Not blocking, because `make test` and unit runs are legitimately outside a
    container — but a real crawl on the host is a conformance violation, so it
    must at least be visible.
    """
    if cfg.in_docker:
        return CheckResult(Marker.OK, "docker", "running inside a container")
    return CheckResult(
        Marker.WARN,
        "docker",
        "running on the host",
        "Use ./bin/wikimill <cmd> or `make buildsh`. Crawlers must run in Docker.",
    )


def check_env_file(cfg: Config) -> CheckResult:
    if cfg.env_file_found:
        return CheckResult(Marker.OK, "config", f"loaded {cfg.env_file.name}")
    return CheckResult(
        Marker.WARN,
        "config",
        f"no {cfg.env_file.name} (using environment + defaults)",
        f"cp {cfg.env_file.name}.example {cfg.env_file.name} and fill it in.",
    )


def check_identity(cfg: Config) -> CheckResult:
    """The crawler's public identity. Blocking: we do not touch anyone's server
    anonymously, and Wikimedia's User-Agent policy requires real contact info."""
    try:
        ua = cfg.user_agent
    except ConfigError as exc:
        return CheckResult(Marker.FAIL, "identity", exc.message, exc.remediation)
    return CheckResult(Marker.OK, "identity", ua)


def check_state_dir(cfg: Config) -> CheckResult:
    try:
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        cfg.outputs_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.state_dir / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return CheckResult(
            Marker.FAIL,
            "state dir",
            f"{cfg.state_dir} is not writable: {exc}",
            "Check the mount and its permissions.",
        )
    return CheckResult(Marker.OK, "state dir", str(cfg.state_dir))


def check_database(cfg: Config) -> CheckResult:
    """Open and migrate. Idempotent — a migrated database is a no-op."""
    try:
        with open_db(cfg.db_path) as conn:
            version = user_version(conn)
    except Exception as exc:  # noqa: BLE001 - surfaced as a preflight failure
        remediation = getattr(exc, "remediation", None)
        return CheckResult(Marker.FAIL, "database", str(exc), remediation)
    return CheckResult(
        Marker.OK, "database", f"{cfg.db_path.name} at schema v{version}"
    )


def check_db_not_on_dumps_mount(cfg: Config) -> CheckResult:
    """The database must not share a mount with the dumps.

    Dumps commonly live on an external SSD/HDD; SQLite WAL there risks
    corruption (POSIX locking + durable fsync), and a mid-run unplug takes the
    database with it. Warn rather than block: we can only compare device ids,
    which is a proxy, and a false ✗ would be worse than a visible ↷.
    """
    dumps = cfg.dumps_dir
    if not dumps.exists():
        return CheckResult(Marker.OK, "db location", "dumps dir not present yet")
    try:
        db_dev = os.stat(cfg.db_path.parent).st_dev
        dumps_dev = os.stat(dumps).st_dev
    except OSError:
        return CheckResult(Marker.OK, "db location", "not determinable")
    if db_dev == dumps_dev and str(dumps).startswith(str(cfg.state_dir)):
        # The default layout: dumps inside state/, same local disk. Fine.
        return CheckResult(Marker.OK, "db location", "local disk")
    if db_dev == dumps_dev:
        return CheckResult(
            Marker.WARN,
            "db location",
            f"database shares a mount with {dumps}",
            "Keep state/wikimill.db on local disk; only state/dumps/ may be "
            "external. SQLite WAL on removable or non-POSIX media risks "
            "corruption, not just errors.",
        )
    return CheckResult(Marker.OK, "db location", "separate from dumps mount")


def check_dumps(cfg: Config) -> CheckResult:
    """Presence of the dumps. ↷ until v1.C — v1.B must run without 32 GB on disk."""
    dumps = cfg.dumps_dir
    if not dumps.exists():
        return CheckResult(
            Marker.WARN,
            "dumps",
            f"{dumps} does not exist (needed from v1.C)",
            f"Download the dumps there, or set WIKIMILL_DUMPS_DIR to where they "
            f"already live (an external SSD/HDD is fine — ~32 GB total).",
        )
    if not os.access(dumps, os.R_OK):
        return CheckResult(
            Marker.FAIL,
            "dumps",
            f"{dumps} is not readable",
            "Check permissions, or that the external drive is still mounted.",
        )
    found: list[str] = []
    missing: list[str] = []
    for pattern, label, phase in DUMP_PATTERNS:
        if next(dumps.glob(pattern), None) is not None:
            found.append(label)
        else:
            missing.append(f"{label} ({phase})")
    if missing and not found:
        return CheckResult(
            Marker.WARN, "dumps", f"{dumps} present but empty — missing: "
            + ", ".join(missing)
        )
    detail = f"{len(found)}/{len(DUMP_PATTERNS)} present in {dumps}"
    if missing:
        return CheckResult(Marker.WARN, "dumps", f"{detail}; missing: " + ", ".join(missing))
    return CheckResult(Marker.OK, "dumps", detail)


def check_dump_checksums(cfg: Config) -> CheckResult:
    """Verify dumps against a cached checksum.

    Re-hashing ~32 GB over USB on every command would dominate runtime, so the
    cache is keyed on (size, mtime) and only a changed file is re-hashed. A
    full re-hash is available via `preflight --verify-dumps`.
    """
    dumps = cfg.dumps_dir
    if not dumps.exists():
        return CheckResult(Marker.WARN, "dump integrity", "no dumps to verify")
    cache = _load_checksum_cache(cfg)
    changed: list[str] = []
    seen = 0
    for pattern, _label, _phase in DUMP_PATTERNS:
        for path in dumps.glob(pattern):
            seen += 1
            stat = path.stat()
            entry = cache.get(str(path))
            if entry and entry.get("size") == stat.st_size and entry.get(
                "mtime"
            ) == int(stat.st_mtime):
                continue
            changed.append(path.name)
    if seen == 0:
        return CheckResult(Marker.WARN, "dump integrity", "no dumps to verify")
    if changed:
        return CheckResult(
            Marker.WARN,
            "dump integrity",
            f"{len(changed)} file(s) unverified since last change",
            "Run `wikimill preflight --verify-dumps` to hash them (slow on USB).",
        )
    return CheckResult(Marker.OK, "dump integrity", f"{seen} file(s) match cache")


CHECKS: tuple[Check, ...] = (
    check_docker,
    check_env_file,
    check_identity,
    check_state_dir,
    check_database,
    check_db_not_on_dumps_mount,
    check_dumps,
    check_dump_checksums,
)


# --------------------------------------------------------------------------
# Checksum cache
# --------------------------------------------------------------------------


def _load_checksum_cache(cfg: Config) -> dict[str, dict[str, object]]:
    path = cfg.checksum_cache_path
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_checksum_cache(cfg: Config, cache: dict[str, dict[str, object]]) -> None:
    try:
        cfg.checksum_cache_path.write_text(
            json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8"
        )
    except OSError:
        pass  # A cache we cannot persist costs speed, never correctness.


def verify_dumps(cfg: Config, log: RunLog) -> None:
    """Hash every dump and refresh the cache. Slow by nature — 32 GB of I/O."""
    dumps = cfg.dumps_dir
    if not dumps.exists():
        log.warn("verify dumps", f"{dumps} does not exist")
        return
    cache = _load_checksum_cache(cfg)
    for pattern, _label, _phase in DUMP_PATTERNS:
        for path in sorted(dumps.glob(pattern)):
            stat = path.stat()
            log.progress(f"hashing {path.name} ({stat.st_size / 1e9:.1f} GB)…")
            digest = _sha256(path)
            previous = cache.get(str(path), {}).get("sha256")
            cache[str(path)] = {
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
                "sha256": digest,
            }
            if previous and previous != digest:
                log.fail(
                    f"verify {path.name}",
                    "checksum CHANGED since last run — the file was modified or is corrupt",
                )
            else:
                log.ok(f"verify {path.name}", digest[:16] + "…")
    _save_checksum_cache(cfg, cache)


def _sha256(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


def run_checks(cfg: Config) -> Iterator[CheckResult]:
    for check in CHECKS:
        yield check(cfg)


def preflight(cfg: Config, log: RunLog, *, show_config: bool = True) -> bool:
    """Run every check, emitting a marker per step. Returns False if blocked."""
    if show_config:
        log.note("configuration (secrets redacted):")
        for name, value, source in cfg.describe():
            log.progress(f"{name:<26} {value:<40} [{source}]")
        log.note("")

    blocked = False
    for result in run_checks(cfg):
        detail = result.detail
        if result.marker is Marker.OK:
            log.ok(result.step, detail)
        elif result.marker is Marker.WARN:
            log.warn(result.step, detail)
            if result.remediation:
                log.progress(f"→ {result.remediation}")
        else:
            log.fail(result.step, detail)
            if result.remediation:
                log.progress(f"→ {result.remediation}")
            blocked = True
    return not blocked


def gate(cfg: Config, log: RunLog) -> None:
    """Preflight as a hard gate for real commands. Raises on ✗.

    Quiet on success: a command that works should not make the operator read a
    preflight report first. Failures are loud and name the fix.
    """
    from .errors import PreflightError

    failures = [r for r in run_checks(cfg) if r.blocking]
    if failures:
        for result in failures:
            log.fail(result.step, result.detail)
            if result.remediation:
                log.progress(f"→ {result.remediation}")
        raise PreflightError(
            f"Preflight failed ({len(failures)} blocking). No work was done.",
            remediation="Run `wikimill preflight` for the full report.",
        )

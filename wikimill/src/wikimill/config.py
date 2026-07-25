"""Configuration: environment variables sourced from a mounted `wikimill.env`.

Precedence is  process environment  >  wikimill.env  >  built-in default.
That ordering is deliberate: a one-off override must work without editing the
file, e.g. `WIKIMILL_DUMPS_DIR=/mnt/ssd2 ./bin/wikimill enrich`.

Two layers of variables exist (see constants.LAUNCHER_ENV_VARS / APP_ENV_VARS).
This module reads the *application* layer, inside the container. The launcher
layer is read by bin/wikimill on the host, because those variables decide how
the container is built and what is mounted into it — they cannot be read from
inside the container they configure. `WIKIMILL_DUMPS_DIR` is read here too, but
only to *report* the resolved path; the actual mount already happened.

No credentials are needed at v1, but the loading and redaction path is built
now anyway. Retrofitting secret handling around a key that has already been
committed once is far more expensive than having it ready before it is needed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .constants import (
    APP_ENV_VARS,
    CHECKSUM_CACHE_FILENAME,
    DB_FILENAME,
    DEFAULT_CONCURRENCY,
    DEFAULT_CRAWL_DELAY_SECS,
    DUMPS_DIRNAME,
    ENV_FILE_NAME,
    LAUNCHER_ENV_VARS,
    LOGS_DIRNAME,
    OUTPUTS_DIRNAME,
    REDACTED,
    SECRET_NAME_PATTERNS,
    STATE_DIRNAME,
)
from .errors import ConfigError


def is_secret(name: str) -> bool:
    """True when a variable name looks like it holds a credential.

    Matching on the *name* rather than the value is what makes redaction
    reliable for variables that do not exist yet.
    """
    upper = name.upper()
    return any(pat in upper for pat in SECRET_NAME_PATTERNS)


def redact(name: str, value: Any) -> Any:
    """Redact a value if its name marks it as a secret. Empty stays empty —
    showing `<redacted>` for an unset variable would hide a real misconfig."""
    if value in (None, ""):
        return value
    return REDACTED if is_secret(name) else value


def repo_root() -> Path:
    """The project root — three parents up from this file (src/wikimill/config.py)."""
    return Path(__file__).resolve().parents[2]


def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a dotenv file. Falls back to a small parser if python-dotenv is absent."""
    if not path.is_file():
        return {}
    try:
        from dotenv import dotenv_values
    except ImportError:  # pragma: no cover - dotenv is a declared dependency
        values: dict[str, str] = {}
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip("'\"")
        return values
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


@dataclass(frozen=True)
class Config:
    """Resolved configuration for one invocation."""

    root: Path
    env_file: Path
    env_file_found: bool
    values: dict[str, str] = field(repr=False, default_factory=dict)

    # -- lookup ------------------------------------------------------------

    def get(self, name: str, default: str | None = None) -> str | None:
        val = self.values.get(name)
        return val if val not in (None, "") else default

    def require(self, name: str, *, why: str) -> str:
        val = self.get(name)
        if not val:
            raise ConfigError(
                f"{name} is not set — {why}.",
                remediation=(
                    f"Set {name} in {self.env_file.name} "
                    f"(copy {ENV_FILE_NAME}.example if you have not yet), "
                    f"or pass it inline: {name}=... ./bin/wikimill <cmd>"
                ),
            )
        return val

    def get_int(self, name: str, default: int) -> int:
        raw = self.get(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(
                f"{name}={raw!r} is not an integer.",
                remediation=f"Set {name} to a whole number in {self.env_file.name}.",
            ) from exc

    def get_float(self, name: str, default: float) -> float:
        raw = self.get(name)
        if raw is None:
            return default
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(
                f"{name}={raw!r} is not a number.",
                remediation=f"Set {name} to a number in {self.env_file.name}.",
            ) from exc

    # -- derived paths -----------------------------------------------------

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIRNAME

    @property
    def db_path(self) -> Path:
        """Always local, never on the dumps volume — see architecture.md."""
        return self.state_dir / DB_FILENAME

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / LOGS_DIRNAME

    @property
    def outputs_dir(self) -> Path:
        return self.root / OUTPUTS_DIRNAME

    @property
    def dumps_dir(self) -> Path:
        """Where the dumps live. `WIKIMILL_DUMPS_DIR` relocates them (commonly to
        an external SSD/HDD — ~32 GB total); inside the container the launcher has
        already bind-mounted that host path over `state/dumps`."""
        override = self.get("WIKIMILL_DUMPS_DIR")
        return Path(override) if override else self.state_dir / DUMPS_DIRNAME

    @property
    def checksum_cache_path(self) -> Path:
        """Cached dump checksums. Re-hashing ~32 GB over USB on every command
        would dominate runtime, so verification is keyed on (size, mtime)."""
        return self.state_dir / CHECKSUM_CACHE_FILENAME

    # -- crawler identity + politeness -------------------------------------

    @property
    def contact(self) -> str | None:
        return self.get("WIKIMILL_CONTACT")

    @property
    def user_agent(self) -> str:
        """The public identity presented to every server we touch.

        The Wikimedia User-Agent policy requires real contact information, and
        a `(+CONTACT)` placeholder left unsubstituted is treated as unset —
        shipping that string to a real server would be worse than failing here.
        """
        contact = self.contact
        ua = self.get("WIKIMILL_USER_AGENT")
        if ua and "CONTACT" not in ua:
            return ua
        if not contact:
            raise ConfigError(
                "WIKIMILL_CONTACT is not set — the crawler has no public identity.",
                remediation=(
                    "Set WIKIMILL_CONTACT to a URL or email you monitor in "
                    f"{self.env_file.name}. Wikimedia's User-Agent policy requires "
                    "real contact info, and every site we crawl deserves it."
                ),
            )
        from . import __version__

        if ua:
            return ua.replace("CONTACT", contact)
        return f"wikimill/{__version__} (+{contact})"

    @property
    def dns_resolvers(self) -> list[str]:
        """At least two — an `unregistered` verdict needs two independent
        resolvers to agree before it is believed (prd.md §13)."""
        raw = self.get("WIKIMILL_DNS_RESOLVERS", "1.1.1.1,8.8.8.8") or ""
        return [r.strip() for r in raw.split(",") if r.strip()]

    @property
    def concurrency(self) -> int:
        return self.get_int("WIKIMILL_CONCURRENCY", DEFAULT_CONCURRENCY)

    @property
    def crawl_delay(self) -> float:
        return self.get_float("WIKIMILL_CRAWL_DELAY", DEFAULT_CRAWL_DELAY_SECS)

    @property
    def in_docker(self) -> bool:
        return bool(self.get("WIKIMILL_IN_DOCKER")) or Path("/.dockerenv").exists()

    # -- reporting ---------------------------------------------------------

    def describe(self) -> list[tuple[str, str, str]]:
        """Every known variable as (name, displayed_value, source), secrets
        redacted. This is what `preflight` prints — the operator should never
        have to guess which value actually took effect."""
        rows: list[tuple[str, str, str]] = []
        file_values = _load_env_file(self.env_file)
        for name in (*LAUNCHER_ENV_VARS, *APP_ENV_VARS):
            if name in os.environ and os.environ[name] != "":
                source = "environment"
            elif name in file_values and file_values[name] != "":
                source = self.env_file.name
            else:
                source = "default"
            value = self.get(name)
            shown = str(redact(name, value)) if value else "(unset)"
            rows.append((name, shown, source))
        # Surface any *other* WIKIMILL_* variable that is set, so a typo like
        # WIKIMILL_DUMP_DIR is visible rather than silently doing nothing.
        known = {*LAUNCHER_ENV_VARS, *APP_ENV_VARS}
        for name in sorted(set(self.values) - known):
            if name.startswith("WIKIMILL_"):
                rows.append((name, str(redact(name, self.values[name])), "extra"))
        return rows


def load(root: Path | None = None) -> Config:
    """Build the Config for this invocation.

    Reads `wikimill.env` if present, then lets the real process environment win.
    A missing env file is not an error — every value has a default or is
    reported as unset by preflight.
    """
    base = (root or repo_root()).resolve()
    env_file = base / ENV_FILE_NAME
    values = _load_env_file(env_file)
    for name, val in os.environ.items():
        if val != "":
            values[name] = val
    return Config(
        root=base,
        env_file=env_file,
        env_file_found=env_file.is_file(),
        values=values,
    )

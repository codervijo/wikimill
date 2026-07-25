"""Typed error hierarchy.

Every error carries an operator-facing remediation and an exit code. A raw
traceback reaching the operator is a bug: they cannot act on it. The rule is
that any error we can anticipate names the fix in the message.

Errors also carry `transient`, which drives the ✓/↷/✗ colour code (prd.md §13):
transient failures print ↷ yellow because retrying helps; permanent ones print
✗ red because the operator must do something first.
"""

from __future__ import annotations

from .constants import EXIT_ERROR, EXIT_INTERRUPTED, EXIT_PREFLIGHT


class WikimillError(Exception):
    """Base for every anticipated failure.

    Args:
        message: what went wrong, in the operator's terms.
        remediation: the exact next action — a command to run, a variable to
            set, a file to create. Omit only when genuinely nothing applies.
        transient: True when retrying unchanged might succeed.
    """

    exit_code: int = EXIT_ERROR

    def __init__(
        self,
        message: str,
        *,
        remediation: str | None = None,
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.remediation = remediation
        self.transient = transient

    def __str__(self) -> str:
        if self.remediation:
            return f"{self.message}\n  → {self.remediation}"
        return self.message


class ConfigError(WikimillError):
    """Missing or invalid configuration — a variable unset, a value unparseable."""


class PreflightError(WikimillError):
    """One or more preflight checks failed. Aborts before any work or network use."""

    exit_code = EXIT_PREFLIGHT


class StorageError(WikimillError):
    """Database unreachable, unmigratable, or in an unsafe location."""


class DumpError(WikimillError):
    """A dump file is missing, unreadable, truncated, or fails its checksum."""


class CrawlError(WikimillError):
    """A crawl-time failure. Usually transient — set `transient` accordingly."""


class NotImplementedYetError(WikimillError):
    """A command whose phase has not shipped.

    Better than a stack trace or a silent no-op: it names the phase, so the
    operator knows whether to wait or to check they are on the right version.
    """

    def __init__(self, command: str, phase: str) -> None:
        super().__init__(
            f"`{command}` is not implemented yet — it ships in {phase}.",
            remediation=f"See docs/prd.md §7 for the {phase} scope.",
        )
        self.command = command
        self.phase = phase


class Interrupted(WikimillError):
    """Ctrl-C. Checkpointed state is intact and the run can be resumed."""

    exit_code = EXIT_INTERRUPTED

    def __init__(self) -> None:
        super().__init__(
            "Interrupted.",
            remediation="Progress was checkpointed — re-run the same command to resume.",
            transient=True,
        )

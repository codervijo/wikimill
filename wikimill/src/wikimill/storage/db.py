"""SQLite connection and migration runner.

WAL mode, single file, host-mounted on local disk. WAL is what lets `stats` and
`inspect` read while a crawl writes — and it is also why the database must never
live on removable or non-POSIX media (exFAT/NTFS/USB detach break POSIX locking
and durable fsync; the failure mode is a corrupted database, not an error).
`preflight` checks for that; see architecture.md § Storage.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..errors import StorageError
from .schema import LATEST_VERSION, MIGRATIONS


def connect(db_path: Path, *, create: bool = True) -> sqlite3.Connection:
    """Open the database with the pragmas this project relies on."""
    if not db_path.parent.exists():
        if not create:
            raise StorageError(
                f"Database directory does not exist: {db_path.parent}",
                remediation="Run `wikimill preflight` to create it.",
            )
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(
                f"Cannot create database directory {db_path.parent}: {exc}",
                remediation="Check the mount and its permissions.",
            ) from exc
    if not create and not db_path.exists():
        raise StorageError(
            f"Database does not exist: {db_path}",
            remediation="Run `wikimill preflight` to create and migrate it.",
        )
    try:
        conn = sqlite3.connect(db_path, isolation_level=None)
    except sqlite3.Error as exc:
        # One SQLite error covers several very different causes; a generic
        # message sends the operator to the wrong remedy, so disambiguate.
        parent = db_path.parent
        if parent.exists() and not os.access(parent, os.W_OK):
            import pwd

            try:
                owner = pwd.getpwuid(parent.stat().st_uid).pw_name
            except (KeyError, OSError):
                owner = str(parent.stat().st_uid)
            remediation = (
                f"{parent} is not writable by this user (owned by {owner}). "
                "If it was created by an older root-run container, reclaim it: "
                f"sudo chown -R $(id -u):$(id -g) {parent}"
            )
        else:
            remediation = (
                "If this path is on an external or network drive, move it to "
                "local disk — SQLite WAL requires POSIX locking and a durable "
                "fsync. Only state/dumps/ may live on external media."
            )
        raise StorageError(f"Cannot open database {db_path}: {exc}", remediation=remediation) from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> tuple[int, int]:
    """Apply pending migrations. Returns (version_before, version_after).

    Idempotent: with nothing pending, this is a no-op and both values match —
    which is what lets `preflight` run before every command without cost.
    """
    before = user_version(conn)
    if before > LATEST_VERSION:
        raise StorageError(
            f"Database schema is v{before}, but this build only knows v{LATEST_VERSION}.",
            remediation=(
                "The database was written by a newer wikimill. Update this "
                "checkout rather than downgrading the database — migrations are "
                "forward-only."
            ),
        )
    for version in range(before + 1, LATEST_VERSION + 1):
        statements = MIGRATIONS[version - 1]
        try:
            conn.execute("BEGIN")
            for statement in statements:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version={version}")
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            conn.execute("ROLLBACK")
            raise StorageError(
                f"Migration to v{version} failed: {exc}",
                remediation=(
                    f"The database is unchanged at v{version - 1}. This is a bug "
                    "in the migration — report it rather than editing the schema."
                ),
            ) from exc
    return before, user_version(conn)


@contextmanager
def open_db(db_path: Path, *, create: bool = True) -> Iterator[sqlite3.Connection]:
    """Open, migrate, and always close."""
    conn = connect(db_path, create=create)
    try:
        migrate(conn)
        yield conn
    finally:
        conn.close()


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Row count per table — the backbone of `stats`."""
    out: dict[str, int] = {}
    for name in table_names(conn):
        out[name] = int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
    return out

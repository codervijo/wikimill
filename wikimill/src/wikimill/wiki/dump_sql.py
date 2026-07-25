"""Streaming parser for a MySQL `INSERT` dump.

**The dump is parsed, never executed.** `externallinks.sql.gz` is a MySQL dump
from a public, publicly-editable source; piping it into a database engine would
execute attacker-influenceable SQL (prd.md §18). So we read the byte stream and
pull tuples out of it ourselves.

Shape verified against real enwiki rows (2026-07-25):

    INSERT INTO `externallinks` VALUES (2,3850540,'http://edu.berkeley...','/housing/'),(3,...);

Three properties of the real file drive this implementation:

* **Statements are ~1 MB each** — thousands of tuples per line. Parsing is
  incremental so memory stays flat regardless.
* **Values contain escaped quotes** (`'…bleedin\\'_obvious'`), so splitting on
  `'` corrupts data. A character scanner honouring backslash escapes is required.
* **Columns are `blob`/`varbinary`**, so values may be NULL and may hold bytes
  that are not valid UTF-8. Decoding is lossy-but-recorded, never fatal.
"""

from __future__ import annotations

import gzip
import re
from collections.abc import Iterator
from pathlib import Path
from typing import IO

from ..errors import DumpError

# Guard against a decompression bomb: a public archive should never expand past
# a sane ratio, and we stream rather than materialise (prd.md §18).
MAX_EXPANSION_RATIO = 200
_READ_CHUNK = 1 << 20  # 1 MiB

_INSERT_PREFIX = re.compile(rb"^INSERT INTO `(\w+)` VALUES ", re.IGNORECASE)


def open_stream(path: Path) -> IO[bytes]:
    """Open a .sql.gz (or plain .sql) dump as a byte stream."""
    if not path.is_file():
        raise DumpError(
            f"Dump not found: {path}",
            remediation="Check WIKIMILL_DUMPS_DIR, or that the drive is mounted.",
        )
    try:
        if path.suffix == ".gz":
            return gzip.open(path, "rb")
        return path.open("rb")
    except OSError as exc:
        raise DumpError(
            f"Cannot read {path}: {exc}",
            remediation="Check permissions and that the drive is still mounted.",
        ) from exc


def parse_tuples(payload: bytes) -> Iterator[tuple[object, ...]]:
    """Yield each `(...)` tuple from an INSERT's VALUES clause.

    A hand-written scanner, because the two obvious shortcuts both break on real
    data: splitting on `'` corrupts escaped quotes, and a regex over a 1 MB line
    with nested quantifiers backtracks badly.
    """
    i, end = 0, len(payload)
    while i < end:
        # Advance to the start of the next tuple.
        while i < end and payload[i : i + 1] != b"(":
            if payload[i : i + 1] == b";":
                return
            i += 1
        if i >= end:
            return
        i += 1  # past '('
        values: list[object] = []
        field = bytearray()
        in_string = False
        escaped = False
        is_string = False
        while i < end:
            ch = payload[i : i + 1]
            if in_string:
                if escaped:
                    field += _unescape(ch)
                    escaped = False
                elif ch == b"\\":
                    escaped = True
                elif ch == b"'":
                    in_string = False
                else:
                    field += ch
                i += 1
                continue
            if ch == b"'":
                in_string = True
                is_string = True
                i += 1
                continue
            if ch in (b",", b")"):
                values.append(_coerce(bytes(field), is_string))
                field.clear()
                is_string = False
                i += 1
                if ch == b")":
                    break
                continue
            field += ch
            i += 1
        yield tuple(values)


def _unescape(ch: bytes) -> bytes:
    """MySQL backslash escapes. Anything else is a literal following character."""
    return {
        b"n": b"\n",
        b"t": b"\t",
        b"r": b"\r",
        b"0": b"\x00",
        b"b": b"\b",
        b"Z": b"\x1a",
    }.get(ch, ch)


def _coerce(raw: bytes, is_string: bool) -> object:
    """Turn a raw field into str / int / None.

    Columns are `blob`/`varbinary`, so bytes need not be valid UTF-8. Decoding
    is lossy rather than fatal: one malformed URL must not abort a 4.9 GB pass.
    """
    if is_string:
        return raw.decode("utf-8", errors="replace")
    token = raw.strip()
    if not token or token.upper() == b"NULL":
        return None
    try:
        return int(token)
    except ValueError:
        return token.decode("utf-8", errors="replace")


def iter_rows(path: Path, table: str = "externallinks") -> Iterator[tuple[object, ...]]:
    """Stream every row of `table` from the dump.

    Memory stays flat: statements are read one at a time and tuples are yielded
    as they are scanned, so a 4.9 GB dump costs no more than a ~1 MB statement.
    """
    want = table.encode("ascii")
    produced = 0
    # open_stream validates first: stat()-ing a missing file would raise a bare
    # FileNotFoundError and bypass the typed error that names the fix.
    with open_stream(path) as stream:
        compressed_size = path.stat().st_size
        buffer = b""
        while True:
            chunk = stream.read(_READ_CHUNK)
            if not chunk:
                break
            produced += len(chunk)
            if compressed_size and produced > compressed_size * MAX_EXPANSION_RATIO:
                raise DumpError(
                    f"{path.name} expanded past {MAX_EXPANSION_RATIO}x its compressed "
                    "size — refusing to continue.",
                    remediation="The file is corrupt or hostile. Re-download it.",
                )
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                match = _INSERT_PREFIX.match(line)
                if match and match.group(1) == want:
                    yield from parse_tuples(line[match.end() :])
        if buffer:
            match = _INSERT_PREFIX.match(buffer)
            if match and match.group(1) == want:
                yield from parse_tuples(buffer[match.end() :])


def dump_run_from_name(path: Path) -> str | None:
    """Extract the run date from a dump filename, e.g. enwiki-20260701-… -> 20260701.

    The SQL and XML dumps must come from the same run: a page revised between
    runs would otherwise yield context that does not match the link (prd.md §6).
    """
    match = re.search(r"-(\d{8})-", path.name)
    return match.group(1) if match else None


def lang_from_name(path: Path) -> str:
    """enwiki-… -> 'en', simplewiki-… -> 'simple'.

    Language codes are not all two or three letters (`simple`, `zh-yue`,
    `be-tarask`), so match everything before `wiki` rather than a fixed width.
    Defaults to 'en' when the name is unrecognised.
    """
    match = re.match(r"^([a-z][a-z_-]*?)wiki[_-]", path.name)
    return match.group(1) if match else "en"

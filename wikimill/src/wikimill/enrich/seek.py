"""Random access into the multistream article archive.

`pages-articles-multistream.xml.bz2` is a concatenation of independently
decompressable bz2 streams, each holding ~100 pages. Given a byte offset from
the companion index, one `seek()` plus one small decompression yields those
pages — without touching the other 26 GB.

This is the mechanism the entire pipeline ordering rests on. Because retrieving
any page is cheap and offline, context extraction can be deferred until a link
has proven interesting, instead of being harvested up front and mostly thrown
away.

Guarded as hostile input (prd.md §18): the offset is operator-influenced and the
archive is a public download, so decompression is bounded by both output size
and expansion ratio. A bad offset must fail cleanly rather than consume the
machine.
"""

from __future__ import annotations

import bz2
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ..errors import DumpError

# One block holds ~100 pages. This ceiling is far above that and exists purely
# so a wrong offset cannot decompress unboundedly.
MAX_BLOCK_BYTES = 64 * 1024 * 1024
MAX_EXPANSION_RATIO = 400
_READ_CHUNK = 1 << 20

_PAGE = re.compile(rb"<page>.*?</page>", re.DOTALL)
_ID = re.compile(rb"<id>(\d+)</id>")
_TITLE = re.compile(rb"<title>(.*?)</title>", re.DOTALL)
_TEXT = re.compile(rb'<text[^>]*>(.*?)</text>', re.DOTALL)
_REDIRECT = re.compile(rb"<redirect ")


@dataclass(frozen=True)
class Page:
    page_id: int
    title: str
    wikitext: str
    is_redirect: bool


def find_archive(dumps_dir: Path) -> Path:
    """Locate the multistream article archive (whole or partitioned)."""
    matches = sorted(
        p
        for p in dumps_dir.glob("*-pages-articles-multistream*.xml*.bz2")
        if "index" not in p.name
    )
    if not matches:
        raise DumpError(
            f"No multistream article dump found in {dumps_dir}",
            remediation=(
                "Download <wiki>-<date>-pages-articles-multistream.xml.bz2 (or a "
                "matching part file) from https://dumps.wikimedia.org/ into that "
                "directory. Only `enrich` needs it — the earlier stages do not."
            ),
        )
    return matches[-1]


def read_block(archive: Path, offset: int) -> bytes:
    """Decompress exactly the one stream beginning at `offset`.

    `BZ2Decompressor` stops at the end of its stream and exposes the remainder
    as `unused_data`, which is what makes a concatenated archive addressable at
    all — a plain `bz2.open()` would run on into the following blocks.
    """
    if offset < 0:
        raise DumpError(f"Negative multistream offset: {offset}")
    try:
        size = archive.stat().st_size
    except OSError as exc:
        raise DumpError(
            f"Cannot stat {archive}: {exc}",
            remediation="Check the drive is still mounted.",
        ) from exc
    if offset >= size:
        raise DumpError(
            f"Offset {offset} is past the end of {archive.name} ({size} bytes).",
            remediation=(
                "The index and the archive are probably from different dump runs "
                "or different part files. wikimill pins both to one run."
            ),
        )

    decompressor = bz2.BZ2Decompressor()
    out = bytearray()
    consumed = 0
    try:
        with archive.open("rb") as fh:
            fh.seek(offset)
            while not decompressor.eof:
                chunk = fh.read(_READ_CHUNK)
                if not chunk:
                    break
                consumed += len(chunk)
                out += decompressor.decompress(chunk)
                if len(out) > MAX_BLOCK_BYTES:
                    raise DumpError(
                        f"Block at offset {offset} exceeded {MAX_BLOCK_BYTES} bytes.",
                        remediation="The offset is wrong, or the archive is corrupt.",
                    )
                if consumed and len(out) > consumed * MAX_EXPANSION_RATIO:
                    raise DumpError(
                        f"Block at offset {offset} expanded past "
                        f"{MAX_EXPANSION_RATIO}x — refusing to continue.",
                        remediation="Re-download the archive.",
                    )
    except OSError as exc:
        raise DumpError(
            f"Cannot read {archive} at offset {offset}: {exc}",
            remediation="Check the drive is still mounted.",
        ) from exc
    except (EOFError, ValueError) as exc:
        raise DumpError(
            f"Offset {offset} is not the start of a bz2 stream: {exc}",
            remediation=(
                "Multistream offsets come from the matching index file. A whole-"
                "archive index will not address a part file, or vice versa."
            ),
        ) from exc
    return bytes(out)


def parse_pages(block: bytes) -> Iterator[Page]:
    """Extract pages from one decompressed block.

    Regex rather than an XML parser: a block is a *fragment*, not a document —
    it has no root element — so a conforming parser rejects it outright. The
    shape is machine-generated and rigidly consistent, which is what makes this
    safe here and would not make it safe on arbitrary XML.
    """
    for match in _PAGE.finditer(block):
        raw = match.group(0)
        id_match = _ID.search(raw)
        title_match = _TITLE.search(raw)
        text_match = _TEXT.search(raw)
        if not id_match or not title_match:
            continue
        yield Page(
            page_id=int(id_match.group(1)),
            title=_unescape(title_match.group(1).decode("utf-8", "replace")),
            wikitext=_unescape(
                text_match.group(1).decode("utf-8", "replace") if text_match else ""
            ),
            is_redirect=bool(_REDIRECT.search(raw)),
        )


def _unescape(text: str) -> str:
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#039;", "'")
        .replace("&apos;", "'")
        .replace("&amp;", "&")  # last: an escaped ampersand must not double-decode
    )


def pages_at(archive: Path, offset: int) -> dict[int, Page]:
    """Every page in the block at `offset`, keyed by page id."""
    return {page.page_id: page for page in parse_pages(read_block(archive, offset))}

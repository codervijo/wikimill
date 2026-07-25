"""The multistream index — random access into the article dump.

`pages-articles-multistream.xml.bz2` is a concatenation of independently
decompressable bz2 streams, each holding ~100 pages. Its companion index is one
line per page:

    offset:page_id:page title

Two things fall out of that, and both shape the whole project:

1. **Enrichment is a seek, not a scan** (v1.H). We persist `ms_offset` per page,
   so retrieving one page costs one seek plus one small block decompress.
2. **`page.sql.gz` (2.4 GB) is unnecessary** — the index already carries
   `page_id -> title`.

The index is also our namespace filter. `externallinks` has no namespace column,
so `el_from` is intersected with the page IDs in this index. Whether that is a
*clean* article-namespace filter is a hypothesis, not an assumption — see
`namespace_report()`, which measures it (acceptance criterion 4).
"""

from __future__ import annotations

import bz2
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ..errors import DumpError

# Namespaces that appear as a `Prefix:` on a title. Used only to *measure*
# whether the article dump's index is article-only — never to guess.
KNOWN_NAMESPACE_PREFIXES = (
    "Media", "Special", "Talk", "User", "User talk", "Wikipedia", "Wikipedia talk",
    "File", "File talk", "MediaWiki", "MediaWiki talk", "Template", "Template talk",
    "Help", "Help talk", "Category", "Category talk", "Portal", "Portal talk",
    "Draft", "Draft talk", "TimedText", "TimedText talk", "Module", "Module talk",
    "Image", "Image talk", "Book", "Book talk",
)
_NS_RE = re.compile(r"^(" + "|".join(re.escape(p) for p in KNOWN_NAMESPACE_PREFIXES) + r"):")

_PAGE_RANGE = re.compile(r"^p?(\d+)[p\-](\d+)$", re.IGNORECASE)


@dataclass(frozen=True)
class IndexEntry:
    offset: int
    page_id: int
    title: str


@dataclass(frozen=True)
class PageRange:
    """An inclusive page-ID slice, e.g. `p1p41242` or `1-41242`."""

    start: int
    end: int

    def contains(self, page_id: int) -> bool:
        return self.start <= page_id <= self.end

    def __str__(self) -> str:
        return f"p{self.start}p{self.end}"


def parse_page_range(text: str) -> PageRange:
    """Parse `p1p41242` (the dump's own part naming) or `1-41242`."""
    match = _PAGE_RANGE.match(text.strip())
    if not match:
        raise DumpError(
            f"Cannot parse page range {text!r}.",
            remediation="Use the dump's own form, e.g. --pages p1p41242 (or 1-41242).",
        )
    start, end = int(match.group(1)), int(match.group(2))
    if start > end:
        raise DumpError(f"Page range {text!r} starts after it ends.")
    return PageRange(start, end)


def find_index_files(dumps_dir: Path) -> list[Path]:
    """Locate index files — the combined index, or the per-part ones."""
    files = sorted(dumps_dir.glob("*-pages-articles-multistream-index*.txt*.bz2"))
    if not files:
        raise DumpError(
            f"No multistream index found in {dumps_dir}",
            remediation=(
                "Download <wiki>-<date>-pages-articles-multistream-index.txt.bz2 "
                "from https://dumps.wikimedia.org/ into that directory."
            ),
        )
    return files


def iter_index(path: Path) -> Iterator[IndexEntry]:
    """Stream a bz2 index file. Malformed lines are skipped, never fatal."""
    try:
        with bz2.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                # Title may itself contain ':', so split only twice.
                parts = line.rstrip("\n").split(":", 2)
                if len(parts) != 3:
                    continue
                try:
                    yield IndexEntry(int(parts[0]), int(parts[1]), parts[2])
                except ValueError:
                    continue
    except OSError as exc:
        raise DumpError(
            f"Cannot read index {path}: {exc}",
            remediation="Re-download it, or check the drive is still mounted.",
        ) from exc


def is_namespaced(title: str) -> bool:
    """True when a title carries a known `Namespace:` prefix.

    Only *known* prefixes count. A real article can contain a colon
    ("Star Trek: First Contact"), and dropping those would silently lose
    encyclopedic pages — the exact opposite of what this filter is for.
    """
    return _NS_RE.match(title) is not None


def iter_range(
    paths: list[Path],
    page_range: PageRange | None,
    *,
    articles_only: bool = False,
) -> Iterator[IndexEntry]:
    """Stream index entries, optionally restricted to a page-ID slice.

    `articles_only` drops known-namespaced titles. Measured on the real
    20260701 index (slice p1p41242, 27,353 entries): 99.27% are articles, the
    remainder being Wikipedia:/Portal:/Help:/Draft: pages. So the index is a
    *good* namespace proxy but not a clean one — hence this filter rather than
    the assumption that intersection alone suffices.
    """
    for path in paths:
        for entry in iter_index(path):
            if page_range is not None and not page_range.contains(entry.page_id):
                continue
            if articles_only and is_namespaced(entry.title):
                continue
            yield entry


def namespace_report(entries: Iterator[IndexEntry], sample: int = 200_000) -> dict:
    """Measure whether the index is a clean article-namespace filter.

    Acceptance criterion 4 requires this be *verified*, not assumed. Counts
    titles carrying a known `Namespace:` prefix. A real article can legitimately
    contain a colon ("Star Trek: First Contact"), which is why only known
    namespace prefixes count — and why this is reported as evidence for a human
    to judge rather than used to silently drop rows.
    """
    total = 0
    flagged: dict[str, int] = {}
    examples: dict[str, str] = {}
    for entry in entries:
        total += 1
        match = _NS_RE.match(entry.title)
        if match:
            ns = match.group(1)
            flagged[ns] = flagged.get(ns, 0) + 1
            examples.setdefault(ns, entry.title)
        if total >= sample:
            break
    return {
        "sampled": total,
        "namespaced": sum(flagged.values()),
        "by_namespace": dict(sorted(flagged.items(), key=lambda kv: -kv[1])),
        "examples": examples,
        "article_fraction": (
            1.0 - sum(flagged.values()) / total if total else 0.0
        ),
    }

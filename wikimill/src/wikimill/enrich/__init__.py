"""Stage 6 — lazy context enrichment.

The expensive half of the pipeline, and the reason for its ordering: it runs
only on links already classified as interesting, and reaches them by seeking
directly into the multistream archive rather than scanning it.
"""

from .runner import EnrichStats
from .runner import run as enrich
from .seek import Page, find_archive, pages_at, parse_pages, read_block
from .select import Candidate, count_pending, group_by_block, parse_states, select
from .wikitext import LinkContext, extract, find_link, section_at

__all__ = [
    "Candidate",
    "EnrichStats",
    "LinkContext",
    "Page",
    "count_pending",
    "enrich",
    "extract",
    "find_archive",
    "find_link",
    "group_by_block",
    "pages_at",
    "parse_pages",
    "parse_states",
    "read_block",
    "section_at",
    "select",
]

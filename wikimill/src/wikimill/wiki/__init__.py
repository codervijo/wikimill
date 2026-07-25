"""Wikipedia ingestion: dump readers and URL reconstruction.

Nothing here touches the network. Everything reads local dump files, which is
what makes ingestion reproducible and testable against small fixtures.
"""

from .eldomain import ReconstructedUrl, ReconstructionError, reconstruct
from .msindex import PageRange, find_index_files, iter_range, parse_page_range

__all__ = [
    "PageRange",
    "ReconstructedUrl",
    "ReconstructionError",
    "find_index_files",
    "iter_range",
    "parse_page_range",
    "reconstruct",
]

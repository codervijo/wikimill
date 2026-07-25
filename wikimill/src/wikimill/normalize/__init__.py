"""URL and domain normalization (prd.md §10).

Stage 2 of the pipeline. Runs inside `ingest`, and is a pure function: the same
input always produces the same `url_normalized`, *for a given
`NORMALIZER_VERSION`*. That caveat is why the version is stamped on every row.
"""

from .archive import ARCHIVE_HOSTS, is_archive_host, unwrap
from .domain import DomainInfo, analyse
from .url import DropReason, NormalizedUrl, normalize, url_hash

__all__ = [
    "ARCHIVE_HOSTS",
    "DomainInfo",
    "DropReason",
    "NormalizedUrl",
    "analyse",
    "is_archive_host",
    "normalize",
    "unwrap",
    "url_hash",
]

"""Unwrap web-archive URLs to the origin they preserve.

This matters more here than in most crawlers. A large share of Wikipedia's
citations point at `web.archive.org` rather than the source, so crawling the
wrapper measures the Internet Archive's uptime instead of the cited domain's —
producing results that are confidently wrong. Unwrapping happens once, at
normalization, so no later stage can get it wrong (prd.md §10 rule 9).

Both parts are kept: the origin URL is what gets queued and crawled, and the
wrapper is recorded on the link row as `archive_url`.

Some archives (ghostarchive, webcitation) address snapshots by opaque ID and
embed no origin URL. Those are recognised as archive hosts but cannot be
unwrapped — recorded honestly rather than guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Hosts whose URLs wrap another URL, with the origin embedded in the path.
WAYBACK_HOSTS = frozenset({"web.archive.org", "web.archive.org.wstub.archive.org"})

ARCHIVE_TODAY_HOSTS = frozenset(
    {
        "archive.today", "archive.ph", "archive.is", "archive.li",
        "archive.fo", "archive.md", "archive.vn", "archive.ec",
    }
)

# Archive hosts with no recoverable origin URL — opaque snapshot IDs.
OPAQUE_ARCHIVE_HOSTS = frozenset(
    {"ghostarchive.org", "www.webcitation.org", "webcitation.org", "archive.org"}
)

ARCHIVE_HOSTS = WAYBACK_HOSTS | ARCHIVE_TODAY_HOSTS | OPAQUE_ARCHIVE_HOSTS

# /web/20200101000000/https://example.com/x  — the flags (id_, im_, js_, cs_)
# select a raw or rewritten rendition and are not part of the origin URL.
_WAYBACK = re.compile(
    r"^/web/(?P<ts>\d{4,14})(?:[a-z]{2}_)?/(?P<url>.+)$", re.IGNORECASE
)
# archive.today: /<shortcode-or-timestamp>/<url>, or /newest/<url>
_ARCHIVE_TODAY = re.compile(
    r"^/(?:(?P<ts>\d{4,14})|newest|oldest|[A-Za-z0-9]{5})/(?P<url>https?://.+)$"
)


@dataclass(frozen=True)
class Unwrapped:
    origin_url: str
    archive_url: str
    archive_date: str | None


def is_archive_host(host: str) -> bool:
    return host.lower() in ARCHIVE_HOSTS


def unwrap(url: str, host: str, path_and_query: str) -> Unwrapped | None:
    """Extract the origin URL a wrapper preserves, or None if there is none.

    `path_and_query` must carry the query string too: a wrapped URL's own query
    lives there (`…/https://example.com/a?b=c`), and dropping it would queue a
    different resource than the one that was cited.
    """
    lowered = host.lower()

    if lowered in WAYBACK_HOSTS:
        match = _WAYBACK.match(path_and_query)
        if match:
            return Unwrapped(
                origin_url=_repair_scheme(match.group("url")),
                archive_url=url,
                archive_date=match.group("ts"),
            )
        return None

    if lowered in ARCHIVE_TODAY_HOSTS:
        match = _ARCHIVE_TODAY.match(path_and_query)
        if match:
            return Unwrapped(
                origin_url=_repair_scheme(match.group("url")),
                archive_url=url,
                archive_date=match.group("ts"),
            )
        return None

    return None


def _repair_scheme(url: str) -> str:
    """Archive paths often lose a slash: `https:/example.com` -> `https://…`.

    Real wayback URLs contain both forms, and a normalizer that does not repair
    this yields a hostless URL that silently drops out of the queue.
    """
    match = re.match(r"^(https?):/{1,2}(?!/)(.*)$", url, re.IGNORECASE)
    if match:
        return f"{match.group(1).lower()}://{match.group(2)}"
    if not re.match(r"^[a-z][a-z0-9+.\-]*://", url, re.IGNORECASE):
        # A scheme-less origin (`example.com/x`) — assume http, the scheme
        # Wikipedia's own archived links overwhelmingly used.
        return f"http://{url}"
    return url

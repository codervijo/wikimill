"""URL canonicalization — RFC 3986, then a deliberately conservative policy layer.

The output is `url_normalized`; `sha256(url_normalized)` is the identity key for
the whole system. Two consequences shape every rule below:

* **Over-normalizing changes what we fetch.** `/foo` and `/foo/` are different
  resources to many servers, query order can matter, and a stripped parameter
  can change the page. When in doubt this module leaves the URL alone — a
  false merge is far worse than a missed one, because it silently attributes
  one site's liveness to another.
* **Changing a rule changes every hash.** `NORMALIZER_VERSION` is stamped on
  every `urls` row so a ruleset change is detectable rather than silently
  forking identity across the table.

Rules implemented here are prd.md §10 1-12.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..constants import NORMALIZER_VERSION
from . import archive
from .domain import DomainInfo, analyse

CRAWLABLE_SCHEMES = frozenset({"http", "https"})
DEFAULT_PORTS = {"http": 80, "https": 443}

# Unambiguous analytics/click identifiers. Kept tight on purpose: stripping a
# parameter that is load-bearing would make us fetch a different page than the
# one Wikipedia cited. Anything ambiguous (`ref`, `id`, `source`) stays.
TRACKING_PARAMS = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "utm_id", "utm_name", "utm_reader", "utm_brand", "utm_social",
        "utm_social-type", "utm_place", "utm_pubreferrer", "utm_swu",
        "fbclid", "gclid", "gclsrc", "dclid", "wbraid", "gbraid", "msclkid",
        "yclid", "twclid", "igshid", "igsh", "ttclid", "li_fat_id",
        "mc_cid", "mc_eid", "_ga", "_gl", "_hsenc", "_hsmi", "hsCtaTracking",
        "vero_conv", "vero_id", "oly_anon_id", "oly_enc_id", "s_cid",
        "mkt_tok", "trk", "trkCampaign", "sc_campaign", "sc_channel",
    }
)

_UNRESERVED = re.compile(rb"[A-Za-z0-9\-._~]")
_PCT = re.compile(r"%([0-9A-Fa-f]{2})")


class DropReason(StrEnum):
    """Why a URL never reaches the crawl queue. Counted, never silent."""

    NOT_CRAWLABLE_SCHEME = "not_crawlable_scheme"
    WIKIMEDIA_INTERNAL = "wikimedia_internal"
    IDENTIFIER_RESOLVER = "identifier_resolver"
    NO_HOST = "no_host"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class NormalizedUrl:
    url: str
    scheme: str
    host: str
    port: int | None
    domain: DomainInfo
    archive_url: str | None = None
    archive_date: str | None = None
    drop_reason: DropReason | None = None
    normalizer_version: int = NORMALIZER_VERSION

    @property
    def keep(self) -> bool:
        return self.drop_reason is None


def _dropped(reason: DropReason, url: str = "") -> NormalizedUrl:
    return NormalizedUrl(
        url=url,
        scheme="",
        host="",
        port=None,
        domain=analyse(""),
        drop_reason=reason,
    )


def url_hash(normalized_url: str) -> str:
    """The system-wide identity key: SHA-256 hex of the normalized URL.

    Always hash the *normalized* form — hashing a raw URL would give the same
    resource several identities and defeat the whole dedup stage.
    """
    return hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()


def remove_dot_segments(path: str) -> str:
    """RFC 3986 §5.2.4. `/a/./b/../c` -> `/a/c`."""
    output: list[str] = []
    for segment in path.split("/"):
        if segment == ".":
            continue
        if segment == "..":
            if output and output[-1] != "":
                output.pop()
            continue
        output.append(segment)
    result = "/".join(output)
    if path.startswith("/") and not result.startswith("/"):
        result = "/" + result
    return result


def normalize_percent_encoding(text: str) -> str:
    """Uppercase hex digits, and decode octets that never needed encoding.

    `%7Euser` and `%7euser` are the same resource as `~user`; leaving all three
    distinct would triple-count one URL.
    """

    def replace(match: re.Match[str]) -> str:
        octet = bytes.fromhex(match.group(1))
        if _UNRESERVED.fullmatch(octet):
            return octet.decode("ascii")
        return "%" + match.group(1).upper()

    return _PCT.sub(replace, text)


def normalize_host(host: str) -> str:
    """Lowercase, strip a trailing dot, and punycode any non-ASCII label."""
    lowered = host.strip().rstrip(".").lower()
    if not lowered:
        return ""
    if lowered.isascii():
        return lowered
    try:
        import idna

        return idna.encode(lowered, uts46=True, transitional=False).decode("ascii")
    except Exception:
        # An unencodable IDN is left as-is rather than dropped: it is still a
        # real citation, and mangling it would be worse than passing it through.
        return lowered


def strip_tracking(query: str) -> str:
    """Remove known tracking parameters, preserving the order of the rest.

    Order is preserved deliberately — sorting is a common "normalization" that
    breaks order-sensitive servers and buys nothing here.
    """
    if not query:
        return ""
    pairs = parse_qsl(query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if k.lower() not in TRACKING_PARAMS]
    if len(kept) == len(pairs):
        return query  # untouched — do not re-encode and risk changing it
    return urlencode(kept)


def normalize(raw: str) -> NormalizedUrl:
    """Canonicalize one URL and decide whether it belongs in the queue."""
    candidate = (raw or "").strip()
    if not candidate:
        return _dropped(DropReason.MALFORMED, raw)

    archive_url: str | None = None
    archive_date: str | None = None

    # Unwrap archives first: everything after this operates on the origin URL,
    # so no later rule can accidentally normalize the wrapper instead.
    for _ in range(2):  # at most one nesting level; archives do get double-wrapped
        try:
            parts = urlsplit(candidate)
        except ValueError:
            return _dropped(DropReason.MALFORMED, raw)
        if not parts.hostname or not archive.is_archive_host(parts.hostname):
            break
        tail = parts.path + (f"?{parts.query}" if parts.query else "")
        unwrapped = archive.unwrap(candidate, parts.hostname, tail)
        if unwrapped is None:
            break  # an opaque archive (ghostarchive, webcitation) — keep as-is
        archive_url = archive_url or unwrapped.archive_url
        archive_date = archive_date or unwrapped.archive_date
        candidate = unwrapped.origin_url

    try:
        parts = urlsplit(candidate)
    except ValueError:
        return _dropped(DropReason.MALFORMED, raw)

    scheme = parts.scheme.lower()
    if scheme not in CRAWLABLE_SCHEMES:
        return _dropped(DropReason.NOT_CRAWLABLE_SCHEME, candidate)

    host = normalize_host(parts.hostname or "")
    if not host:
        return _dropped(DropReason.NO_HOST, candidate)

    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None and port == DEFAULT_PORTS.get(scheme):
        port = None

    path = normalize_percent_encoding(remove_dot_segments(parts.path)) or "/"
    query = normalize_percent_encoding(strip_tracking(parts.query))

    domain = analyse(host)
    if domain.is_wikimedia:
        return _dropped(DropReason.WIKIMEDIA_INTERNAL, candidate)
    if domain.is_resolver:
        return _dropped(DropReason.IDENTIFIER_RESOLVER, candidate)

    authority = f"{host}:{port}" if port is not None else host
    # Fragment is always dropped: it is never sent to the server and cannot
    # affect whether a page is alive.
    normalized = urlunsplit((scheme, authority, path, query, ""))

    return NormalizedUrl(
        url=normalized,
        scheme=scheme,
        host=host,
        port=port,
        domain=domain,
        archive_url=archive_url,
        archive_date=archive_date,
    )

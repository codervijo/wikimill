"""Reconstruct a URL from the `externallinks` columns.

MediaWiki ≥1.40 stores links as `el_to_domain_index` + `el_to_path`, where the
domain index is the scheme plus a **reversed, dot-terminated** host. Getting
this subtly wrong corrupts every domain in the database, so the rules below were
verified against real enwiki dump rows (2026-07-25) rather than inferred:

    http://edu.berkeley.housing.www.   ->  http://www.housing.berkeley.edu
    http://uk.co.bbc.news.             ->  http://news.bbc.co.uk
    http://uk.co.linearb.:8080         ->  http://linearb.co.uk:8080
    http://V4.66.102.9.104.            ->  http://66.102.9.104

Two of those are traps:

* **IP addresses are NOT reversed.** They carry a `V4.`/`V6.` marker and appear
  in normal order. Reversing them would corrupt every IP host.
* **The port follows the trailing dot** (`…linearb.:8080`), so it must be split
  off before the labels are reversed.

`el_to_path` is a blob and may be NULL, which means "no path" — not an error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Schemes we will actually crawl. Everything else is recorded but never queued
# (prd.md §10 rule 10) — real dumps contain irc/ftp/gopher/telnet/worldwind.
CRAWLABLE_SCHEMES = frozenset({"http", "https"})

_SCHEME_SPLIT = re.compile(r"^([a-z0-9+.\-]+)://(.*)$", re.IGNORECASE)
# Real dumps also hold non-hierarchical schemes written `scheme:` with no `//`
# — `mailto:com.gmail.@LordRM`, `news:ada.lang.comp.`. They have no host to
# un-reverse, but they are a *known, expected* category, not malformed data, so
# they parse into an opaque result and are counted rather than logged as errors.
_OPAQUE_SPLIT = re.compile(r"^([a-z0-9+.\-]+):(.*)$", re.IGNORECASE)
_PORT_SUFFIX = re.compile(r":(\d{1,5})$")


class ReconstructionError(ValueError):
    """The domain index did not match any known form. Recorded, never guessed."""


@dataclass(frozen=True)
class ReconstructedUrl:
    url: str
    scheme: str
    host: str
    port: int | None
    path: str
    is_ip: bool
    opaque: bool = False
    """True for non-hierarchical schemes (`mailto:`, `news:`) that carry no host."""

    @property
    def crawlable(self) -> bool:
        return not self.opaque and self.scheme in CRAWLABLE_SCHEMES


def unreverse_host(reversed_host: str) -> tuple[str, bool]:
    """Turn a reversed, dot-terminated host into a real one.

    Returns (host, is_ip). Raises ReconstructionError on an empty host.
    """
    body = reversed_host.rstrip(".")
    if not body:
        raise ReconstructionError("empty host in domain index")

    # IP literals carry a marker and are stored in normal order.
    if body.startswith(("V4.", "V6.")):
        return body[3:], True

    labels = [label for label in body.split(".") if label]
    if not labels:
        raise ReconstructionError(f"no labels in {reversed_host!r}")
    return ".".join(reversed(labels)), False


def reconstruct(domain_index: str, path: str | None) -> ReconstructedUrl:
    """Rebuild the URL MediaWiki recorded, from the two stored columns."""
    match = _SCHEME_SPLIT.match(domain_index)
    if not match:
        opaque = _OPAQUE_SPLIT.match(domain_index)
        if opaque:
            # No host to un-reverse. Recorded and counted under its scheme;
            # never queued. Reconstructing a mailto address is not attempted —
            # we do not invent what we cannot verify.
            scheme = opaque.group(1).lower()
            return ReconstructedUrl(
                url=f"{scheme}:{opaque.group(2)}{path or ''}",
                scheme=scheme,
                host="",
                port=None,
                path=path or "",
                is_ip=False,
                opaque=True,
            )
        raise ReconstructionError(f"no scheme in {domain_index!r}")
    scheme = match.group(1).lower()
    rest = match.group(2)

    port: int | None = None
    port_match = _PORT_SUFFIX.search(rest)
    if port_match:
        # Only strip it when it really is a trailing port: the reversed host is
        # dot-terminated, so a genuine port is preceded by that dot.
        candidate = rest[: port_match.start()]
        if candidate.endswith("."):
            value = int(port_match.group(1))
            if 0 < value <= 65535:
                port = value
                rest = candidate

    host, is_ip = unreverse_host(rest)
    authority = f"{host}:{port}" if port is not None else host
    tail = path or ""
    return ReconstructedUrl(
        url=f"{scheme}://{authority}{tail}",
        scheme=scheme,
        host=host,
        port=port,
        path=tail,
        is_ip=is_ip,
    )

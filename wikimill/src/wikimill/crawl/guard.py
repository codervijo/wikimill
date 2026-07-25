"""SSRF and address-space guards.

Every URL we crawl came from a **publicly editable wiki**. Anyone can add a link
to Wikipedia, so every target is treated as hostile (prd.md §18).

The rule is resolve-then-check, applied at **every redirect hop**. Checking only
the first hop is the standard way to get this wrong: a target that answers with
a redirect to `169.254.169.254` reaches cloud metadata unless each hop is
re-validated.

**Known limitation, stated rather than papered over.** There is a TOCTOU gap
between our resolution and httpx's own: a hostile DNS server could return a
public address to us and a private one microseconds later. Closing it fully
needs a transport that pins the connection to the address we validated. We do
not do that yet, because wikimill sends no credentials and reads no response
into a trust boundary, so the residual exposure is a request being made rather
than data being disclosed. It is logged as a tracked refactor, not forgotten.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass


class BlockedAddress(Exception):
    """The host resolved into an address range we refuse to contact."""

    def __init__(self, host: str, address: str, reason: str) -> None:
        super().__init__(f"{host} resolved to {address} ({reason})")
        self.host = host
        self.address = address
        self.reason = reason


@dataclass(frozen=True)
class Resolution:
    host: str
    addresses: tuple[str, ...]


def classify_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Return a refusal reason, or None when the address is safe to contact.

    `ipaddress`' own predicates cover the ranges that matter; the cloud metadata
    address is called out separately because it is link-local *and* the single
    most valuable SSRF target, so a reader should see it named.
    """
    # Order matters for the *label*, not the outcome — every branch blocks. But
    # `is_private` is true for link-local and unspecified addresses too, so the
    # specific checks come first or the diagnostic would always read
    # "private range" and tell the operator nothing useful.
    if str(ip) == "169.254.169.254":
        return "cloud metadata endpoint"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_multicast:
        return "multicast"
    if ip.is_private:
        return "private range"
    if ip.is_reserved:
        return "reserved"
    return None


def check_address(host: str, address: str) -> None:
    """Raise BlockedAddress if this literal address must not be contacted."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return  # not an address literal; nothing to judge here
    # IPv4-mapped IPv6 (::ffff:127.0.0.1) would otherwise slip past the v6
    # predicates while still reaching a v4 loopback.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    reason = classify_address(ip)
    if reason is not None:
        raise BlockedAddress(host, address, reason)


def resolve_and_check(host: str, *, resolver=socket.getaddrinfo) -> Resolution:
    """Resolve a hostname and refuse it if *any* address is in a blocked range.

    Any, not all: a host that resolves to both a public and a private address is
    a classic bypass, and contacting it would be a coin flip.
    """
    check_address(host, host)  # host may itself be an address literal
    try:
        infos = resolver(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise LookupError(f"DNS resolution failed for {host}: {exc}") from exc
    addresses = tuple(dict.fromkeys(info[4][0] for info in infos))
    if not addresses:
        raise LookupError(f"DNS resolution returned no addresses for {host}")
    for address in addresses:
        check_address(host, address)
    return Resolution(host=host, addresses=addresses)

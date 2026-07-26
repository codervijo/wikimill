"""Stage 5 — domain checks: DNS + RDAP.

The only place `unregistered` can be established, and deliberately hard to
satisfy: two independent resolvers must agree on NXDOMAIN *and* the
authoritative registry must return an explicit 404.
"""

from .dns import DnsResult, DnsStatus, lookup
from .rdap import Bootstrap, RdapResult, RdapStatus, expires_within, query
from .rules import DomainVerdict, classify
from .runner import CheckStats
from .runner import run as check

__all__ = [
    "Bootstrap",
    "CheckStats",
    "DnsResult",
    "DnsStatus",
    "DomainVerdict",
    "RdapResult",
    "RdapStatus",
    "check",
    "classify",
    "expires_within",
    "lookup",
    "query",
]

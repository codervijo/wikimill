"""Multi-resolver DNS lookups.

**A single resolver's NXDOMAIN is never trusted.** A false NXDOMAIN produces a
fabricated "available domain" — the most expensive error this tool can make,
because the operator would act on it and try to buy something that is not for
sale. So a not-exists verdict requires at least two independent resolvers to
agree, and `resolvers_agreed` is recorded alongside the result (prd.md §13).

Queried NS-first: the question is "does this domain exist in the registry?",
which delegation answers more directly than an address record. A domain can
legitimately have no A record while being very much registered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

DEFAULT_TIMEOUT = 5.0
DEFAULT_LIFETIME = 8.0


class DnsStatus(StrEnum):
    OK = "ok"
    NXDOMAIN = "nxdomain"
    SERVFAIL = "servfail"
    TIMEOUT = "timeout"
    NO_RECORDS = "no_records"
    ERROR = "error"


@dataclass
class ResolverAnswer:
    """One resolver's opinion."""

    resolver: str
    status: DnsStatus
    ns_records: list[str] = field(default_factory=list)
    a_records: list[str] = field(default_factory=list)
    detail: str | None = None


@dataclass
class DnsResult:
    """The corroborated view across resolvers."""

    status: DnsStatus
    ns_records: list[str] = field(default_factory=list)
    a_records: list[str] = field(default_factory=list)
    answers: list[ResolverAnswer] = field(default_factory=list)
    resolvers_agreed: bool = False

    @property
    def confirmed_nxdomain(self) -> bool:
        """True only when ≥2 resolvers independently said the name does not exist
        **and no resolver said otherwise**.

        This is the gate on `unregistered`. Nothing else may open it. The
        second condition matters: counting NXDOMAIN votes alone would confirm a
        domain as gone even while another resolver was happily resolving it.
        """
        if self.status is not DnsStatus.NXDOMAIN:
            return False
        agreeing = [a for a in self.answers if a.status is DnsStatus.NXDOMAIN]
        return len(agreeing) >= 2


def _query(resolver_ip: str, name: str, timeout: float) -> ResolverAnswer:
    """Ask one resolver about one name. Never raises."""
    try:
        import dns.exception
        import dns.resolver
    except ImportError as exc:  # pragma: no cover - declared dependency
        from ..errors import ConfigError

        raise ConfigError(
            f"dnspython is not installed ({exc}).",
            remediation="Run `uv sync`, or rebuild the image if pyproject changed.",
        ) from exc

    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = [resolver_ip]
    resolver.timeout = timeout
    resolver.lifetime = min(DEFAULT_LIFETIME, timeout * 2)

    answer = ResolverAnswer(resolver=resolver_ip, status=DnsStatus.ERROR)
    try:
        ns = resolver.resolve(name, "NS")
        answer.ns_records = sorted(str(r.target).rstrip(".") for r in ns)
        answer.status = DnsStatus.OK
    except dns.resolver.NXDOMAIN:
        answer.status = DnsStatus.NXDOMAIN
        return answer
    except dns.resolver.NoAnswer:
        # The name exists but is not delegated here — still registered.
        answer.status = DnsStatus.NO_RECORDS
    except dns.resolver.NoNameservers as exc:
        answer.status = DnsStatus.SERVFAIL
        answer.detail = str(exc)[:200]
        return answer
    except dns.exception.Timeout:
        answer.status = DnsStatus.TIMEOUT
        return answer
    except Exception as exc:  # noqa: BLE001 - one resolver failing is not fatal
        answer.status = DnsStatus.ERROR
        answer.detail = f"{type(exc).__name__}: {exc}"[:200]
        return answer

    try:
        a = resolver.resolve(name, "A")
        answer.a_records = sorted(str(r) for r in a)
    except Exception:  # noqa: BLE001 - a missing A record is normal
        pass
    return answer


def lookup(
    name: str, resolvers: list[str], *, timeout: float = DEFAULT_TIMEOUT, query=_query
) -> DnsResult:
    """Ask every resolver, then reconcile.

    Disagreement is reported rather than averaged away: if one resolver says
    NXDOMAIN and another answers, the domain is treated as existing. Erring
    toward "registered" is the safe direction — a missed candidate costs
    nothing, a fabricated one costs the operator real money.
    """
    answers = [query(ip, name, timeout) for ip in resolvers]
    result = DnsResult(status=DnsStatus.ERROR, answers=answers)

    nxdomain = [a for a in answers if a.status is DnsStatus.NXDOMAIN]
    positive = [a for a in answers if a.status in (DnsStatus.OK, DnsStatus.NO_RECORDS)]

    if positive:
        best = positive[0]
        result.status = best.status
        result.ns_records = best.ns_records
        result.a_records = best.a_records
        result.resolvers_agreed = len(positive) == len(answers)
        return result

    if len(nxdomain) >= 2:
        result.status = DnsStatus.NXDOMAIN
        result.resolvers_agreed = len(nxdomain) == len(answers)
        return result

    if nxdomain:
        # Exactly one NXDOMAIN and no positive answer — not enough to act on.
        result.status = DnsStatus.ERROR
        result.resolvers_agreed = False
        return result

    for status in (DnsStatus.SERVFAIL, DnsStatus.TIMEOUT):
        matching = [a for a in answers if a.status is status]
        if matching:
            result.status = status
            result.resolvers_agreed = len(matching) == len(answers)
            return result
    return result

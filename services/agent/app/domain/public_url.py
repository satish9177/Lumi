"""Where Lumi's browser may go to inspect a page: the M7a destination policy.

The question this module answers is narrow: **may an isolated browser open this
exact address on the user's behalf, and read it?** It is answered in three
layers, and every layer fails closed.

1. **Shape.** Only `https:` with a DNS host name, the default port, no user
   information, no IP literal in any spelling a browser would accept (`127.1`,
   `0x7f000001` and `2130706433` all mean loopback to Chromium), no
   percent-encoded or backslashed authority, no local or reserved suffix
   (`localhost`, `.local`, `.internal`, `.home.arpa`, ...), and nothing but
   printable ASCII. Browser-internal, `file:`, `data:`, `javascript:` and every
   custom protocol fail here because they are not `https:`.

2. **Scope.** The host must be on an allowlist that trusted configuration --
   never a request, a model or a page -- supplied. This is the honest limit of
   Milestone 7a: without the M7b egress broker, Lumi cannot *guarantee* that an
   arbitrary host name keeps resolving to a public address between the check
   and the connection (DNS rebinding), so arbitrary hosts are not offered.

3. **Resolution.** Immediately before a request is allowed, the host is
   resolved and every address must be globally routable. Loopback, private,
   link-local (including the 169.254.169.254 metadata service), carrier-grade
   NAT, multicast, reserved and unique-local addresses are refused, as are
   IPv4-mapped and 6to4 spellings of them. The residual time-of-check gap is
   documented, not hidden.

Deterministic tests need a page Lumi controls. A *test origin* is an exact
`http://127.0.0.1:<port>` origin, again supplied only by trusted configuration.
It is not a loophole in the rules above: it is a separately named capability
that matches one origin exactly and is never inferred from a URL.
"""

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

POLICY_VERSION = "public-url-v1"
#: Milestone 7b research: the same shape and resolution rules, with the host
#: allowlist replaced by "any host that is not local, reserved or private".
#: A distinct version string so a grant records which rules authorised it.
RESEARCH_POLICY_VERSION = "public-research-v1"
#: Milestone 8 S0: the egress broker's own destination policy. Shape and address
#: only -- the broker decides whether a socket may be opened, never which host a
#: task is in scope to read, which stays with the grant and the network guard.
BROKER_POLICY_VERSION = "egress-broker-v1"
MAX_URL_LENGTH = 2_048
MAX_ALLOWED_HOSTS = 64

#: Suffixes that name something on the local machine or network, or that are
#: reserved and never public. Compared label-wise, lower case.
_LOCAL_SUFFIXES = (
    "localhost",
    "local",
    "localdomain",
    "internal",
    "intranet",
    "private",
    "corp",
    "home",
    "lan",
    "home.arpa",
    "arpa",
    "test",
    "example",
    "invalid",
    "onion",
)
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
#: The WHATWG URL parser treats a host whose last label is a number as IPv4.
_NUMERIC_LABEL = re.compile(r"^(0x[0-9a-f]*|[0-9]+)$")
_TEST_ORIGIN = re.compile(r"^http://127\.0\.0\.1:([0-9]{1,5})$")
#: RFC 3986 characters a canonical URL may contain. Anything else (space,
#: quotes, angle brackets, backslash, non-ASCII) must already be percent-encoded
#: by the trusted caller, so there is one spelling of every accepted address.
_URL_CHARACTERS = re.compile(r"^[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%|^{}]+$")


class UrlPolicyError(Exception):
    """A refused destination. `code` is stable, safe to log and to show."""

    def __init__(self, code: str) -> None:
        super().__init__(f"The destination was refused ({code}).")
        self.code = code


@dataclass(frozen=True, slots=True)
class CheckedUrl:
    """An address that passed shape and scope checks. Not yet resolved."""

    url: str
    host: str
    #: True for a configured exact loopback test origin.
    test_origin: bool


def _normalise_host_entry(entry: str) -> str:
    value = entry.strip().lower()
    wildcard = value.startswith("*.")
    host = value[2:] if wildcard else value
    _check_host_name(host)
    return f"*.{host}" if wildcard else host


def parse_allowed_hosts(raw: str | Iterable[str]) -> frozenset[str]:
    """`github.com, *.example.org` -> a validated set. Invalid entries refuse all."""
    entries = [part for part in (raw.split(",") if isinstance(raw, str) else raw) if part.strip()]
    if len(entries) > MAX_ALLOWED_HOSTS:
        raise ValueError(f"at most {MAX_ALLOWED_HOSTS} inspection hosts may be configured")
    try:
        return frozenset(_normalise_host_entry(entry) for entry in entries)
    except UrlPolicyError as error:
        raise ValueError(f"invalid inspection host entry ({error.code})") from None


def parse_test_origins(raw: str | Iterable[str]) -> frozenset[str]:
    """Exact `http://127.0.0.1:<port>` origins, and nothing else."""
    entries = [part.strip() for part in (raw.split(",") if isinstance(raw, str) else raw) if part.strip()]
    origins: set[str] = set()
    for entry in entries:
        match = _TEST_ORIGIN.fullmatch(entry)
        if match is None or not 1 <= int(match.group(1)) <= 65_535:
            raise ValueError("inspection test origins must be http://127.0.0.1:<port>")
        origins.add(entry)
    return frozenset(origins)


def _check_host_name(host: str) -> None:
    if not host or len(host) > 253:
        raise UrlPolicyError("invalid_host")
    if host.endswith("."):
        raise UrlPolicyError("invalid_host")
    labels = host.split(".")
    if len(labels) < 2:
        # A single-label name resolves through local search domains.
        raise UrlPolicyError("local_host")
    if any(_LABEL.fullmatch(label) is None for label in labels):
        raise UrlPolicyError("invalid_host")
    if _NUMERIC_LABEL.fullmatch(labels[-1]):
        raise UrlPolicyError("ip_literal")
    for suffix in _LOCAL_SUFFIXES:
        if host == suffix or host.endswith(f".{suffix}"):
            raise UrlPolicyError("local_host")


def _host_is_allowed(host: str, allowed: frozenset[str]) -> bool:
    if host in allowed:
        return True
    labels = host.split(".")
    return any(f"*.{'.'.join(labels[index:])}" in allowed for index in range(1, len(labels) - 1))


@dataclass(frozen=True, slots=True)
class PublicUrlPolicy:
    allowed_hosts: frozenset[str] = field(default_factory=frozenset)
    test_origins: frozenset[str] = field(default_factory=frozenset)
    version: str = POLICY_VERSION
    #: Milestone 7b. When true, layer 2 (the trusted host allowlist) is replaced
    #: by "any name that passes layer 1 and resolves only to globally routable
    #: addresses". Research needs this: a research task cannot know its
    #: destinations in advance. It does not weaken layers 1 and 3, and it is
    #: never set on the Milestone 7a inspection policy, which keeps its
    #: allowlist. The residual DNS-rebinding gap documented at the top of this
    #: module is *wider* here, because the allowlist no longer bounds which
    #: names may be resolved at all; see `network_policy` in the M7b review.
    allow_any_public_host: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.allowed_hosts or self.test_origins or self.allow_any_public_host)

    def check(self, raw: str) -> CheckedUrl:
        """Shape and scope. Returns the one canonical spelling, or refuses."""
        if not isinstance(raw, str) or not raw or len(raw) > MAX_URL_LENGTH:
            raise UrlPolicyError("invalid_url")
        if raw != raw.strip() or _URL_CHARACTERS.fullmatch(raw) is None:
            raise UrlPolicyError("invalid_url")
        try:
            parts = urlsplit(raw)
        except ValueError:
            raise UrlPolicyError("invalid_url") from None
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            raise UrlPolicyError("scheme_not_allowed")
        authority = parts.netloc
        if "@" in authority:
            raise UrlPolicyError("credentials_in_url")
        if "%" in authority or "[" in authority or "]" in authority:
            raise UrlPolicyError("invalid_host")
        path = parts.path or "/"
        if not path.startswith("/"):
            raise UrlPolicyError("invalid_url")
        query = f"?{parts.query}" if parts.query else ""

        if scheme == "http":
            origin = f"http://{authority.lower()}"
            if origin in self.test_origins:
                return CheckedUrl(url=f"{origin}{path}{query}", host="127.0.0.1", test_origin=True)
            raise UrlPolicyError("https_required")
        if scheme != "https":
            raise UrlPolicyError("scheme_not_allowed")

        host_part, _, port = authority.partition(":")
        if port and port != "443":
            raise UrlPolicyError("port_not_allowed")
        host = host_part.lower()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise UrlPolicyError("ip_literal")
        _check_host_name(host)
        if not self.allow_any_public_host and not _host_is_allowed(host, self.allowed_hosts):
            raise UrlPolicyError("destination_not_allowed")
        return CheckedUrl(url=f"https://{host}{path}{query}", host=host, test_origin=False)


# ---- resolution ---------------------------------------------------------------

Resolver = Callable[[str], Awaitable[list[str]]]


def address_is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped or ip.sixtofour
        if mapped is not None:
            return address_is_public(str(mapped))
        if ip.teredo is not None:
            return False
    return bool(
        ip.is_global
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_private
    )


async def system_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [str(record[4][0]) for record in records]


async def resolve_public_addresses(
    host: str, resolver: Resolver = system_resolver, timeout_seconds: float = 5.0
) -> list[str]:
    """Resolve `host`, and return the addresses only if **every** one is public.

    Fail-closed in both directions. An empty or failed lookup is `dns_failed`; a
    host that resolves to any non-public address is `non_public_address` *in
    full*, never "use the public ones and ignore the rest" -- a mixed answer is
    how a rebinding resolver offers a private address while looking innocent.

    The list matters to the caller that connects: Milestone 8's egress broker
    dials one of exactly these addresses, so the address that passed the check
    is the address on the wire. `ensure_public_resolution` is the same check for
    callers that only need the verdict.
    """
    try:
        addresses = await asyncio.wait_for(resolver(host), timeout=timeout_seconds)
    except (OSError, TimeoutError, UnicodeError):
        raise UrlPolicyError("dns_failed") from None
    if not addresses:
        raise UrlPolicyError("dns_failed")
    if not all(address_is_public(address) for address in addresses):
        raise UrlPolicyError("non_public_address")
    return list(addresses)


async def ensure_public_resolution(
    checked: CheckedUrl, resolver: Resolver = system_resolver, timeout_seconds: float = 5.0
) -> None:
    """Every address the host resolves to must be public, or nothing is sent."""
    if checked.test_origin:
        return
    await resolve_public_addresses(checked.host, resolver, timeout_seconds)

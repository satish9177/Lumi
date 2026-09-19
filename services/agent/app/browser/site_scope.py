"""The one site-scope predicate an authenticated read consults.

A profile is bound to one registrable domain (eTLD+1, from the pinned Public
Suffix List). A top-level document is *in scope* when its host's registrable
domain is that site: `github.com`, `www.github.com` and `gist.github.com` are
in scope for a `github.com` profile; `github.io` and `evil-github.com` are not.

There is exactly one implementation, here, and every caller reaches it through
the module attribute (`site_scope.in_site(...)`) rather than a bound import.
That is what lets a browser test that runs a fixture on a bare loopback IP --
which has no registrable domain by construction -- substitute a `host:port`
comparison for this one function without touching anything that calls it,
exactly as the S2 tests substitute `_site_scope`. Production never patches it.
"""

from urllib.parse import urlsplit

from app.domain.public_suffix import PublicSuffixError, registrable_domain


def in_site(url: str, site: str) -> bool:
    """True when `url`'s host belongs to the profile's registrable `site`."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    if not host:
        return False
    try:
        return registrable_domain(host) == site
    except PublicSuffixError:
        return False


__all__ = ["in_site"]

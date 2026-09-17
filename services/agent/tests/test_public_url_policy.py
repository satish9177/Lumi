"""The M7a destination policy. No database, no browser, no network.

Resolution is exercised with an injected resolver: these tests prove the
policy's decisions, not the behaviour of a DNS server.
"""

import pytest

from app.domain.public_url import (
    PublicUrlPolicy,
    UrlPolicyError,
    address_is_public,
    ensure_public_resolution,
    parse_allowed_hosts,
    parse_test_origins,
)

POLICY = PublicUrlPolicy(
    allowed_hosts=parse_allowed_hosts("github.com, *.example.org, example.com"),
    test_origins=parse_test_origins("http://127.0.0.1:8811"),
)


@pytest.mark.parametrize(
    ("raw", "canonical", "host"),
    [
        ("https://github.com/satish9177/Lumi", "https://github.com/satish9177/Lumi", "github.com"),
        ("HTTPS://GitHub.com/a?b=1#frag", "https://github.com/a?b=1", "github.com"),
        ("https://github.com", "https://github.com/", "github.com"),
        ("https://github.com:443/x", "https://github.com/x", "github.com"),
        ("https://docs.example.org/page", "https://docs.example.org/page", "docs.example.org"),
        ("https://example.com/", "https://example.com/", "example.com"),
        ("http://127.0.0.1:8811/profiles/rated", "http://127.0.0.1:8811/profiles/rated", "127.0.0.1"),
    ],
)
def test_allowed_destinations_have_one_canonical_spelling(raw: str, canonical: str, host: str) -> None:
    checked = POLICY.check(raw)
    assert (checked.url, checked.host) == (canonical, host)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("file:///C:/Windows/win.ini", "scheme_not_allowed"),
        ("javascript:alert(1)", "scheme_not_allowed"),
        ("data:text/html,<b>x</b>", "invalid_url"),
        ("chrome://settings", "scheme_not_allowed"),
        ("about:blank", "scheme_not_allowed"),
        ("ms-settings:privacy", "scheme_not_allowed"),
        ("vscode://file/C:/x", "scheme_not_allowed"),
        ("ftp://github.com/x", "scheme_not_allowed"),
        ("http://github.com/", "https_required"),
        ("https://user:pass@github.com/", "credentials_in_url"),
        ("https://github.com@evil.com/", "credentials_in_url"),
        ("https://localhost/", "local_host"),
        ("https://api.localhost/", "local_host"),
        ("https://127.0.0.1/", "ip_literal"),
        ("https://127.1/", "ip_literal"),
        ("https://2130706433/", "local_host"),
        ("https://0x7f000001/", "local_host"),
        ("https://10.0.0.1/", "ip_literal"),
        ("https://169.254.169.254/latest/meta-data/", "ip_literal"),
        ("https://[::1]/", "invalid_host"),
        ("https://metadata.google.internal/computeMetadata/v1/", "local_host"),
        ("https://printer.local/", "local_host"),
        ("https://router.home.arpa/", "local_host"),
        ("https://intranet/", "local_host"),
        ("https://github.com./", "invalid_host"),
        ("https://%67ithub.com/", "invalid_host"),
        ("https://github.com:8443/", "port_not_allowed"),
        ("https://evil.com/", "destination_not_allowed"),
        ("https://github.com.evil.com/", "destination_not_allowed"),
        ("https://example.org/", "destination_not_allowed"),
        ("https://gist.github.com/", "destination_not_allowed"),
        ("https://github.com\\@evil.com/", "invalid_url"),
        (" https://github.com/", "invalid_url"),
        ("https://github.com/a b", "invalid_url"),
        ("https://gíthub.com/", "invalid_url"),
        ("http://127.0.0.1:8812/", "https_required"),
        ("http://127.0.0.1/", "https_required"),
        ("http://localhost:8811/", "https_required"),
        ("https://github.com/" + "a" * 2_100, "invalid_url"),
    ],
)
def test_refused_destinations_fail_closed_with_a_stable_code(raw: str, code: str) -> None:
    with pytest.raises(UrlPolicyError) as refused:
        POLICY.check(raw)
    assert refused.value.code == code


def test_an_empty_policy_allows_nothing() -> None:
    empty = PublicUrlPolicy()
    assert not empty.configured
    with pytest.raises(UrlPolicyError):
        empty.check("https://github.com/")


@pytest.mark.parametrize("entry", ["localhost", "127.0.0.1", "*.local", "https://github.com", "github.com/x", "*"])
def test_misconfigured_hosts_refuse_the_whole_configuration(entry: str) -> None:
    with pytest.raises(ValueError):
        parse_allowed_hosts(f"github.com,{entry}")


@pytest.mark.parametrize("entry", ["http://localhost:1", "https://127.0.0.1:1", "http://10.0.0.1:80", "http://127.0.0.1"])
def test_test_origins_are_exact_loopback_origins_only(entry: str) -> None:
    with pytest.raises(ValueError):
        parse_test_origins(entry)


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("140.82.112.3", True),
        ("2606:4700::6810:84e5", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("0.0.0.0", False),
        ("224.0.0.1", False),
        ("255.255.255.255", False),
        ("::1", False),
        ("fe80::1", False),
        ("fd00::1", False),
        ("::ffff:127.0.0.1", False),
        ("::ffff:10.0.0.1", False),
        ("2002:7f00:1::", False),
        ("not-an-address", False),
    ],
)
def test_only_globally_routable_addresses_are_public(address: str, public: bool) -> None:
    assert address_is_public(address) is public


async def test_a_host_resolving_to_any_private_address_is_refused() -> None:
    checked = POLICY.check("https://github.com/")

    async def rebinding(_: str) -> list[str]:
        return ["140.82.112.3", "127.0.0.1"]

    with pytest.raises(UrlPolicyError) as refused:
        await ensure_public_resolution(checked, rebinding)
    assert refused.value.code == "non_public_address"


async def test_resolution_failure_is_a_refusal_not_a_pass() -> None:
    checked = POLICY.check("https://github.com/")

    async def broken(_: str) -> list[str]:
        raise OSError("no such host")

    async def empty(_: str) -> list[str]:
        return []

    for resolver in (broken, empty):
        with pytest.raises(UrlPolicyError) as refused:
            await ensure_public_resolution(checked, resolver)
        assert refused.value.code == "dns_failed"


async def test_public_resolution_passes_and_test_origins_skip_dns() -> None:
    async def public(_: str) -> list[str]:
        return ["140.82.112.3"]

    async def never(_: str) -> list[str]:
        raise AssertionError("a configured loopback test origin is not resolved")

    await ensure_public_resolution(POLICY.check("https://github.com/"), public)
    await ensure_public_resolution(POLICY.check("http://127.0.0.1:8811/x"), never)

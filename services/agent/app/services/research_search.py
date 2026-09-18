"""The public search primitive: one bounded JSON GET, made by the runtime.

Search is deliberately *not* a browser operation and *not* a screen-scrape of a
search engine. Three reasons, in order of importance:

1. **No credential reaches the browser.** A search API key is configuration the
   runtime holds; the isolated worker is never given one and has no code path
   that would use it. If search ran in the browser, the key would have to.
2. **No search-result markup reaches a browser context.** The reply is JSON,
   parsed by the bounded reader below into `r<n>` refs. A search engine's page
   -- its scripts, its trackers, its injected content -- never renders.
3. **It is deterministic to test.** The endpoint is a configured URL template,
   so a fixture serving the documented shape is a complete search provider.

The reply is still **untrusted data**. Titles and snippets are page-authored
text that a third party chose; they become refs and bounded strings, never
instructions, and a result whose address the destination policy refuses is
dropped here rather than offered to a planner as a suggestion.

Configuration (`app/config.py`): `LUMI_RESEARCH_SEARCH_ENDPOINT` is a URL
template containing `{query}`; `LUMI_RESEARCH_SEARCH_API_KEY` and
`LUMI_RESEARCH_SEARCH_HEADER` add one request header when the provider needs
one. Providers whose JSON matches any of the shapes in `_RESULT_PATHS` work
without new code (Brave, Bing, SerpAPI and the repository's own fixture at the
time of writing); anything else needs a reviewed reader, not a configuration
flag. Lumi ships with no default provider, so search is absent until someone
configures one -- navigation and link-following still work without it.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from app.domain.public_url import (
    PublicUrlPolicy,
    Resolver,
    UrlPolicyError,
    ensure_public_resolution,
    system_resolver,
)
from app.domain.research import (
    MAX_RESULTS,
    MAX_SNIPPET_CHARS,
    MAX_TITLE_CHARS,
    MAX_URL_CHARS,
)

logger = logging.getLogger("lumi.research.search")

MAX_RESPONSE_BYTES = 512_000
QUERY_PLACEHOLDER = "{query}"
JSON_CONTENT_TYPES = frozenset({"application/json", "application/ld+json", "text/json"})

#: Where a result list lives, and which keys hold its fields. Tried in order.
_RESULT_PATHS: tuple[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...] = (
    (("results",), ("title", "name"), ("url", "link"), ("snippet", "description", "summary")),
    (("web", "results"), ("title", "name"), ("url", "link"), ("description", "snippet")),
    (("webPages", "value"), ("name", "title"), ("url", "link"), ("snippet", "description")),
    (("organic_results",), ("title", "name"), ("link", "url"), ("snippet", "description")),
)


class SearchNotConfiguredError(Exception):
    def __init__(self) -> None:
        super().__init__("No public search provider is configured.")


class SearchFailedError(Exception):
    """A refused or unusable search. `code` is stable and safe to report."""

    def __init__(self, code: str) -> None:
        super().__init__(f"The public search could not be completed ({code}).")
        self.code = code


@dataclass(frozen=True, slots=True)
class SearchConfig:
    endpoint: str = ""
    api_key: SecretStr | None = None
    header: str = "X-Subscription-Token"
    timeout_seconds: float = 15.0

    @property
    def configured(self) -> bool:
        return QUERY_PLACEHOLDER in self.endpoint


@dataclass(frozen=True, slots=True)
class RawResult:
    title: str
    url: str
    host: str
    snippet: str


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.replace("\x00", " ").split())
    return text[:limit]


def _walk(body: Any, path: tuple[str, ...]) -> Any:
    current = body
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first(item: dict[str, Any], keys: tuple[str, ...], limit: int) -> str:
    for key in keys:
        value = _clean(item.get(key), limit)
        if value:
            return value
    return ""


def parse_results(body: Any, policy: PublicUrlPolicy) -> list[RawResult]:
    """Untrusted JSON -> bounded results whose addresses the policy allows."""
    for path, title_keys, url_keys, snippet_keys in _RESULT_PATHS:
        items = _walk(body, path)
        if not isinstance(items, list):
            continue
        results: list[RawResult] = []
        seen: set[str] = set()
        for entry in items:
            if not isinstance(entry, dict):
                continue
            raw_url = _first(entry, url_keys, MAX_URL_CHARS)
            if not raw_url:
                continue
            try:
                checked = policy.check(raw_url)
            except UrlPolicyError:
                # Refused here, so it is never offered to a planner at all.
                continue
            if checked.url in seen:
                continue
            seen.add(checked.url)
            results.append(
                RawResult(
                    title=_first(entry, title_keys, MAX_TITLE_CHARS),
                    url=checked.url,
                    host=checked.host,
                    snippet=_first(entry, snippet_keys, MAX_SNIPPET_CHARS),
                )
            )
            if len(results) >= MAX_RESULTS:
                break
        if results or isinstance(items, list):
            return results
    raise SearchFailedError("unreadable_search_response")


class SearchProvider(Protocol):
    """What the research controller needs of a search provider, and no more.

    Deliberately two members: a test or an offline build supplies a scripted
    one without inheriting any HTTP behaviour, and the controller cannot ask a
    provider for anything but a bounded list of results.
    """

    @property
    def configured(self) -> bool: ...

    async def search(self, query: str) -> list[RawResult]: ...


class PublicSearchProvider:
    """One configured endpoint, called with GET and nothing else."""

    def __init__(
        self,
        config: SearchConfig,
        policy: PublicUrlPolicy,
        *,
        resolver: Resolver = system_resolver,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._config = config
        self._policy = policy
        self._resolver = resolver
        self._client_factory = client_factory

    @property
    def configured(self) -> bool:
        return self._config.configured and self._policy.configured

    async def search(self, query: str) -> list[RawResult]:
        if not self.configured:
            raise SearchNotConfiguredError()
        url = self._config.endpoint.replace(QUERY_PLACEHOLDER, quote(query, safe=""))
        try:
            checked = self._policy.check(url)
        except UrlPolicyError as error:
            # A misconfigured endpoint is a configuration error, not a research
            # failure, but it fails closed either way.
            raise SearchFailedError(f"endpoint_{error.code}") from None
        try:
            await ensure_public_resolution(checked, self._resolver)
        except UrlPolicyError as error:
            raise SearchFailedError(error.code) from None

        headers = {"accept": "application/json"}
        if self._config.api_key is not None:
            headers[self._config.header] = self._config.api_key.get_secret_value()
        client = (
            self._client_factory()
            if self._client_factory is not None
            else httpx.AsyncClient(timeout=self._config.timeout_seconds)
        )
        try:
            try:
                # No redirects: a redirected search endpoint is a different
                # destination, and following it would skip the policy check.
                response = await client.get(checked.url, headers=headers, follow_redirects=False)
            except httpx.HTTPError as error:
                logger.info("public search failed", extra={"reason": type(error).__name__})
                raise SearchFailedError("search_unavailable") from None
            if response.status_code != 200:
                raise SearchFailedError(f"search_http_{min(response.status_code, 599)}")
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in JSON_CONTENT_TYPES:
                raise SearchFailedError("search_not_json")
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise SearchFailedError("search_response_too_large")
            try:
                body = response.json()
            except ValueError:
                raise SearchFailedError("search_not_json") from None
        finally:
            await client.aclose()
        return parse_results(body, self._policy)


__all__ = [
    "MAX_RESPONSE_BYTES",
    "QUERY_PLACEHOLDER",
    "PublicSearchProvider",
    "RawResult",
    "SearchConfig",
    "SearchFailedError",
    "SearchProvider",
    "SearchNotConfiguredError",
    "parse_results",
]

"""Apple Music API client.

Adapted from Music Assistant (Apache-2.0), https://github.com/music-assistant/server
(music_assistant/providers/apple_music/api_client.py)
Modifications:
- takes an aiohttp session + tokens directly instead of an AppleMusicProvider/MA instance;
- uses local error classes (errors.py) instead of music_assistant_models.errors;
- request-counter diagnostics, ratings and write (PUT/POST/DELETE) helpers removed;
- 429 now honours the Retry-After header (MA ignored it); headers are never logged;
- the language comes from the storefront's defaultLanguageTag unless a locale is given.
Endpoints, auth headers, throttle (4 req/s, 8 attempts, 1s initial backoff) and the
paged-listing truncation handling are unchanged.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, Concatenate, cast

import aiohttp
from aiohttp import ClientConnectionError, ClientPayloadError, ClientTimeout

from .errors import AuthError, MediaNotFound, RateLimited, ResourceTemporarilyUnavailable
from .helpers.throttle_retry import ThrottlerManager, parse_retry_after, throttle_with_retries

APPLE_API_BASE = "https://api.music.apple.com/v1"

_LIBRARY_PAGE_SIZE = 100

_PAGE_TRUNCATION_RETRIES = 3


def _retry_transient_transport_errors[ClientT, **P, R](
    func: Callable[Concatenate[ClientT, P], Awaitable[R]],
) -> Callable[Concatenate[ClientT, P], Awaitable[R]]:
    """Convert dropped connections / truncated bodies into the retryable error type."""

    @functools.wraps(func)
    async def wrapper(self: ClientT, *args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await func(self, *args, **kwargs)
        except (ClientConnectionError, ClientPayloadError, TimeoutError) as err:
            raise ResourceTemporarilyUnavailable(
                f"Transient transport error calling Apple Music: {type(err).__name__}: {err}"
            ) from err

    return wrapper


def _raise_on_auth_error(status: int, endpoint: str) -> None:
    """Raise AuthError when Apple rejected our credentials."""
    if status not in (401, 403):
        return
    if endpoint.startswith("catalog/"):
        raise AuthError(
            f"Apple Music denied access to {endpoint}: the developer token is invalid or expired"
        )
    raise AuthError(
        f"Apple Music denied access to {endpoint}: the account needs to be signed in again, "
        "or the developer token is invalid"
    )


def match_language_tag(locale: str, supported_tags: list[str]) -> str | None:
    """Return the storefront language tag closest to the locale, if any."""
    # Apple ignores a valid tag the storefront lacks, but rejects a malformed one with a 400.
    language = locale.split("-", maxsplit=1)[0].lower()
    for tag in supported_tags:
        if tag.lower() == locale.lower():
            return tag
    for tag in supported_tags:
        if tag.split("-")[0].lower() == language:
            return tag
    return None


class AppleMusicAPIClient:
    """Handles all HTTP communication with the Apple Music API."""

    # period=0.25 -> 4 req/s. Apple throttles per developer account and 429s clear within
    # a second; 8 attempts keep the 1/2/4/.../64s ladder spanning ~2 minutes.
    # Class-level on purpose: shared by every client using the same process.
    throttler = ThrottlerManager(rate_limit=1, period=0.25, retry_attempts=8, initial_backoff=1)

    def __init__(
        self,
        http: aiohttp.ClientSession,
        developer_token: str | None,
        user_token: str | None,
        logger: logging.Logger,
    ) -> None:
        self.http = http
        self.developer_token = developer_token
        self.user_token = user_token
        self.logger = logger
        self.language: str | None = None

    @throttle_with_retries
    @_retry_transient_transport_errors
    async def get_data(self, endpoint: str, /, **kwargs: Any) -> dict[str, Any]:
        """GET data from the Apple Music API."""
        url = f"{APPLE_API_BASE}/{endpoint}"
        if self.language and "l" not in kwargs:
            kwargs = {**kwargs, "l": self.language}
        async with self.http.get(
            url,
            headers=self._headers_for(endpoint),
            params=kwargs,
            timeout=ClientTimeout(total=120),
        ) as response:
            _raise_on_auth_error(response.status, endpoint)
            if response.status == 404 and "limit" in kwargs and "offset" in kwargs:
                return {}
            if response.status == 404:
                raise MediaNotFound(f"{endpoint} not found")
            if response.status == 504:
                self.logger.debug("Apple Music API timeout on %s", endpoint)
                raise ResourceTemporarilyUnavailable("Apple Music API Timeout")
            if response.status == 429:
                self.logger.debug("Apple Music rate limiter on %s", endpoint)
                raise RateLimited(
                    "Apple Music Rate Limiter",
                    backoff_time=parse_retry_after(response.headers.get("Retry-After")),
                )
            if response.status == 500:
                # Apple 500s are typically transient
                raise ResourceTemporarilyUnavailable(
                    "Unexpected server error when calling Apple Music"
                )
            response.raise_for_status()
            return cast("dict[str, Any]", await response.json(content_type=None))

    async def iter_all_items(
        self, endpoint: str, key: str = "data", page_size: int = _LIBRARY_PAGE_SIZE, **kwargs: Any
    ) -> AsyncGenerator[dict[str, Any]]:
        """Yield items from a paged list one page at a time."""
        offset = 0
        while True:
            kwargs["limit"] = page_size
            kwargs["offset"] = offset
            result = await self._get_page(endpoint, key, offset, kwargs)
            if key not in result:
                # offset 0 only: empty collection or 404. _get_page raises on mid-list truncation.
                break
            for item in result[key]:
                yield item
            if not result.get("next"):
                break
            offset += page_size

    async def get_all_items(
        self, endpoint: str, key: str = "data", **kwargs: Any
    ) -> list[dict[str, Any]]:
        """Get all items from a paged list."""
        return [item async for item in self.iter_all_items(endpoint, key, **kwargs)]

    async def get_user_storefront(self, locale: str | None = None) -> str:
        """Return the user's storefront id and pick the request language."""
        params = {"l": locale.split("-")[0]} if locale else {}
        result = await self.get_data("me/storefront", **params)
        storefront = result["data"][0]
        attributes = storefront.get("attributes", {})
        if locale:
            self.language = match_language_tag(
                locale.replace("_", "-"), attributes.get("supportedLanguageTags", [])
            )
        else:
            self.language = attributes.get("defaultLanguageTag")
        return cast("str", storefront["id"])

    async def _get_page(
        self, endpoint: str, key: str, offset: int, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Fetch one page of a paged listing, recovering from transient truncation.

        Apple returns 404/``{}`` for a paged request that yields nothing. At offset 0 that
        means an empty collection; mid-pagination it is a transient truncation, so re-fetch.
        """
        result = await self.get_data(endpoint, **kwargs)
        if key in result or offset == 0:
            return result
        for _ in range(_PAGE_TRUNCATION_RETRIES):
            result = await self.get_data(endpoint, **kwargs)
            if key in result:
                return result
        raise ResourceTemporarilyUnavailable(
            f"Incomplete paged listing for {endpoint} at offset {offset}"
        )

    def _headers_for(self, endpoint: str) -> dict[str, str]:
        """Return the auth headers for a request to the given endpoint."""
        headers = {"Authorization": f"Bearer {self.developer_token}"}
        # Without the user token, all installs share one Akamai cache variant per catalog URL
        if not endpoint.startswith("catalog/") and self.user_token:
            headers["Music-User-Token"] = self.user_token
        return headers

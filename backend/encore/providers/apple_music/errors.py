"""Error types of the Apple Music provider.

Adapted from Music Assistant (Apache-2.0), https://github.com/music-assistant/server
Modifications: replaces music_assistant_models.errors (LoginFailed, MediaNotFoundError,
RateLimited, ResourceTemporarilyUnavailable, RetriesExhausted) with small local classes
that subclass Encore's ProviderError, so callers only need to catch ProviderError.
"""

from __future__ import annotations

from encore.models import ProviderError


class AuthError(ProviderError):
    """Apple rejected our developer token or Music-User-Token (401/403)."""


class MediaNotFound(ProviderError):
    """The requested item does not exist or cannot be streamed."""


class ResourceTemporarilyUnavailable(ProviderError):
    """A transient failure that is worth retrying."""

    def __init__(self, message: str, backoff_time: float = 0) -> None:
        super().__init__(message)
        self.backoff_time = backoff_time


class RateLimited(ResourceTemporarilyUnavailable):
    """Apple answered 429."""


class RetriesExhausted(ProviderError):
    """A request kept failing transiently until we gave up."""

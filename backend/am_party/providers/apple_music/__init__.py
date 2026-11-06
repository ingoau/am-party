"""Apple Music provider for am-party.

Adapted from Music Assistant (Apache-2.0), https://github.com/music-assistant/server
(music_assistant/providers/apple_music). See NOTICE.md and the per-file headers for the
modifications made.
"""

from .auth import (
    developer_token_expiry,
    is_plausible_user_token,
    render_musickit_auth_page,
    validate_developer_token,
)
from .client import AppleMusicClient
from .errors import AuthError, MediaNotFound, RateLimited, RetriesExhausted

__all__ = [
    "AppleMusicClient",
    "AuthError",
    "MediaNotFound",
    "RateLimited",
    "RetriesExhausted",
    "developer_token_expiry",
    "is_plausible_user_token",
    "render_musickit_auth_page",
    "validate_developer_token",
]

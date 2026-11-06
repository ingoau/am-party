"""Various Apple Music utils/helpers.

Adapted from Music Assistant (Apache-2.0), https://github.com/music-assistant/server
Modifications: only the id helpers and the throttler are kept; browse/request-counter
helpers were dropped.
"""

from .utils import is_catalog_id, is_library_id

__all__ = ["is_catalog_id", "is_library_id"]

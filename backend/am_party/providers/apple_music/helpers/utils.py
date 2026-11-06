"""Utility helpers for the Apple Music provider.

Adapted from Music Assistant (Apache-2.0), https://github.com/music-assistant/server
Modifications: removed MediaType translation (MA-specific); kept the id-shape helpers.
"""

from __future__ import annotations

import re
from typing import Any


def is_library_id(library_id: Any) -> bool:
    """Return True if the ID matches the Apple Music library ID format (i./l./p./a.)."""
    if not isinstance(library_id, str):
        return False
    return bool(re.fullmatch(r"[ailp]\.[a-zA-Z0-9]+", library_id))


def is_catalog_id(catalog_id: str) -> bool:
    """Return True if the ID is a catalog ID (numeric or starts with 'pl.')."""
    return catalog_id.isnumeric() or catalog_id.startswith("pl.")

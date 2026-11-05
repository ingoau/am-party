"""Parsers for Apple Music API response objects (and the HLS playlists it serves).

Adapted from Music Assistant (Apache-2.0), https://github.com/music-assistant/server
(music_assistant/providers/apple_music/parsers.py, plus parse_m3u/_parse_hls_key from
music_assistant/helpers/playlists.py)
Modifications:
- produce Encore's flat Track / PlaylistInfo dataclasses instead of MA media items
  (no artist/album objects, versions, genres, favourites, provider mappings);
- library-song -> catalog resolution returns the catalog id or None (unplayable);
- artwork is always rendered at a fixed size (600x600 by default) instead of
  min(native, 1000), and {c}/{f} template placeholders are filled too;
- M3U parsing reduced to what the stream resolver needs (segment path + EXT-X-KEY URI);
- added parse_playlist_ref() for music.apple.com URLs.
The availability rules (_is_available) are unchanged.
"""

from __future__ import annotations

import base64
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

from encore.models import PlaylistInfo, Track

from .helpers.utils import is_library_id

ARTWORK_SIZE = 600
UNKNOWN_PLAYLIST_NAME = "Unknown Apple Music Playlist"


def _norm(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize("NFC", value).strip()
    return value or None


def format_artwork_url(attributes: dict[str, Any], size: int = ARTWORK_SIZE) -> str | None:
    """Return the artwork URL of a raw api item's attributes, filled at ``size``x``size``."""
    if not (artwork := attributes.get("artwork")):
        return None
    if not (url := artwork.get("url")):
        return None
    return (
        str(url)
        .replace("{w}", str(size))
        .replace("{h}", str(size))
        .replace("{c}", "bb")
        .replace("{f}", "jpg")
    )


def is_available(attributes: dict[str, Any]) -> bool:
    """
    Return whether Apple will actually serve a stream for this item.

    ``playParams`` is absent entirely for items Apple has withdrawn. It is present but
    carries a ``purchasedId`` with no ``catalogId`` for purchase-only items, which the
    stream endpoint also refuses.
    """
    play_params = attributes.get("playParams") or {}
    if play_params.get("id") is None:
        return False
    return not (play_params.get("purchasedId") is not None and play_params.get("catalogId") is None)


def resolve_catalog_song(track_obj: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """
    Return ``(catalog_id, attributes)`` for a song object, or None when it has no catalog id.

    Catalog ``songs`` are returned as-is. ``library-songs`` are resolved through their
    ``catalog`` relationship (request with ``include=catalog``), falling back to
    ``playParams.catalogId``; library-only uploads have neither and yield None.
    """
    item_type = track_obj.get("type")
    attributes = track_obj.get("attributes") or {}
    if item_type == "songs":
        if not track_obj.get("id"):
            return None
        return str(track_obj["id"]), attributes
    if item_type != "library-songs":
        return None  # music-videos, uploaded-videos, ...
    catalog_data = (track_obj.get("relationships") or {}).get("catalog", {}).get("data") or []
    if catalog_data and catalog_data[0].get("id"):
        cat = catalog_data[0]
        cat_attrs = cat.get("attributes") or {}
        if not cat_attrs:
            return str(cat["id"]), attributes
        # the catalog copy is authoritative (incl. availability); library values fill gaps
        return str(cat["id"]), {**attributes, **cat_attrs, "playParams": cat_attrs.get("playParams")}
    catalog_id = (attributes.get("playParams") or {}).get("catalogId")
    if catalog_id and str(catalog_id).isnumeric():
        # the library playParams describe the library item; fake catalog playParams
        merged = {**attributes, "playParams": {"id": str(catalog_id), "kind": "song"}}
        return str(catalog_id), merged
    return None


def parse_track(track_obj: dict[str, Any]) -> Track | None:
    """
    Parse a song object (catalog or library) into a playable catalog Track.

    Returns None when the song cannot be streamed (no catalog id, withdrawn, purchase-only).
    """
    resolved = resolve_catalog_song(track_obj)
    if resolved is None:
        return None
    track_id, attributes = resolved
    if not is_available(attributes):
        return None
    return Track(
        id=track_id,
        title=_norm(attributes.get("name")) or "",
        artist=_norm(attributes.get("artistName")) or "",
        album=_norm(attributes.get("albumName")),
        duration=float(attributes.get("durationInMillis") or 0) / 1000,
        artwork_url=format_artwork_url(attributes),
        explicit=attributes.get("contentRating") == "explicit",
        isrc=attributes.get("isrc") or None,
    )


def parse_playlist(playlist_obj: dict[str, Any]) -> PlaylistInfo:
    """Parse a catalog or library playlist object; library ids (p.*) are kept as the id."""
    attributes = playlist_obj.get("attributes") or {}
    raw_playlist_id = str(playlist_obj["id"])
    play_params = attributes.get("playParams") or {}
    playlist_id = (
        raw_playlist_id if is_library_id(raw_playlist_id) else play_params.get("globalId")
    ) or raw_playlist_id
    return PlaylistInfo(
        id=playlist_id,
        name=_norm(attributes.get("name")) or UNKNOWN_PLAYLIST_NAME,
        artwork_url=format_artwork_url(attributes),
    )


# --------------------------------------------------------------------------- #
#  Playlist references                                                        #
# --------------------------------------------------------------------------- #

_PLAYLIST_ID_RE = re.compile(r"^(?:pl\.[A-Za-z0-9_-]+|p\.[A-Za-z0-9]+)$")


def parse_playlist_ref(url_or_id: str) -> str | None:
    """
    Extract a playlist id from a music.apple.com URL or a raw id.

    Accepts ``https://music.apple.com/us/playlist/<slug>/pl.xxx``,
    ``https://music.apple.com/library/playlist/p.xxx`` (also ``…/<storefront>/library/…``),
    and raw ``pl.xxx`` / ``p.xxx`` ids. Returns None for anything else.
    """
    if not isinstance(url_or_id, str):
        return None
    ref = url_or_id.strip()
    if not ref:
        return None
    if _PLAYLIST_ID_RE.match(ref):
        return ref
    if "://" not in ref and ref.startswith(("music.apple.com", "beta.music.apple.com")):
        ref = "https://" + ref
    parsed = urlparse(ref)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not (
        host == "music.apple.com" or host.endswith(".music.apple.com")
    ):
        return None
    segments = [unquote(s) for s in parsed.path.split("/") if s]
    if "playlist" not in segments:
        return None
    for segment in reversed(segments[segments.index("playlist") + 1 :]):
        if _PLAYLIST_ID_RE.match(segment):
            return segment
    return None


# --------------------------------------------------------------------------- #
#  HLS                                                                        #
# --------------------------------------------------------------------------- #


@dataclass
class HlsItem:
    """One media entry of an M3U8 playlist with the key in effect for it."""

    path: str
    key: str | None = None


def parse_hls_key(line: str, current_key: str | None) -> str | None:
    """Parse #EXT-X-KEY and return the key URI, or None for METHOD=NONE."""
    if "METHOD=NONE" in line:
        return None
    match = re.search(r'(?:^|[:,])URI="([^"]*)"', line)
    if match:
        return match.group(1)
    if ",URI=" in line:
        return line.split(",URI=")[1].split(",")[0].strip('"')
    return current_key


def parse_m3u(m3u_data: str) -> list[HlsItem]:
    """Parse M3U8 data into media entries, tracking the active #EXT-X-KEY URI."""
    items: list[HlsItem] = []
    key: str | None = None
    for raw_line in m3u_data.splitlines():
        line = raw_line.strip()
        if line.startswith("#EXT-X-KEY:"):
            key = parse_hls_key(line, key)
        elif line.startswith("#") or not line:
            continue
        else:
            items.append(HlsItem(path=line.replace("%20", " ").replace("\\", "/"), key=key))
    return items


def pick_ctrp256_url(assets: list[dict[str, Any]]) -> str | None:
    """Return the HLS playlist URL of the ``28:ctrp256`` (256 kbps AAC, CTR) flavor."""
    urls = [a["URL"] for a in assets if a.get("flavor") == "28:ctrp256" and a.get("URL")]
    return urls[0] if urls else None


def key_id_from_uri(uri: str) -> bytes:
    """Return the raw Widevine key id from a ``data:;base64,<kid>`` EXT-X-KEY URI."""
    if "," not in uri:
        raise ValueError("Unexpected EXT-X-KEY URI format")
    return base64.b64decode(uri.split(",", 1)[1])

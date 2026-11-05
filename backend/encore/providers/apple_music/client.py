"""Encore's Apple Music client: catalog search, playlists and encrypted streams.

Adapted from Music Assistant (Apache-2.0), https://github.com/music-assistant/server
(music_assistant/providers/apple_music/provider.py, media.py and library.py)
Modifications: a single MA-free facade over the vendored api_client / parsers /
streaming modules. Search is songs-only and drops unavailable items; library
playlists are listed from `me/library/playlists` without MA's per-playlist catalog
lookups and ratings; playlist tracks resolve library songs to catalog ids and skip
library-only/unplayable items; no MA cache (only the 1h decryption-key cache).
"""

from __future__ import annotations

import logging

import aiohttp

from encore.models import NotConfigured, PlaylistInfo, ProviderError, StreamInfo, Track

from .api_client import AppleMusicAPIClient
from .errors import MediaNotFound
from .helpers.utils import is_catalog_id, is_library_id
from .parsers import parse_playlist, parse_playlist_ref, parse_track
from .streaming import AppleMusicStreamingManager

_SEARCH_PAGE_SIZE = 25  # Apple's max for catalog search
_PLAYLIST_TRACKS_PAGE_SIZE = 100


class AppleMusicClient:
    """Plain async Apple Music client for Encore."""

    def __init__(
        self,
        http: aiohttp.ClientSession,
        developer_token: str | None,
        user_token: str | None,
        widevine_client_id: bytes | None,
        widevine_private_key: bytes | None,
        logger: logging.Logger | None = None,
        *,
        locale: str | None = None,
    ) -> None:
        self.http = http
        self.logger = logger or logging.getLogger("encore.providers.apple_music")
        self.locale = locale
        self.storefront: str | None = None
        self._developer_token = (developer_token or "").strip() or None
        self._user_token = (user_token or "").strip() or None
        self.api = AppleMusicAPIClient(http, self._developer_token, self._user_token, self.logger)
        self.streaming = AppleMusicStreamingManager(
            http,
            self.logger,
            self._developer_token,
            self._user_token,
            widevine_client_id or None,
            widevine_private_key or None,
        )

    # ------------------------------------------------------------------ setup

    @property
    def configured(self) -> bool:
        """True when both the developer token and the Music-User-Token are present."""
        return bool(self._developer_token and self._user_token)

    @property
    def can_stream(self) -> bool:
        """True when encrypted catalog tracks can be streamed (tokens + Widevine CDM)."""
        return self.configured and self.streaming.widevine_configured

    async def setup(self) -> None:
        """Fetch the user's storefront and language; raises NotConfigured / ProviderError."""
        if not self.configured:
            raise NotConfigured("Apple Music developer token and user token are required")
        self.storefront = await self.api.get_user_storefront(self.locale)
        self.logger.debug(
            "Apple Music storefront %s, language %s", self.storefront, self.api.language
        )

    async def set_credentials(
        self, developer_token: str | None = None, user_token: str | None = None
    ) -> None:
        """Replace one or both tokens at runtime (None keeps the current one) and re-run setup."""
        if developer_token is not None:
            self._developer_token = developer_token.strip() or None
        if user_token is not None:
            self._user_token = user_token.strip() or None
        self.api.developer_token = self._developer_token
        self.api.user_token = self._user_token
        self.streaming.developer_token = self._developer_token
        self.streaming.user_token = self._user_token
        self.storefront = None
        self.api.language = None
        await self.setup()

    async def _ensure_ready(self) -> str:
        if not self.configured:
            raise NotConfigured("Apple Music is not configured")
        if self.storefront is None:
            await self.setup()
        assert self.storefront is not None
        return self.storefront

    # ----------------------------------------------------------------- tracks

    async def search_tracks(self, term: str, limit: int = 20) -> list[Track]:
        """Search catalog songs; unavailable songs are dropped."""
        storefront = await self._ensure_ready()
        term = term.replace("'", "").strip()
        if not term or limit <= 0:
            return []
        endpoint = f"catalog/{storefront}/search"
        tracks: list[Track] = []
        seen: set[str] = set()
        offset = 0
        while len(tracks) < limit:
            page_size = min(_SEARCH_PAGE_SIZE, limit - len(tracks))
            params: dict[str, object] = {"term": term, "types": "songs", "limit": page_size}
            if offset:
                params["offset"] = offset
            response = await self.api.get_data(endpoint, **params)
            songs = (response.get("results") or {}).get("songs") or {}
            data = songs.get("data") or []
            for item in data:
                track = parse_track(item)
                if track and track.id not in seen:
                    seen.add(track.id)
                    tracks.append(track)
            if not songs.get("next") or not data:
                break
            offset += len(data)
        return tracks[:limit]

    async def get_track(self, track_id: str) -> Track:
        """Return a catalog track. Library song ids (i.*) are resolved to their catalog song."""
        storefront = await self._ensure_ready()
        if is_library_id(track_id):
            response = await self.api.get_data(
                f"me/library/songs/{track_id}", include="catalog"
            )
        else:
            response = await self.api.get_data(f"catalog/{storefront}/songs/{track_id}")
        data = response.get("data") or []
        track = parse_track(data[0]) if data else None
        if track is None:
            raise MediaNotFound(f"Apple Music track {track_id} is not available")
        return track

    # -------------------------------------------------------------- playlists

    async def get_library_playlists(self) -> list[PlaylistInfo]:
        """List the user's library playlists (all pages)."""
        await self._ensure_ready()
        items = await self.api.get_all_items("me/library/playlists")
        return [parse_playlist(item) for item in items if item and item.get("id")]

    async def get_playlist(self, playlist_id: str) -> PlaylistInfo:
        """Fetch a catalog (pl.*) or library (p.*) playlist. URLs are accepted too."""
        storefront = await self._ensure_ready()
        playlist_id = self._require_playlist_id(playlist_id)
        if is_catalog_id(playlist_id):
            endpoint = f"catalog/{storefront}/playlists/{playlist_id}"
        else:
            endpoint = f"me/library/playlists/{playlist_id}"
        response = await self.api.get_data(endpoint)
        data = response.get("data") or []
        if not data:
            raise MediaNotFound(f"Playlist {playlist_id} not found")
        playlist = parse_playlist(data[0])
        # keep the id the caller asked for (a library playlist may report a catalog globalId)
        playlist.id = playlist_id
        return playlist

    async def get_playlist_tracks(self, playlist_id: str, max_tracks: int = 1000) -> list[Track]:
        """Return playable catalog tracks of a playlist (library songs resolved to catalog)."""
        storefront = await self._ensure_ready()
        playlist_id = self._require_playlist_id(playlist_id)
        if is_catalog_id(playlist_id):
            endpoint = f"catalog/{storefront}/playlists/{playlist_id}/tracks"
            params: dict[str, str] = {}
        else:
            endpoint = f"me/library/playlists/{playlist_id}/tracks"
            params = {"include": "catalog"}
        tracks: list[Track] = []
        skipped = 0
        async for item in self.api.iter_all_items(
            endpoint, page_size=_PLAYLIST_TRACKS_PAGE_SIZE, **params
        ):
            if len(tracks) >= max_tracks:
                break
            track = parse_track(item) if item else None
            if track is None:
                skipped += 1
                continue
            tracks.append(track)
        if skipped:
            self.logger.debug(
                "Skipped %d unplayable/library-only items in playlist %s", skipped, playlist_id
            )
        return tracks

    @staticmethod
    def parse_playlist_ref(url_or_id: str) -> str | None:
        """Extract a playlist id from a music.apple.com URL or a raw pl./p. id."""
        return parse_playlist_ref(url_or_id)

    def _require_playlist_id(self, url_or_id: str) -> str:
        playlist_id = parse_playlist_ref(url_or_id)
        if playlist_id is None:
            raise ProviderError(f"Not an Apple Music playlist reference: {url_or_id!r}")
        return playlist_id

    # --------------------------------------------------------------- streaming

    async def get_stream(self, track_id: str) -> StreamInfo:
        """Resolve a catalog track id to an encrypted fMP4 URL + hex content key."""
        await self._ensure_ready()
        return await self.streaming.get_stream(track_id)

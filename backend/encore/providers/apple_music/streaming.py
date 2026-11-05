"""Stream URL resolution and Widevine key retrieval for Apple Music.

Adapted from Music Assistant (Apache-2.0), https://github.com/music-assistant/server
(music_assistant/providers/apple_music/streaming.py)
Modifications:
- Widevine client id / private key are passed in as bytes instead of read from
  /usr/local/bin/widevine_cdm;
- the MA cache is replaced by an in-memory dict (key -> (hex key, expiry), 1 hour);
- pywidevine Device parsing, challenge generation and license parsing run in a worker
  thread (asyncio.to_thread); the Device is built once and reused;
- concurrent requests for the same track share one license request;
- MA's fetch_playlist() is replaced by a direct GET + parsers.parse_m3u();
- the license POST keeps TLS verification on (MA used ssl=False);
- returns Encore's StreamInfo instead of MA StreamDetails; errors are ProviderErrors.
The webPlayback request/headers, `28:ctrp256` flavor selection, PSSH construction and
license request body are unchanged.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from aiohttp import ClientError, ClientTimeout

from encore.models import NotConfigured, ProviderError, StreamInfo

from .errors import AuthError, MediaNotFound
from .helpers.utils import is_library_id
from .parsers import key_id_from_uri, parse_m3u, pick_ctrp256_url

if TYPE_CHECKING:
    from pywidevine import PSSH, Device

WEB_PLAYBACK_URL = "https://play.music.apple.com/WebObjects/MZPlay.woa/wa/webPlayback"
KEY_CACHE_TTL = 3600.0


class AppleMusicStreamingManager:
    """Handles stream URL resolution and Widevine decryption."""

    def __init__(
        self,
        http: aiohttp.ClientSession,
        logger: logging.Logger,
        developer_token: str | None,
        user_token: str | None,
        widevine_client_id: bytes | None,
        widevine_private_key: bytes | None,
    ) -> None:
        self.http = http
        self.logger = logger
        self.developer_token = developer_token
        self.user_token = user_token
        self._client_id = widevine_client_id
        self._private_key = widevine_private_key
        self._device: Device | None = None
        self._key_cache: dict[str, tuple[str, float]] = {}
        self._key_locks: dict[str, asyncio.Lock] = {}

    @property
    def widevine_configured(self) -> bool:
        return bool(self._client_id and self._private_key)

    async def get_stream(self, item_id: str) -> StreamInfo:
        """Return StreamInfo for a catalog track (encrypted) or a library upload (plain)."""
        stream_metadata = await self._fetch_song_stream_metadata(item_id)
        if is_library_id(item_id):
            try:
                stream_url = stream_metadata["assets"][0]["URL"]
            except (KeyError, IndexError, TypeError) as exc:
                raise MediaNotFound(
                    f"Failed to extract stream URL for library track {item_id}: {exc}"
                ) from exc
            return StreamInfo(url=stream_url)
        if not self.widevine_configured:
            raise NotConfigured(
                "Widevine CDM files are not available; cannot stream encrypted catalog tracks."
            )
        license_url = stream_metadata.get("hls-key-server-url")
        if not license_url:
            raise MediaNotFound(f"No key server URL for song {item_id}")
        stream_url, uri = await self._parse_stream_url_and_uri(stream_metadata.get("assets") or [])
        if not stream_url or not uri:
            raise MediaNotFound("No stream URL found for song.")
        key_id = key_id_from_uri(uri)
        key = await self._get_decryption_key(license_url, key_id, uri, item_id)
        return StreamInfo(url=stream_url, decryption_key=key, codec="aac")

    async def _fetch_song_stream_metadata(self, song_id: str) -> dict[str, Any]:
        """Get the stream metadata (assets, key server) for a song."""
        data: dict[str, Any] = {}
        if is_library_id(song_id):
            data["universalLibraryId"] = song_id
            data["isLibrary"] = True
        else:
            data["salableAdamId"] = song_id
        for retry in (True, False):
            try:
                async with self.http.post(
                    WEB_PLAYBACK_URL,
                    headers=self._decryption_headers,
                    json=data,
                    timeout=ClientTimeout(total=30),
                ) as response:
                    if response.status in (401, 403):
                        raise AuthError(
                            "Apple Music refused webPlayback: tokens invalid or no subscription"
                        )
                    response.raise_for_status()
                    content = await response.json(content_type=None)
                    if failure := content.get("failureType"):
                        message = content.get("customerMessage") or content.get("failureMessage")
                        if str(failure) == "2002":
                            # "Your session has ended. Sign in again." -> Music-User-Token dead
                            raise AuthError(f"Apple Music session ended, sign in again: {message}")
                        raise MediaNotFound(
                            f"Failed to get song stream metadata ({content.get('failureType')}):"
                            f" {message}"
                        )
                    return cast("dict[str, Any]", content["songList"][0])
            except (MediaNotFound, ClientError, TimeoutError, KeyError, IndexError) as exc:
                if retry:
                    self.logger.warning("Failed to get song stream metadata: %s", exc)
                    continue
                if isinstance(exc, ProviderError):
                    raise
                raise ProviderError(f"Failed to get song stream metadata: {exc}") from exc
        raise MediaNotFound(f"Failed to get song stream metadata for {song_id}")

    async def _parse_stream_url_and_uri(
        self, stream_assets: list[dict[str, Any]]
    ) -> tuple[str | None, str | None]:
        """Pick the ctrp256 flavor, fetch its M3U8 and return (media URL, key URI)."""
        playlist_url = pick_ctrp256_url(stream_assets)
        if not playlist_url:
            raise MediaNotFound("No ctrp256 URL found for song.")
        try:
            async with self.http.get(
                playlist_url, allow_redirects=True, timeout=ClientTimeout(total=30)
            ) as resp:
                resp.raise_for_status()
                m3u_data = await resp.text()
        except (ClientError, TimeoutError) as err:
            raise ProviderError(f"Error while fetching HLS playlist: {err}") from err
        items = parse_m3u(m3u_data)
        if not items:
            raise MediaNotFound("Empty HLS playlist for song.")
        base_path = playlist_url.rsplit("/", 1)[0]
        path = items[0].path
        track_url = path if "://" in path else base_path + "/" + path
        return track_url, items[0].key

    @property
    def _decryption_headers(self) -> dict[str, str]:
        """Headers required for webPlayback and license requests."""
        return {
            "authorization": f"Bearer {self.developer_token}",
            "media-user-token": self.user_token or "",
            "connection": "keep-alive",
            "accept": "application/json",
            "origin": "https://music.apple.com",
            "referer": "https://music.apple.com/",
            "accept-encoding": "gzip, deflate, br",
            "content-type": "application/json;charset=utf-8",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                " (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"
            ),
        }

    async def _get_decryption_key(
        self, license_url: str, key_id: bytes, uri: str, item_id: str
    ) -> str:
        """Get (or retrieve from the 1h in-memory cache) the content key for a song."""
        lock = self._key_locks.setdefault(item_id, asyncio.Lock())
        try:
            async with lock:
                cached = self._key_cache.get(item_id)
                if cached and cached[1] > time.monotonic():
                    self.logger.debug("Decryption key for %s found in cache.", item_id)
                    return cached[0]
                key = await self._fetch_decryption_key(license_url, key_id, uri, item_id)
                self._key_cache[item_id] = (key, time.monotonic() + KEY_CACHE_TTL)
                self._prune_cache()
                return key
        finally:
            if not lock.locked() and self._key_locks.get(item_id) is lock:
                del self._key_locks[item_id]

    def _prune_cache(self) -> None:
        now = time.monotonic()
        for k in [k for k, (_, exp) in self._key_cache.items() if exp <= now]:
            del self._key_cache[k]

    def _get_device(self) -> Device:
        from pywidevine import Device, DeviceTypes

        if self._device is None:
            self._device = Device(
                client_id=self._client_id,
                private_key=self._private_key,
                type_=DeviceTypes.ANDROID,
                security_level=3,
                flags={},
            )
        return self._device

    async def _fetch_decryption_key(
        self, license_url: str, key_id: bytes, uri: str, item_id: str
    ) -> str:
        from pywidevine import Cdm

        def _challenge() -> tuple[Any, bytes, bytes]:
            cdm = Cdm.from_device(self._get_device())
            session_id = cdm.open()
            try:
                challenge = cdm.get_license_challenge(session_id, _build_pssh(key_id))
            except BaseException:
                cdm.close(session_id)
                raise
            return cdm, session_id, challenge

        def _parse(cdm: Any, session_id: bytes, track_license: str) -> str | None:
            try:
                cdm.parse_license(session_id, track_license)
                key = next((k for k in cdm.get_keys(session_id) if k.type == "CONTENT"), None)
                return key.key.hex() if key else None
            finally:
                cdm.close(session_id)

        try:
            cdm, session_id, challenge = await asyncio.to_thread(_challenge)
        except Exception as err:
            raise ProviderError(f"Widevine CDM error: {err}") from err
        try:
            track_license = await self._get_license(challenge, license_url, uri, item_id)
        except BaseException:
            cdm.close(session_id)
            raise
        try:
            decryption_key = await asyncio.to_thread(_parse, cdm, session_id, track_license)
        except Exception as err:
            raise ProviderError(f"Failed to parse Widevine license: {err}") from err
        if not decryption_key:
            raise MediaNotFound(f"Unable to get decryption key for song {item_id}.")
        return decryption_key

    async def _get_license(self, challenge: bytes, license_url: str, uri: str, item_id: str) -> str:
        """Request a Widevine license from Apple Music."""
        data = {
            "challenge": base64.b64encode(challenge).decode("utf-8"),
            "key-system": "com.widevine.alpha",
            "uri": uri,
            "adamId": item_id,
            "isLibrary": False,
            "user-initiated": True,
        }
        try:
            async with self.http.post(
                license_url,
                json=data,
                headers=self._decryption_headers,
                timeout=ClientTimeout(total=30),
            ) as response:
                if response.status in (401, 403):
                    raise AuthError("Apple Music refused the license request")
                response.raise_for_status()
                content = await response.json(content_type=None)
        except (ClientError, TimeoutError) as err:
            raise ProviderError(f"License request failed: {err}") from err
        track_license = content.get("license")
        if not track_license:
            status = content.get("status")
            raise MediaNotFound(f"No license found for song {item_id} (status {status}).")
        return cast("str", track_license)


def _build_pssh(key_id: bytes) -> PSSH:
    """Build a Widevine PSSH object for the given key ID."""
    from pywidevine import PSSH
    from pywidevine.license_protocol_pb2 import WidevinePsshData

    pssh_data = WidevinePsshData()
    pssh_data.algorithm = WidevinePsshData.AESCTR
    pssh_data.key_ids.append(key_id)
    init_data = base64.b64encode(pssh_data.SerializeToString()).decode("utf-8")
    return PSSH.new(system_id=PSSH.SystemId.Widevine, init_data=init_data)

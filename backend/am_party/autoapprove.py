"""Automatic request review: fetch a song's lyrics and ask Jev whether to play it.

Lyrics come from LRCLIB (https://lrclib.net, free, no key). The decision comes from
Jev, TypeSafe's System One model (https://docs.typesafe.ai), asked as a two-option
Choice, either directly from TypeSafe or through OpenRouter's System One API, which
takes the same request and returns the same answer shape. Jev returns a calibrated probability per option; the caller acts on the
decision only when the chosen option's probability reaches the configured threshold,
and leaves the request for a person otherwise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from .models import Track

LOGGER = logging.getLogger(__name__)

LRCLIB_URL = "https://lrclib.net/api"
# provider -> System One endpoint; both take TypeSafe's request/response shape
PROVIDERS = {
    "typesafe": "https://api.typesafe.ai/v1/systemone",
    "openrouter": "https://openrouter.ai/api/v1/systemone",
}
PROVIDER_NAMES = {"typesafe": "TypeSafe", "openrouter": "OpenRouter"}
DEFAULT_PROVIDER = "typesafe"
DEFAULT_MODEL = "jev-latest"
USER_AGENT = "am-party (https://github.com/ingoau/am-party)"
LYRICS_TIMEOUT = aiohttp.ClientTimeout(total=10)
JEV_TIMEOUT = aiohttp.ClientTimeout(total=30)
MAX_LYRICS_CHARS = 12_000  # plenty for a song; keeps a pathological entry from bloating the request

DEFAULT_PROMPT = (
    "Should this song be played at the party? Approve songs that suit a party crowd. "
    "Reject songs whose lyrics are sexually explicit, hateful, contain slurs, or glorify "
    "violence or self-harm. A song that only swears a little is fine."
)
APPROVE_CRITERIA = "Approve: the song is suitable to play at this party"
REJECT_CRITERIA = "Reject: the song is not suitable to play at this party"


class ReviewError(Exception):
    """The review couldn't be completed (no API key, Jev error, bad response)."""


@dataclass
class Lyrics:
    text: str | None  # None when not found
    instrumental: bool = False


@dataclass
class Verdict:
    decision: str  # "approve" | "reject"
    probability: float  # Jev's probability for `decision`, 0..1
    lyrics_found: bool
    model: str | None = None


class AutoReviewer:
    """Looks up lyrics and asks Jev through the selected provider."""

    def __init__(self, http: aiohttp.ClientSession, api_keys: dict[str, str | None] | None = None) -> None:
        self.http = http
        self.api_keys: dict[str, str | None] = dict(api_keys or {})
        self.provider = DEFAULT_PROVIDER

    @property
    def configured(self) -> bool:
        return bool(self.api_keys.get(self.provider))

    async def review(self, track: Track, prompt: str, model: str) -> Verdict:
        provider = self.provider
        key = self.api_keys.get(provider)
        if not key:
            raise ReviewError(f"No {PROVIDER_NAMES.get(provider, provider)} API key configured")
        lyrics = await self.lyrics(track)
        return await self._ask_jev(
            provider, key, track, lyrics, prompt or DEFAULT_PROMPT, model or DEFAULT_MODEL
        )

    # -- lyrics --------------------------------------------------------------------
    async def lyrics(self, track: Track) -> Lyrics:
        """Best effort: never raises, returns Lyrics(None) when nothing usable was found."""
        try:
            params = {"track_name": track.title, "artist_name": track.artist}
            if track.album:
                params["album_name"] = track.album
            if track.duration:
                params["duration"] = str(round(track.duration))
            hit = await self._lrclib("/get", params)
            if not isinstance(hit, dict):
                # /get needs a near-exact match (album, duration); search is looser
                found = await self._lrclib(
                    "/search", {"track_name": track.title, "artist_name": track.artist}
                )
                hit = _best_search_hit(found, track) if isinstance(found, list) else None
            if isinstance(hit, dict):
                text = hit.get("plainLyrics")
                if isinstance(text, str) and text.strip():
                    return Lyrics(text.strip()[:MAX_LYRICS_CHARS])
                if hit.get("instrumental"):
                    return Lyrics(None, instrumental=True)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            LOGGER.info("Lyrics lookup failed for %s - %s: %s", track.artist, track.title, err)
        return Lyrics(None)

    async def _lrclib(self, path: str, params: dict[str, str]) -> Any:
        async with self.http.get(
            LRCLIB_URL + path,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=LYRICS_TIMEOUT,
        ) as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            return await resp.json(content_type=None)

    # -- Jev -------------------------------------------------------------------------
    async def _ask_jev(
        self, provider: str, key: str, track: Track, lyrics: Lyrics, prompt: str, model: str
    ) -> Verdict:
        name = PROVIDER_NAMES.get(provider, provider)
        payload = {
            "model": model,
            "state": build_state(track, lyrics),
            "questions": {
                "decision": {
                    "type": "choice",
                    "instructions": prompt,
                    "criteria": {"approve": APPROVE_CRITERIA, "reject": REJECT_CRITERIA},
                }
            },
        }
        try:
            async with self.http.post(
                PROVIDERS[provider],
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
                timeout=JEV_TIMEOUT,
            ) as resp:
                if resp.status >= 400:
                    detail = (await resp.text())[:200]
                    raise ReviewError(f"{name} returned HTTP {resp.status}: {detail}")
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ReviewError(f"Couldn't reach {name}: {err}") from err
        return parse_verdict(data, lyrics_found=lyrics.text is not None)


def build_state(track: Track, lyrics: Lyrics) -> str:
    lines = [f"Song: {track.title}", f"Artist: {track.artist}"]
    if track.album:
        lines.append(f"Album: {track.album}")
    lines.append(f"Apple Music explicit rating: {'explicit' if track.explicit else 'clean'}")
    if lyrics.text:
        lines += ["", "Lyrics:", lyrics.text]
    elif lyrics.instrumental:
        lines += ["", "Lyrics: none (instrumental)"]
    else:
        lines += ["", "Lyrics: not available"]
    return "\n".join(lines)


def parse_verdict(data: Any, lyrics_found: bool) -> Verdict:
    try:
        answer = data["answers"]["decision"]
        choice = answer["choice"]
        probs = answer.get("probabilities") or {}
        prob = float(probs.get(choice, answer.get("confidence", 0.0)))
    except (KeyError, TypeError, ValueError) as err:
        raise ReviewError("Unexpected response from Jev") from err
    if choice not in ("approve", "reject"):
        raise ReviewError(f"Unexpected Jev choice: {choice!r}")
    return Verdict(
        decision=choice,
        probability=max(0.0, min(1.0, prob)),
        lyrics_found=lyrics_found,
        model=data.get("model") if isinstance(data.get("model"), str) else None,
    )


def _best_search_hit(results: list[Any], track: Track) -> dict[str, Any] | None:
    """Closest-duration LRCLIB search result that has lyrics (or is instrumental)."""
    usable = [r for r in results if isinstance(r, dict) and (r.get("plainLyrics") or r.get("instrumental"))]
    if not usable:
        return None
    if track.duration:
        usable.sort(key=lambda r: abs(float(r.get("duration") or 0) - track.duration))
        if abs(float(usable[0].get("duration") or 0) - track.duration) > 15:
            return None  # probably a different recording/song
    return usable[0]

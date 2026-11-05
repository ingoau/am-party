"""Shared data types."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Internal PCM format used everywhere in the playback pipeline:
# interleaved float32 little-endian, 48 kHz, stereo.
SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_SAMPLE = 4
FRAME_SIZE = CHANNELS * BYTES_PER_SAMPLE  # bytes per frame
BYTES_PER_SECOND = SAMPLE_RATE * FRAME_SIZE


def seconds_to_bytes(seconds: float) -> int:
    """Convert seconds to a frame-aligned byte count."""
    return int(seconds * SAMPLE_RATE) * FRAME_SIZE


@dataclass
class Track:
    id: str
    title: str
    artist: str
    album: str | None = None
    duration: float = 0.0
    artwork_url: str | None = None
    explicit: bool = False
    isrc: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Track:
        return cls(
            id=str(d["id"]),
            title=d.get("title") or "",
            artist=d.get("artist") or "",
            album=d.get("album"),
            duration=float(d.get("duration") or 0),
            artwork_url=d.get("artwork_url"),
            explicit=bool(d.get("explicit")),
            isrc=d.get("isrc"),
        )


@dataclass
class PlaylistInfo:
    id: str
    name: str
    artwork_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StreamInfo:
    """How to fetch decodable audio for a track."""

    url: str
    decryption_key: str | None = None  # hex; for CENC-encrypted MP4
    codec: str | None = None  # ffmpeg decoder name to force, e.g. "aac"
    headers: dict[str, str] = field(default_factory=dict)

    def ffmpeg_input_args(self) -> list[str]:
        args: list[str] = [
            "-reconnect", "1",
            "-reconnect_delay_max", "10",
            "-reconnect_on_http_error", "5xx,429",
        ]
        if self.decryption_key:
            args += ["-decryption_key", self.decryption_key]
        if self.headers:
            args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in self.headers.items())]
        if self.codec:
            args += ["-acodec", self.codec]
        args += ["-i", self.url]
        return args


class ProviderError(Exception):
    """Music provider failed (auth, network, not found...)."""


class NotConfigured(ProviderError):
    """Provider has no credentials."""

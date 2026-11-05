"""
Tiny PCM helpers replacing the Music Assistant internals used by smart fades.

Adapted from Music Assistant (Apache-2.0), https://github.com/music-assistant/server
(music_assistant/helpers/audio.py, helpers/ffmpeg.py, music_assistant_models AudioFormat).
Modified for Encore: reduced to the float32/int PCM subset the smart-fades code needs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum

LOGGER = logging.getLogger("encore.smartfades")

# MA's extra-verbose level (below DEBUG)
VERBOSE_LOG_LEVEL = 5


class ContentType(Enum):
    """Raw PCM content types (``name.lower()`` is the ffmpeg codec, value the demuxer)."""

    PCM_S16LE = "s16le"
    PCM_S24LE = "s24le"
    PCM_S32LE = "s32le"
    PCM_F32LE = "f32le"
    PCM_F64LE = "f64le"


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """Minimal stand-in for MA's AudioFormat."""

    content_type: ContentType = ContentType.PCM_F32LE
    sample_rate: int = 48000
    bit_depth: int = 32
    channels: int = 2

    @property
    def frame_size(self) -> int:
        """Bytes per (multi-channel) frame."""
        return (self.bit_depth // 8) * self.channels

    @property
    def pcm_sample_size(self) -> int:
        """Bytes per second of audio (MA naming)."""
        return self.sample_rate * self.frame_size


def encore_pcm_format() -> AudioFormat:
    """Return the Encore pipeline format (f32le, 48 kHz, stereo)."""
    from encore.models import BYTES_PER_SAMPLE, CHANNELS, SAMPLE_RATE  # noqa: PLC0415

    return AudioFormat(ContentType.PCM_F32LE, SAMPLE_RATE, BYTES_PER_SAMPLE * 8, CHANNELS)


def get_ffmpeg_channel_args(audio_format: AudioFormat) -> list[str]:
    """Return the FFmpeg channel count/layout arguments for the given audio format."""
    args = ["-ac", str(audio_format.channels)]
    layout = {1: "mono", 2: "stereo"}.get(audio_format.channels)
    if layout:
        args += ["-channel_layout", layout]
    return args


def iter_pcm_slices(
    audio: bytes,
    pcm_format: AudioFormat,
    target_duration_ms: int = 100,
) -> Iterator[bytes]:
    """Yield frame-aligned PCM slices of approximately ``target_duration_ms``."""
    if not audio:
        return
    frame_size = max(1, pcm_format.bit_depth // 8) * pcm_format.channels
    samples_per_slice = max(1, round((target_duration_ms / 1000) * pcm_format.sample_rate))
    slice_size = max(frame_size, samples_per_slice * frame_size)
    offset = 0
    audio_len = len(audio)
    while offset < audio_len:
        end = min(audio_len, offset + slice_size)
        if end < audio_len:
            aligned_end = end - (end % frame_size)
            if aligned_end <= offset:
                aligned_end = min(audio_len, offset + frame_size)
            end = aligned_end
        yield audio[offset:end]
        offset = end


def align_audio_to_frame_boundary(audio_data: bytes, pcm_format: AudioFormat) -> bytes:
    """Align audio data to frame boundaries by truncating incomplete frames."""
    frame_size = (pcm_format.bit_depth // 8) * pcm_format.channels
    valid_bytes = (len(audio_data) // frame_size) * frame_size
    return audio_data[:valid_bytes]


async def communicate(args: list[str], stdin_data: bytes) -> tuple[int, bytes, bytes]:
    """Run a process, feed it ``stdin_data`` and collect stdout/stderr."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(stdin_data)
    return proc.returncode or 0, stdout, stderr


async def strip_silence(
    audio_data: bytes,
    pcm_format: AudioFormat,
    reverse: bool = False,
) -> bytes:
    """Strip silence from begin or end of pcm audio using ffmpeg (same filter as MA)."""
    args = ["ffmpeg", "-hide_banner", "-loglevel", "quiet"]
    args += [
        "-acodec",
        pcm_format.content_type.name.lower(),
        "-f",
        pcm_format.content_type.value,
        "-ac",
        str(pcm_format.channels),
        "-ar",
        str(pcm_format.sample_rate),
        "-i",
        "-",
    ]
    if reverse:
        args += [
            "-af",
            "areverse,atrim=start=0.2,silenceremove=start_periods=1"
            ":start_silence=0.1:start_threshold=0.02,areverse",
        ]
    else:
        args += [
            "-af",
            "atrim=start=0.2,silenceremove=start_periods=1:start_silence=0.1:start_threshold=0.02",
        ]
    args += ["-f", pcm_format.content_type.value, "-"]
    _returncode, stripped_data, _stderr = await communicate(args, audio_data)
    return stripped_data

"""
Smart Fades Mixer - Mixes audio tracks using smart fades.

Adapted from Music Assistant (Apache-2.0), https://github.com/music-assistant/server
(music_assistant/controllers/streams/smart_fades/mixer.py, v2.10.8).
Modified for am-party: analyses are passed in directly instead of being loaded via
StreamsController/StreamDetails; the mode is a plain string ("smart"/"standard"); smart planning runs in a thread.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from am_party.smartfades.mixing.fades import (
    SmartCrossFade,
    SmartFade,
    SmartFadeNotApplicable,
    StandardCrossFade,
)
from am_party.smartfades.mixing.helpers import detect_effective_audio_end
from am_party.smartfades.mixing.vocal import (
    PROTECTIVE_VOCAL_CONFIG,
    VocalMask,
    build_vocal_windows,
    parse_vocal_probabilities,
)
from am_party.smartfades.pcm import align_audio_to_frame_boundary, strip_silence
from am_party.smartfades.audio_analysis import AudioAnalysisData

if TYPE_CHECKING:
    import logging

    from am_party.smartfades.pcm import AudioFormat

SMART_CROSSFADE = "smart"
STANDARD_CROSSFADE = "standard"


class SmartFadesMixer:
    """Smart fades mixer class that mixes tracks based on analysis data."""

    def __init__(self, logger: logging.Logger) -> None:
        """Initialize smart fades mixer."""
        self.logger = logger

    async def build(
        self,
        fade_in_analysis: AudioAnalysisData | None,
        fade_out_analysis: AudioAnalysisData | None,
        pcm_format: AudioFormat,
        standard_crossfade_duration: float,
        mode: str,
        fade_out_data: bytes,
        fade_in_bytes_len: int,
    ) -> SmartFade:
        """
        Pick the SmartFade implementation, prime its filters, and return it.

        For the standard crossfade path (explicit mode or smart-crossfade fallback)
        the trailing silence in ``fade_out_data`` is measured so that ``timing_info``
        reflects the audio that will actually be rendered.  The trim itself is deferred
        to ``apply()``, which executes it as a plain slice — no bytes are modified here.

        :param fade_in_analysis: Analysis for the incoming track (or None).
        :param fade_out_analysis: Analysis for the outgoing track (or None).
        :param pcm_format: Audio format of both input buffers (and mix output).
        :param standard_crossfade_duration: Duration in seconds for standard crossfade.
        :param mode: Smart fades mode (SMART_CROSSFADE or STANDARD_CROSSFADE).
        :param fade_out_data: PCM buffer of the outgoing track's tail.
        :param fade_in_bytes_len: Expected length in bytes of the fade-in input.
        """
        # degradation chain: smart-crossfade → standard; richer modes prepend their builder
        smart_fade: SmartFade | None = None
        retained_out_analysis: AudioAnalysisData | None = None
        if mode == SMART_CROSSFADE:
            smart_fade, retained_out_analysis = await self._build_smart_crossfade(
                fade_in_analysis=fade_in_analysis,
                fade_out_analysis=fade_out_analysis,
                fade_out_bytes_len=len(fade_out_data),
                fade_in_bytes_len=fade_in_bytes_len,
                pcm_format=pcm_format,
            )
        if smart_fade is None:
            smart_fade = await self._build_standard_crossfade(
                fade_out_data=fade_out_data,
                fade_in_bytes_len=fade_in_bytes_len,
                pcm_format=pcm_format,
                standard_crossfade_duration=standard_crossfade_duration,
                fade_out_analysis=retained_out_analysis,
            )
        return smart_fade

    async def mix(
        self,
        smart_fade: SmartFade,
        fade_in_part: bytes | AsyncGenerator[bytes],
        fade_out_part: bytes,
        pcm_format: AudioFormat,
    ) -> AsyncGenerator[bytes]:
        """Run the already-built SmartFade and yield mixed PCM audio chunks."""
        async for chunk in smart_fade.apply(fade_out_part, fade_in_part, pcm_format):
            yield chunk

    async def _build_standard_crossfade(
        self,
        fade_out_data: bytes,
        fade_in_bytes_len: int,
        pcm_format: AudioFormat,
        standard_crossfade_duration: float,
        fade_out_analysis: AudioAnalysisData | None = None,
    ) -> StandardCrossFade:
        """
        Build a StandardCrossFade — the tail of the degradation chain, never fails.

        Measures the trailing silence here so timing_info reflects the audio that
        will actually be rendered; apply() executes the cut as a plain slice.

        :param fade_out_data: PCM buffer of the outgoing track's tail.
        :param fade_in_bytes_len: Expected length in bytes of the fade-in input.
        :param pcm_format: Audio format of both input buffers.
        :param standard_crossfade_duration: Duration in seconds for standard crossfade.
        :param fade_out_analysis: Outgoing analysis retained from a failed smart
            build, or ``None`` for the regular standard path.
        """
        trailing_silence_bytes = 0
        try:
            stripped = align_audio_to_frame_boundary(
                await strip_silence(fade_out_data, pcm_format=pcm_format, reverse=True),
                pcm_format,
            )
            retained_bytes = max(
                len(stripped),
                self._get_vocal_retention_bytes(
                    fade_out_analysis,
                    len(fade_out_data),
                    pcm_format,
                ),
            )
            trailing_silence_bytes = max(0, len(fade_out_data) - retained_bytes)
        except Exception as err:
            # a failed measurement degrades to the old late-boundary bookkeeping
            # instead of killing the stream
            self.logger.warning("Measuring trailing silence failed: %s", err)
        smart_fade = StandardCrossFade(
            logger=self.logger,
            crossfade_duration=standard_crossfade_duration,
            trailing_silence_bytes=trailing_silence_bytes,
        )
        smart_fade.build(len(fade_out_data) - trailing_silence_bytes, fade_in_bytes_len, pcm_format)
        self.logger.debug(
            "Built standard fade: tail=%.2fs silence=%.2fs usable=%.2fs fade_in=%.2fs "
            "requested=%.2fs applied=%.2fs",
            len(fade_out_data) / pcm_format.pcm_sample_size,
            trailing_silence_bytes / pcm_format.pcm_sample_size,
            (len(fade_out_data) - trailing_silence_bytes) / pcm_format.pcm_sample_size,
            fade_in_bytes_len / pcm_format.pcm_sample_size,
            float(standard_crossfade_duration),
            smart_fade.timing_info.crossfade_duration,
        )
        return smart_fade

    async def _build_smart_crossfade(
        self,
        fade_in_analysis: AudioAnalysisData | None,
        fade_out_analysis: AudioAnalysisData | None,
        fade_out_bytes_len: int,
        fade_in_bytes_len: int,
        pcm_format: AudioFormat,
    ) -> tuple[SmartFade | None, AudioAnalysisData | None]:
        """
        Attempt to build a SmartCrossFade and retain outgoing analysis for fallback.

        Returns the built fade (or ``None`` when fallback is needed) together
        with the outgoing analysis row, when available.
        """
        if not (
            fade_out_analysis
            and fade_in_analysis
            and fade_out_analysis.bpm
            and fade_in_analysis.bpm
            and fade_out_analysis.beats is not None
            and fade_in_analysis.beats is not None
        ):
            self.logger.debug(
                "Smart fade lacks analysis: out_row=%s out_bpm=%s out_beats=%s | "
                "in_row=%s in_bpm=%s in_beats=%s",
                fade_out_analysis is not None,
                fade_out_analysis.bpm if fade_out_analysis else None,
                fade_out_analysis.beats is not None if fade_out_analysis else None,
                fade_in_analysis is not None,
                fade_in_analysis.bpm if fade_in_analysis else None,
                fade_in_analysis.beats is not None if fade_in_analysis else None,
            )
            return None, fade_out_analysis
        try:
            smart_fade = SmartCrossFade(
                logger=self.logger,
                fade_out_analysis=fade_out_analysis,
                fade_in_analysis=fade_in_analysis,
            )
            # planning is pure numpy (10-150 ms): keep it off the event loop
            await asyncio.to_thread(
                smart_fade.build, fade_out_bytes_len, fade_in_bytes_len, pcm_format
            )
        except SmartFadeNotApplicable as e:
            self.logger.debug("Smart crossfade not applicable: %s - using standard crossfade", e)
            return None, fade_out_analysis
        except Exception as e:
            self.logger.warning(
                "Smart crossfade build failed: %s, falling back to standard crossfade", e
            )
            return None, fade_out_analysis
        return smart_fade, fade_out_analysis

    @staticmethod
    def _get_vocal_retention_bytes(
        analysis: AudioAnalysisData | None,
        fade_out_bytes_len: int,
        pcm_format: AudioFormat,
    ) -> int:
        """
        Return the frame-aligned outgoing length needed to retain an audible vocal.

        Invalid or stale vocal data, missing/invalid RMS data, and vocals beyond
        the RMS-audible boundary return zero so standard silence removal keeps its
        existing behavior.

        :param analysis: Outgoing track analysis retained from the smart path.
        :param fade_out_bytes_len: Full outgoing PCM buffer length in bytes.
        :param pcm_format: Audio format of the outgoing buffer.
        """
        if analysis is None or analysis.rms_energy is None:
            return 0
        rms_energy = analysis.rms_energy
        if len(rms_energy) < 2 or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0.0
            for value in rms_energy
        ):
            return 0
        timeline = parse_vocal_probabilities(analysis)
        if timeline is None:
            return 0

        buffer_duration = fade_out_bytes_len / pcm_format.pcm_sample_size
        track_duration = analysis.duration
        assert track_duration is not None  # guaranteed by the validated timeline
        buffer_offset = max(0.0, track_duration - buffer_duration)
        vocal_mask = build_vocal_windows(
            timeline.probabilities,
            timeline.frame_duration,
            buffer_offset,
            track_duration,
            beat_duration=60.0 / analysis.bpm if analysis.bpm and analysis.bpm > 0.0 else None,
            config=PROTECTIVE_VOCAL_CONFIG,
        )
        if not vocal_mask.windows:
            return 0

        audio_end = detect_effective_audio_end(
            rms_energy,
            track_duration,
            buffer_duration,
        )
        buffer_local_mask = VocalMask(
            windows=[
                (left - buffer_offset, right - buffer_offset) for left, right in vocal_mask.windows
            ]
        ).clamped_to(audio_end)
        retained_seconds = buffer_local_mask.last_end()
        frame_size = pcm_format.channels * pcm_format.bit_depth // 8
        retained_frames = math.ceil(retained_seconds * pcm_format.pcm_sample_size / frame_size)
        return min(fade_out_bytes_len, retained_frames * frame_size)

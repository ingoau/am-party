"""
Encore glue: build one track transition (smart -> standard -> cut).

Uses the vendored Music Assistant smart-fades mixer (``encore.smartfades.mixing``).
"""

from __future__ import annotations

import logging
import math
import shutil
from dataclasses import dataclass
from typing import Any

from encore.models import BYTES_PER_SECOND, FRAME_SIZE

LOGGER = logging.getLogger("encore.smartfades")

# MA: SMART_CROSSFADE_DURATION (outgoing tail held back for smart mode)
TAIL_SECONDS_SMART = 45.0
# MA: MIN_CROSSFADE_DURATION - a shorter usable window means no fade at all
MIN_CROSSFADE_SECONDS = 3.0
# standard-fade length used when smart mode degrades but no standard length is set
DEFAULT_STANDARD_FALLBACK_SECONDS = 8.0
# a rendered transition whose length is off by more than this is distrusted
_LENGTH_TOLERANCE_SECONDS = 0.05


def _norm_mode(mode: str) -> str:
    mode = (mode or "").strip().lower()
    return mode if mode in ("smart", "standard", "off") else "standard"


def tail_seconds(mode: str, standard_duration: float) -> float:
    """
    Return how much of the outgoing track's tail the engine should hold back.

    The engine should additionally cap it to half the outgoing track's duration
    (as Music Assistant does) - ``build_transition`` copes with any shorter tail.
    """
    mode = _norm_mode(mode)
    if mode == "off":
        return 0.0
    if mode == "smart":
        return TAIL_SECONDS_SMART
    return max(0.0, float(standard_duration))


def head_seconds(mode: str, standard_duration: float) -> float:
    """
    Return how much of the incoming track's head the engine must supply.

    Should be capped by the engine to half the incoming track's duration and to the
    tail actually held back; shorter heads are fine.
    """
    return tail_seconds(mode, standard_duration)


@dataclass
class TransitionResult:
    """A rendered transition that replaces (outgoing tail + incoming head)."""

    pcm: bytes  # after it the engine continues the incoming track at byte offset len(in_head)
    handover_offset: float  # seconds into pcm where "now playing" switches to the incoming track
    in_position_at_end: float  # incoming track media position (s) at the end of pcm
    in_start_offset: float  # seconds into pcm where incoming position 0 is (would be) audible
    mode_used: str  # "smart" | "standard" | "cut"


def _align(data: bytes) -> bytes:
    return data[: len(data) - (len(data) % FRAME_SIZE)]


def _cut(out_tail: bytes, in_head: bytes) -> TransitionResult:
    out_s = len(out_tail) / BYTES_PER_SECOND
    return TransitionResult(
        pcm=out_tail + in_head,
        handover_offset=out_s,
        in_position_at_end=len(in_head) / BYTES_PER_SECOND,
        in_start_offset=out_s,
        mode_used="cut",
    )


def _parse_analysis(data: Any, logger: logging.Logger) -> Any:
    if not data:
        return None
    from encore.smartfades.audio_analysis import AudioAnalysisData  # noqa: PLC0415

    if isinstance(data, AudioAnalysisData):
        return data
    try:
        return AudioAnalysisData.from_dict(dict(data))
    except Exception as err:
        logger.debug("Ignoring malformed analysis: %s", err)
        return None


def _sane(pcm: bytes) -> bool:
    """Reject renders with non-finite samples or exploded levels."""
    import numpy as np  # noqa: PLC0415

    samples = np.frombuffer(pcm, dtype="<f4")
    if not samples.size:
        return False
    if not np.isfinite(samples).all():
        return False
    return float(np.abs(samples).max()) <= 4.0  # ~+12 dBFS: clearly broken beyond this


async def _render(
    mixer: Any,
    fade: Any,
    out_tail: bytes,
    in_head: bytes,
    pcm_format: Any,
    mode_used: str,
    logger: logging.Logger,
) -> TransitionResult | None:
    """Run a built fade and convert its timing info into a TransitionResult."""
    chunks = [chunk async for chunk in mixer.mix(fade, in_head, out_tail, pcm_format)]
    pcm = _align(b"".join(chunks))
    timing = fade.timing_info
    expected = (
        timing.pre_crossfade_duration + timing.crossfade_duration + timing.post_crossfade_duration
    )
    actual = len(pcm) / BYTES_PER_SECOND
    in_len_s = len(in_head) / BYTES_PER_SECOND
    if abs(actual - expected) > _LENGTH_TOLERANCE_SECONDS:
        # the mix always ends exactly where the incoming head ends; a large mismatch
        # means the incoming side was cut short or padded - never risk a skip/dup
        logger.warning(
            "%s transition rendered %.3fs, expected %.3fs - discarding",
            mode_used,
            actual,
            expected,
        )
        return None
    if not _sane(pcm):
        logger.warning("%s transition produced non-finite or exploded audio - discarding", mode_used)
        return None
    # The mix ends where the incoming head ends, so incoming position 0 sits exactly
    # len(in_head) before the end of pcm (the incoming side is never time-stretched).
    in_start = actual - in_len_s
    planned_start = timing.pre_crossfade_duration - timing.fadein_trimmed_duration
    if abs(planned_start - in_start) > 0.02:
        logger.debug(
            "%s transition: incoming start %.3fs differs from plan %.3fs",
            mode_used,
            in_start,
            planned_start,
        )
    handover = timing.pre_crossfade_duration + timing.crossfade_duration / 2.0
    handover = min(max(0.0, handover), actual)
    return TransitionResult(
        pcm=pcm,
        handover_offset=handover,
        in_position_at_end=in_len_s,
        in_start_offset=in_start,
        mode_used=mode_used,
    )


async def build_transition(
    out_tail: bytes,
    in_head: bytes,
    out_analysis: dict | None,
    in_analysis: dict | None,
    mode: str,
    standard_duration: float,
    logger: logging.Logger | None = None,
) -> TransitionResult:
    """
    Render the transition between two tracks. Never raises for planning/render problems.

    :param out_tail: The held-back end of the outgoing track (f32le/48k/stereo).
    :param in_head: The start of the incoming track (same format).
    :param out_analysis: AudioAnalysisData dict of the whole outgoing track, or None.
    :param in_analysis: AudioAnalysisData dict of the whole incoming track, or None.
    :param mode: "smart", "standard" or "off".
    :param standard_duration: Standard crossfade length in seconds.
    :param logger: Optional logger.
    """
    logger = logger or LOGGER
    out_tail = _align(bytes(out_tail))
    in_head = _align(bytes(in_head))
    mode = _norm_mode(mode)
    window = min(len(out_tail), len(in_head)) / BYTES_PER_SECOND
    try:
        standard_duration = float(standard_duration)
    except (TypeError, ValueError):
        standard_duration = 0.0
    if not math.isfinite(standard_duration):
        standard_duration = 0.0
    if mode == "off" or window < MIN_CROSSFADE_SECONDS:
        return _cut(out_tail, in_head)
    if mode == "standard" and standard_duration <= 0:
        return _cut(out_tail, in_head)
    if standard_duration <= 0:
        # smart mode without a configured standard length: fallback fade length
        standard_duration = DEFAULT_STANDARD_FALLBACK_SECONDS

    try:
        from encore.smartfades.mixing.fades import SmartCrossFade  # noqa: PLC0415
        from encore.smartfades.mixing.mixer import (  # noqa: PLC0415
            SMART_CROSSFADE,
            STANDARD_CROSSFADE,
            SmartFadesMixer,
        )
        from encore.smartfades.pcm import encore_pcm_format  # noqa: PLC0415
    except Exception as err:  # e.g. numpy missing
        logger.warning("Smart fades mixer unavailable (%s) - cutting", err)
        return _cut(out_tail, in_head)

    if shutil.which("ffmpeg") is None:
        logger.warning("ffmpeg not found - cutting instead of crossfading")
        return _cut(out_tail, in_head)
    pcm_format = encore_pcm_format()
    mixer = SmartFadesMixer(logger)
    out_a = _parse_analysis(out_analysis, logger)
    in_a = _parse_analysis(in_analysis, logger)

    attempts = [SMART_CROSSFADE, STANDARD_CROSSFADE] if mode == "smart" else [STANDARD_CROSSFADE]
    standard_tried = False
    for attempt in attempts:
        if attempt == STANDARD_CROSSFADE and standard_tried:
            continue
        try:
            fade = await mixer.build(
                fade_in_analysis=in_a,
                fade_out_analysis=out_a,
                pcm_format=pcm_format,
                standard_crossfade_duration=standard_duration,
                mode=attempt,
                fade_out_data=out_tail,
                fade_in_bytes_len=len(in_head),
            )
            mode_used = "smart" if isinstance(fade, SmartCrossFade) else "standard"
            if mode_used == "standard":
                # the mixer may already have degraded smart -> standard; don't redo it
                standard_tried = True
            logger.debug("Transition (%s): %r", mode_used, fade)
            result = await _render(mixer, fade, out_tail, in_head, pcm_format, mode_used, logger)
            if result is not None:
                return result
        except Exception as err:
            logger.warning("%s transition failed: %s", attempt, err, exc_info=True)
    return _cut(out_tail, in_head)

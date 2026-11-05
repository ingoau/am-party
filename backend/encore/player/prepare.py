"""Track preparation: resolve stream, decode to PCM file, measure loudness, analyse.

Because a party queue is known ahead of time we decode the whole upcoming track to a
local PCM file while the current one plays. That gives us gapless reads, exact
lengths and lets the smart-fade analysis run on the full track *before* the mix,
which Music Assistant can only do after a track has been played once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import BYTES_PER_SECOND, CHANNELS, SAMPLE_RATE, StreamInfo, Track

LOGGER = logging.getLogger(__name__)

TARGET_LUFS = -14.0
MAX_BOOST_DB = 9.0
TRUE_PEAK_CEILING_DB = -1.0
ANALYSIS_VERSION_KEY = "analysis_version"

_I_RE = re.compile(r"I:\s+(-?[\d.]+|-inf)\s+LUFS")
_PEAK_RE = re.compile(r"Peak:\s+(-?[\d.]+|-inf)\s+dBFS")


@dataclass
class PreparedTrack:
    track: Track
    pcm_path: Path
    size: int = 0  # bytes of PCM
    gain: float = 1.0  # linear gain to apply on read
    loudness: float | None = None
    analysis: dict[str, Any] | None = None
    decoded: asyncio.Event = field(default_factory=asyncio.Event)
    analysed: asyncio.Event = field(default_factory=asyncio.Event)
    error: str | None = None
    prepared_at: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return self.decoded.is_set() and self.error is None and self.size > 0

    @property
    def duration(self) -> float:
        return self.size / BYTES_PER_SECOND


StreamResolver = Callable[[str], Awaitable[StreamInfo]]
AnalysisLoader = Callable[[str], Awaitable[dict[str, Any] | None]]
AnalysisSaver = Callable[[str, dict[str, Any]], Awaitable[None]]


class TrackPreparer:
    """Decodes and analyses tracks into a small on-disk cache."""

    def __init__(
        self,
        cache_dir: Path,
        resolve_stream: StreamResolver,
        analysis_worker: Any | None,
        load_analysis: AnalysisLoader,
        save_analysis: AnalysisSaver,
        want_analysis: Callable[[], bool],
    ) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        for stale in self.cache_dir.glob("*.f32*"):
            stale.unlink(missing_ok=True)
        self.resolve_stream = resolve_stream
        self.analysis_worker = analysis_worker
        self.load_analysis = load_analysis
        self.save_analysis = save_analysis
        self.want_analysis = want_analysis
        self._items: dict[str, PreparedTrack] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._decode_lock = asyncio.Lock()  # one Apple stream at a time
        self._analysis_lock = asyncio.Lock()

    def get(self, track_id: str) -> PreparedTrack | None:
        return self._items.get(track_id)

    def prepare(self, track: Track) -> PreparedTrack:
        """Start preparing (idempotent). Returns the handle immediately."""
        existing = self._items.get(track.id)
        if existing and not existing.error:
            return existing
        item = PreparedTrack(track=track, pcm_path=self.cache_dir / f"{_safe(track.id)}.f32")
        self._items[track.id] = item
        self._tasks[track.id] = asyncio.create_task(self._run(item), name=f"prepare-{track.id}")
        return item

    async def wait_decoded(self, item: PreparedTrack, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(item.decoded.wait(), timeout)
        except TimeoutError:
            return False
        return item.ok

    def retain_only(self, keep_ids: set[str]) -> None:
        """Drop cached tracks not in keep_ids."""
        for tid in list(self._items):
            if tid in keep_ids:
                continue
            item = self._items.pop(tid)
            if task := self._tasks.pop(tid, None):
                if not task.done():
                    task.cancel()
            item.pcm_path.unlink(missing_ok=True)
            item.pcm_path.with_suffix(".f32.part").unlink(missing_ok=True)

    async def close(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        self.retain_only(set())

    # ------------------------------------------------------------------------
    async def _run(self, item: PreparedTrack) -> None:
        try:
            await self._decode(item)
        except asyncio.CancelledError:
            item.error = "cancelled"
            item.decoded.set()
            item.analysed.set()
            raise
        except Exception as err:  # noqa: BLE001
            LOGGER.warning("Preparing %s failed: %s", item.track.id, err)
            item.error = str(err) or err.__class__.__name__
            item.decoded.set()
            item.analysed.set()
            return
        item.decoded.set()
        try:
            await self._analyse(item)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            LOGGER.warning("Analysis of %s failed: %s", item.track.id, err)
        finally:
            item.analysed.set()

    async def _decode(self, item: PreparedTrack) -> None:
        async with self._decode_lock:
            started = time.monotonic()
            stream = await self.resolve_stream(item.track.id)
            part = item.pcm_path.with_suffix(".f32.part")
            args = [
                "ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", "-y",
                *stream.ffmpeg_input_args(),
                "-vn", "-map", "0:a:0",
                "-af", "aresample=resampler=soxr:precision=28,ebur128=peak=true:framelog=quiet",
                "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
                "-f", "f32le", "-acodec", "pcm_f32le", str(part),
            ]  # fmt: skip
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=300)
            except BaseException:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                part.unlink(missing_ok=True)
                raise
            stderr = stderr_b.decode(errors="replace")
            if proc.returncode != 0:
                part.unlink(missing_ok=True)
                tail = "\n".join(l for l in stderr.splitlines()[-5:] if "LUFS" not in l)
                raise RuntimeError(f"ffmpeg exited {proc.returncode}: {tail}")
            part.rename(item.pcm_path)
            item.size = item.pcm_path.stat().st_size
            item.size -= item.size % 8
            if item.size < BYTES_PER_SECOND:
                raise RuntimeError("decoded audio is too short")
            item.loudness, item.gain = _gain_from_ebur128(stderr)
            LOGGER.info(
                "Prepared %s - %s (%.0fs, %.1f LUFS, gain %.2f) in %.1fs",
                item.track.artist,
                item.track.title,
                item.duration,
                item.loudness if item.loudness is not None else float("nan"),
                item.gain,
                time.monotonic() - started,
            )

    async def _analyse(self, item: PreparedTrack) -> None:
        cached = await self.load_analysis(item.track.id)
        if cached:
            item.analysis = cached
            return
        worker = self.analysis_worker
        if not worker or not getattr(worker, "available", False) or not self.want_analysis():
            return
        async with self._analysis_lock:
            if not item.pcm_path.exists():
                return
            started = time.monotonic()
            result = await worker.analyze_file(str(item.pcm_path))
            if result:
                item.analysis = result
                await self.save_analysis(item.track.id, result)
                LOGGER.info(
                    "Analysed %s (bpm %s) in %.1fs",
                    item.track.title,
                    result.get("bpm"),
                    time.monotonic() - started,
                )


def _gain_from_ebur128(stderr: str) -> tuple[float | None, float]:
    """Parse the ebur128 summary and compute a static normalisation gain."""
    summary = stderr[stderr.rfind("Summary:") :] if "Summary:" in stderr else stderr
    m_i = _I_RE.search(summary)
    if not m_i or m_i.group(1) == "-inf":
        return None, 1.0
    integrated = float(m_i.group(1))
    gain_db = min(TARGET_LUFS - integrated, MAX_BOOST_DB)
    m_p = _PEAK_RE.search(summary)
    if m_p and m_p.group(1) != "-inf":
        peak = float(m_p.group(1))
        gain_db = min(gain_db, TRUE_PEAK_CEILING_DB - peak)
    return integrated, 10 ** (gain_db / 20)


def _safe(track_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", track_id)


def dumps_analysis(data: dict[str, Any]) -> str:
    return json.dumps(data, separators=(",", ":"))

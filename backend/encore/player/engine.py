"""Playback engine: turns the party queue into one continuous PCM stream.

Flow per track:
  - the track is decoded ahead of time to a PCM file (see prepare.py)
  - we stream it to the Sendspin output in small chunks; the output paces us
  - ~LOCK_AHEAD seconds before the outgoing tail starts we lock in the next track
    and render the transition (smart or standard crossfade) in the background
  - at the tail we play the rendered transition, then continue the incoming track
    from the end of its head section, so the whole thing is gapless.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from ..models import BYTES_PER_SECOND, FRAME_SIZE, Track, seconds_to_bytes
from .prepare import PreparedTrack, TrackPreparer

LOGGER = logging.getLogger(__name__)

CHUNK_SECONDS = 0.5
LOCK_AHEAD = 25.0  # seconds before the tail starts that we choose next + render
MIN_TRANSITION = 3.0
SKIP_WAIT_DECODE = 60.0


class Output(Protocol):
    async def begin_stream(self) -> None: ...
    async def write(self, pcm: bytes) -> float: ...
    async def end_stream(self, drain: bool = True) -> None: ...
    def set_metadata(
        self,
        title: str,
        artist: str,
        album: str | None,
        artwork_url: str | None,
        duration_s: float,
        position_s: float,
        playing: bool,
    ) -> None: ...


@dataclass
class Candidate:
    """A track the party wants to play next."""

    track: Track
    queue_item_id: int | None
    source: str  # request | admin | fallback


@dataclass
class NowPlaying:
    candidate: Candidate
    prepared: PreparedTrack
    started_at: float  # wall clock when position 0 was/would have been audible


class PartyHooks(Protocol):
    def peek_next(self) -> Candidate | None: ...
    def peek_upcoming(self, n: int) -> list[Candidate]: ...
    async def on_track_started(self, now: NowPlaying) -> None: ...
    async def on_track_failed(self, cand: Candidate, error: str) -> None: ...
    async def on_engine_state(self) -> None: ...
    def settings(self) -> dict[str, Any]: ...


BuildTransition = Callable[..., Awaitable[Any]]


class Engine:
    def __init__(
        self,
        output: Output,
        preparer: TrackPreparer,
        hooks: PartyHooks,
        build_transition: BuildTransition | None,
        tail_seconds: Callable[[str, float], float],
        head_seconds: Callable[[str, float], float],
    ) -> None:
        self.output = output
        self.preparer = preparer
        self.hooks = hooks
        self.build_transition = build_transition
        self.tail_seconds = tail_seconds
        self.head_seconds = head_seconds
        self.now: NowPlaying | None = None
        self.locked: Candidate | None = None
        self.paused = False
        self.last_transition: str | None = None
        self.last_error: str | None = None
        self._wake = asyncio.Event()
        self._skip = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._prefetch_task: asyncio.Task | None = None
        self._handover_handles: list[asyncio.TimerHandle] = []

    # -- public control ------------------------------------------------------
    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="engine")

    async def stop(self) -> None:
        for t in (self._task, self._prefetch_task):
            if t:
                t.cancel()
        self._cancel_handovers()

    def poke(self) -> None:
        """Queue/settings changed: wake the engine and refresh prefetching."""
        self._wake.set()
        self.schedule_prefetch()

    def skip(self) -> None:
        self._skip.set()
        self._wake.set()

    async def pause(self) -> None:
        if not self.paused:
            self.paused = True
            self._wake.set()

    async def resume(self) -> None:
        if self.paused:
            self.paused = False
            self._wake.set()

    def position(self) -> float:
        if not self.now:
            return 0.0
        return max(0.0, min(time.time() - self.now.started_at, self.now.prepared.duration))

    # -- prefetch ------------------------------------------------------------
    def schedule_prefetch(self) -> None:
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
        self._prefetch_task = asyncio.create_task(self._prefetch(), name="prefetch")

    async def _prefetch(self) -> None:
        await asyncio.sleep(0.2)  # debounce bursts of queue edits
        keep: set[str] = set()
        if self.now:
            keep.add(self.now.prepared.track.id)
        wanted: list[Candidate] = []
        if self.locked:
            wanted.append(self.locked)
        wanted += self.hooks.peek_upcoming(2)
        for cand in wanted[:2]:
            keep.add(cand.track.id)
        self.preparer.retain_only(keep)
        for cand in wanted[:2]:
            self.preparer.prepare(cand.track)

    # -- main loop -----------------------------------------------------------
    async def _run(self) -> None:
        while True:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                LOGGER.exception("Engine error")
                self.last_error = str(err)
                await asyncio.sleep(2)

    async def _wait_wake(self, timeout: float | None = None) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
        except TimeoutError:
            pass

    async def _run_once(self) -> None:
        # idle until we have something to play
        while self.paused or self.hooks.peek_next() is None:
            await self._set_idle()
            await self._wait_wake(5)
        self._skip.clear()
        cand = self.hooks.peek_next()
        assert cand
        self.locked = cand
        item = await self._ensure_decoded(cand)
        if item is None:
            return
        await self.output.begin_stream()
        await self._play_from(cand, item, pos=0, started_at=None)

    async def _set_idle(self) -> None:
        if self.now is not None:
            self.now = None
            self._cancel_handovers()
            await self.hooks.on_engine_state()

    async def _ensure_decoded(self, cand: Candidate, timeout: float = SKIP_WAIT_DECODE) -> PreparedTrack | None:
        item = self.preparer.prepare(cand.track)
        ok = await self.preparer.wait_decoded(item, timeout)
        if not ok:
            err = item.error or "timed out preparing track"
            LOGGER.warning("Skipping %s: %s", cand.track.title, err)
            self.last_error = f"{cand.track.title}: {err}"
            if self.locked is cand:
                self.locked = None
            await self.hooks.on_track_failed(cand, err)
            return None
        return item

    def _read(self, item: PreparedTrack, start: int, length: int) -> bytes:
        length = max(0, min(length, item.size - start))
        length -= length % FRAME_SIZE
        if length <= 0:
            return b""
        with open(item.pcm_path, "rb") as fh:
            fh.seek(start)
            data = fh.read(length)
        if item.gain != 1.0:
            arr = np.frombuffer(data, dtype="<f4") * np.float32(item.gain)
            np.clip(arr, -1.0, 1.0, out=arr)
            data = arr.astype("<f4").tobytes()
        return data

    def _mode(self) -> tuple[str, float]:
        s = self.hooks.settings()
        mode = s.get("crossfade_mode", "smart")
        if mode == "smart" and self.build_transition is None:
            mode = "standard"
        return mode, float(s.get("crossfade_duration", 8))

    async def _write_chunks(self, pcm: bytes) -> tuple[float | None, bool]:
        """Write pcm in chunks. Returns (time first sample is heard, interrupted)."""
        first: float | None = None
        step = seconds_to_bytes(CHUNK_SECONDS)
        for off in range(0, len(pcm), step):
            if self._interrupted():
                return first, True
            t = await self.output.write(pcm[off : off + step])
            if first is None:
                first = t
        return first, False

    def _interrupted(self) -> bool:
        return self.paused or self._skip.is_set()

    async def _play_from(
        self, cand: Candidate, item: PreparedTrack, pos: int, started_at: float | None
    ) -> None:
        """Play `item` from byte `pos`, chaining into following tracks until idle."""
        if started_at is not None:
            await self._handover(cand, item, started_at)
        while True:
            mode, std_dur = self._mode()
            tail_len = 0
            if mode != "off" and self.build_transition is not None:
                tail_len = seconds_to_bytes(
                    min(self.tail_seconds(mode, std_dur), item.duration / 2)
                )
                if tail_len < seconds_to_bytes(MIN_TRANSITION):
                    tail_len = 0
            tail_start = max(pos, item.size - tail_len)
            lock_at = max(pos, tail_start - seconds_to_bytes(LOCK_AHEAD))
            build: asyncio.Task | None = None
            nxt: Candidate | None = None
            step = seconds_to_bytes(CHUNK_SECONDS)

            # body of the track
            while pos < tail_start:
                if self._interrupted():
                    if build:
                        build.cancel()
                    await self._interrupt(cand, item)
                    return
                if build is None and nxt is None and pos >= lock_at:
                    nxt = self._lock_next()
                    if nxt and tail_len:
                        build = asyncio.create_task(
                            self._build(item, tail_start, nxt, mode, std_dur), name="transition"
                        )
                chunk = self._read(item, pos, min(step, tail_start - pos))
                if not chunk:
                    break
                t = await self.output.write(chunk)
                if started_at is None:
                    started_at = t - pos / BYTES_PER_SECOND
                    await self._handover(cand, item, started_at)
                pos += len(chunk)

            result = None
            if build is not None:
                try:
                    result = await build
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001
                    LOGGER.warning("Transition failed, playing through: %s", err)
                    result = None

            if result is None:
                # no transition: play the rest of this track, then the next from 0
                rest = self._read(item, pos, item.size - pos)
                if rest:
                    t, interrupted = await self._write_chunks(rest)
                    if started_at is None and t is not None:
                        started_at = t - pos / BYTES_PER_SECOND
                        await self._handover(cand, item, started_at)
                    if interrupted:
                        await self._interrupt(cand, item)
                        return
                if nxt is None:
                    nxt = self._lock_next()
                if nxt is None:
                    await self.output.end_stream(drain=True)
                    self.locked = None
                    await self._set_idle()
                    return
                nitem = await self._ensure_decoded(nxt)
                if nitem is None:
                    nxt = None
                    # try whatever is next now
                    nxt = self._lock_next()
                    if nxt is None or (nitem := await self._ensure_decoded(nxt)) is None:
                        await self.output.end_stream(drain=True)
                        await self._set_idle()
                        return
                self.last_transition = "gapless"
                cand, item, pos, started_at = nxt, nitem, 0, None
                continue

            # play rendered transition
            nitem, res = result
            self.last_transition = res.mode_used
            t0, interrupted = await self._write_chunks(res.pcm)
            if t0 is None:
                t0 = time.time()
            next_started = t0 + res.in_start_offset
            self._schedule_handover(nxt, nitem, next_started, t0 + res.handover_offset)
            if interrupted:
                # we were mid-transition; treat incoming track as current
                self._cancel_handovers()
                await self._handover(nxt, nitem, next_started)
                await self._interrupt(nxt, nitem)
                return
            cand, item = nxt, nitem  # type: ignore[assignment]
            pos = seconds_to_bytes(res.in_position_at_end)
            started_at = next_started
            # handover callback (scheduled) updates now playing at the right moment

    def _lock_next(self) -> Candidate | None:
        cand = self.hooks.peek_next()
        self.locked = cand
        if cand:
            self.preparer.prepare(cand.track)
        self.schedule_prefetch()
        return cand

    async def _build(
        self, out_item: PreparedTrack, tail_start: int, nxt: Candidate, mode: str, std_dur: float
    ) -> tuple[PreparedTrack, Any] | None:
        nitem = self.preparer.prepare(nxt.track)
        remaining = max(1.0, (tail_start / BYTES_PER_SECOND) - self.position() - 3)
        if not await self.preparer.wait_decoded(nitem, remaining):
            LOGGER.warning("Next track not ready in time for a crossfade")
            return None
        if mode == "smart" and not nitem.analysed.is_set():
            remaining = max(0.0, (tail_start / BYTES_PER_SECOND) - self.position() - 8)
            try:
                await asyncio.wait_for(nitem.analysed.wait(), remaining)
            except TimeoutError:
                LOGGER.info("Analysis of %s not ready, using standard crossfade", nxt.track.title)
        tail = self._read(out_item, tail_start, out_item.size - tail_start)
        head_len = seconds_to_bytes(min(self.head_seconds(mode, std_dur), nitem.duration / 2))
        head = self._read(nitem, 0, head_len)
        assert self.build_transition is not None
        res = await self.build_transition(
            tail,
            head,
            out_item.analysis,
            nitem.analysis,
            mode,
            std_dur,
            logger=LOGGER,
        )
        return nitem, res

    # -- handover / interrupts ------------------------------------------------
    def _schedule_handover(
        self, cand: Candidate, item: PreparedTrack, started_at: float, at: float
    ) -> None:
        loop = asyncio.get_running_loop()
        delay = max(0.0, at - time.time())
        handle = loop.call_later(
            delay, lambda: asyncio.create_task(self._handover(cand, item, started_at))
        )
        self._handover_handles.append(handle)

    def _cancel_handovers(self) -> None:
        for h in self._handover_handles:
            h.cancel()
        self._handover_handles.clear()

    async def _handover(self, cand: Candidate, item: PreparedTrack, started_at: float) -> None:
        is_new = not (self.now and self.now.candidate is cand)
        if self.locked is cand:
            self.locked = None
        if is_new:
            self.now = NowPlaying(candidate=cand, prepared=item, started_at=started_at)
        else:
            assert self.now
            self.now.started_at = started_at
        t = cand.track
        self.output.set_metadata(
            t.title,
            t.artist,
            t.album,
            t.artwork_url,
            item.duration,
            max(0.0, time.time() - started_at),
            True,
        )
        if is_new:
            await self.hooks.on_track_started(self.now)
            self.schedule_prefetch()
        else:
            await self.hooks.on_engine_state()

    async def _interrupt(self, cand: Candidate, item: PreparedTrack) -> None:
        """Handle pause or skip while `cand` is the audible track."""
        await self.output.end_stream(drain=False)
        self._cancel_handovers()
        if self._skip.is_set():
            self._skip.clear()
            self.locked = None
            LOGGER.info("Skipping %s", cand.track.title)
            await self._finish_current(skipped=True)
            return  # main loop picks up the next track
        # pause: remember audible position and wait
        started = self.now.started_at if self.now else time.time()
        pos = seconds_to_bytes(max(0.0, time.time() - started))
        pos = min(pos, item.size)
        self.locked = None
        t = cand.track
        self.output.set_metadata(
            t.title, t.artist, t.album, t.artwork_url, item.duration, pos / BYTES_PER_SECOND, False
        )
        await self.hooks.on_engine_state()
        while self.paused:
            await self._wait_wake()
            if self._skip.is_set():
                self._skip.clear()
                await self._finish_current(skipped=True)
                return
        await self.output.begin_stream()
        await self._play_from(cand, item, pos, started_at=None)

    async def _finish_current(self, skipped: bool) -> None:
        self.now = None
        await self.hooks.on_engine_state()

    def status(self) -> dict[str, Any]:
        return {
            "paused": self.paused,
            "now_playing": self.now.candidate.track.title if self.now else None,
            "locked_next": self.locked.track.title if self.locked else None,
            "last_transition": self.last_transition,
            "last_error": self.last_error,
        }

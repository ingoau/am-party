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
import contextlib
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
# The transition must be rendered before the writer reaches the tail. RENDER_BUDGET is
# reserved for the render itself (decode/analysis waits end that much earlier) and
# BUILD_GRACE is how long the writer may eat into the output buffer (~3s) waiting for
# it before giving up and playing through gaplessly instead of underrunning.
RENDER_BUDGET = {"smart": 5.0, "standard": 2.0}
BUILD_GRACE = 1.5
POLL = 0.1


def _soft_limit(pcm: bytes, knee: float = 0.9) -> bytes:
    """Overlapping tracks in a crossfade can exceed full scale slightly.

    Samples below `knee` are untouched; above it they are smoothly compressed so the
    output never exceeds 1.0.
    """
    arr = np.frombuffer(pcm, dtype="<f4").astype(np.float32)
    if not arr.size or float(np.max(np.abs(arr))) <= knee:
        return pcm
    mag = np.abs(arr)
    over = mag > knee
    head = 1.0 - knee
    mag[over] = knee + head * np.tanh((mag[over] - knee) / head)
    return (np.sign(arr) * mag).astype("<f4").tobytes()


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


@dataclass
class _Resume:
    """Where the main loop continues after a pause or an interrupted transition."""

    cand: Candidate
    item: PreparedTrack
    pos: int  # byte offset to continue from


_INTERRUPTED = object()


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
        # skip that must re-pick the next track from the party (e.g. "previous" put a
        # track in front of one that was already locked in / being mixed in)
        self._requeue = False
        # pending seek target (seconds into `now`) while playing
        self._seek: float | None = None
        self._task: asyncio.Task | None = None
        self._prefetch_task: asyncio.Task | None = None
        self._handover_handles: list[asyncio.TimerHandle] = []
        self._bg: set[asyncio.Task] = set()  # handovers, transition renders
        # frozen position of `now` while paused (cleared when playback resumes)
        self._paused_pos: float | None = None
        self._stream_active = False
        # candidates already written to the output whose handover hasn't fired yet;
        # the party must not offer these again
        self.inflight: list[Candidate] = []

    # -- public control ------------------------------------------------------
    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="engine")

    async def stop(self) -> None:
        self._cancel_handovers()
        tasks = [t for t in (self._task, self._prefetch_task, *self._bg) if t and not t.done()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = self._prefetch_task = None

    def poke(self) -> None:
        """Queue/settings changed: wake the engine and refresh prefetching."""
        self._wake.set()
        self.schedule_prefetch()

    def skip(self, requeue: bool = False) -> None:
        """Skip the audible track. `requeue`: ask the party what's next instead of
        continuing with a track that was already locked in."""
        if requeue:
            self._requeue = True
        self._skip.set()
        self._wake.set()

    def seek(self, pos_s: float) -> bool:
        """Jump within the current track. Returns False if nothing is playing."""
        if not self.now:
            return False
        item = self.now.prepared
        pos_s = max(0.0, min(float(pos_s), max(0.0, item.duration - 1.0)))
        self._seek = pos_s
        if self.paused and self._paused_pos is not None:
            # resume() continues from the frozen position
            self._freeze(self.now.candidate, item, pos_s)
        else:
            # report the new position right away; the handover fixes the exact clock
            self.now.started_at = time.time() - pos_s
            self._wake.set()
        return True

    async def pause(self) -> None:
        if not self.paused:
            self._paused_pos = self.position()
            self.paused = True
            self._wake.set()

    async def resume(self) -> None:
        if self.paused:
            self.paused = False
            self._wake.set()

    def position(self) -> float:
        if not self.now:
            return 0.0
        if self._paused_pos is not None:
            return max(0.0, min(self._paused_pos, self.now.prepared.duration))
        return max(0.0, min(time.time() - self.now.started_at, self.now.prepared.duration))

    def _spawn(self, coro: Any, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._bg.add(task)
        task.add_done_callback(self._bg_done)
        return task

    def _bg_done(self, task: asyncio.Task) -> None:
        self._bg.discard(task)
        if not task.cancelled() and (err := task.exception()) is not None:
            LOGGER.error("Engine task %s failed", task.get_name(), exc_info=err)

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
        for c in self.inflight:
            keep.add(c.track.id)
        wanted: list[Candidate] = []
        if self.locked:
            wanted.append(self.locked)
        wanted += self.hooks.peek_upcoming(2)
        if self.locked:
            keep.add(self.locked.track.id)
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
                with contextlib.suppress(Exception):
                    await self._end(drain=False)
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
        self._requeue = False
        self._seek = None
        cand = self.hooks.peek_next()
        assert cand
        self.locked = cand
        item = await self._ensure_decoded(cand)
        if item is None or self._requeue:
            return
        # pause/resume and interrupted transitions come back here instead of recursing
        resume: _Resume | None = _Resume(cand, item, 0)
        while resume is not None:
            resume = await self._play_from(resume.cand, resume.item, resume.pos)

    async def _begin(self) -> None:
        self._stream_active = True
        await self.output.begin_stream()

    async def _end(self, drain: bool) -> None:
        self._stream_active = False
        await self.output.end_stream(drain=drain)

    async def _set_idle(self) -> None:
        if self.now is not None:
            self.now = None
            self._paused_pos = None
            await self.hooks.on_engine_state()

    async def _drain_and_idle(self, started_at: float | None, item: PreparedTrack) -> _Resume | None:
        """Stream ran out of tracks: let the last one finish audibly, then go idle.

        Returns where to continue if the last track was seeked while it finished.
        """
        was_active = self._stream_active
        await self._end(drain=True)
        self.locked = None
        if was_active and started_at is not None:
            remaining = started_at + item.duration - time.time()
            if remaining > 0:
                await self._wait_wake(min(remaining, 15))
        if self._seek is not None and self.now and not self.paused and not self._skip.is_set():
            seek, self._seek = self._seek, None
            await self._end(drain=False)
            return _Resume(self.now.candidate, self.now.prepared, seconds_to_bytes(seek))
        if not self.inflight:
            await self._set_idle()
        return None

    async def _ensure_decoded(self, cand: Candidate, timeout: float = SKIP_WAIT_DECODE) -> PreparedTrack | None:
        item = self.preparer.prepare(cand.track)
        end = time.monotonic() + timeout
        while not item.decoded.is_set() and (left := end - time.monotonic()) > 0:
            await self.preparer.wait_decoded(item, min(left, 0.25))
            if self._stream_active and (self.paused or self._skip.is_set()):
                # pause/skip pressed while the previous track's last buffered audio
                # plays and we wait for this one: stop that audio now
                await self._stop_audible()
            if self._requeue or (self._seek is not None and self._stream_active):
                # "previous" or a seek: the caller handles it without this track
                return None
        ok = item.ok
        if not ok:
            err = item.error or "timed out preparing track"
            LOGGER.warning("Skipping %s: %s", cand.track.title, err)
            self.last_error = f"{cand.track.title}: {err}"
            if self.locked is cand:
                self.locked = None
            await self.hooks.on_track_failed(cand, err)
            return None
        return item

    async def _stop_audible(self) -> None:
        """Cut the audible track (stream finished writing) for a pause or skip."""
        await self._end(drain=False)
        self._cancel_handovers()
        if self._skip.is_set():
            self._skip.clear()
            if self.now:
                LOGGER.info("Skipping %s", self.now.candidate.track.title)
            await self._finish_current()
        elif self.now:
            self._freeze(self.now.candidate, self.now.prepared, self.position())
            await self.hooks.on_engine_state()

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

    async def _write_chunks(
        self, pcm: bytes, on_first: Callable[[float], Awaitable[None]] | None = None
    ) -> tuple[float | None, int, bool]:
        """Write pcm in chunks. Returns (time first sample is heard, bytes written, interrupted)."""
        first: float | None = None
        step = seconds_to_bytes(CHUNK_SECONDS)
        written = 0
        for off in range(0, len(pcm), step):
            if self._interrupted():
                return first, written, True
            chunk = pcm[off : off + step]
            t = await self.output.write(chunk)
            written += len(chunk)
            if first is None:
                first = t
                if on_first:
                    await on_first(t)
        return first, written, False

    def _interrupted(self) -> bool:
        return self.paused or self._skip.is_set() or self._seek is not None

    def _is_current(self, cand: Candidate, started_at: float | None) -> bool:
        return started_at is not None or bool(self.now and self.now.candidate is cand)

    async def _play_from(self, cand: Candidate, item: PreparedTrack, pos: int) -> _Resume | None:
        """Play `item` from byte `pos`, chaining into following tracks.

        Returns where to continue after a pause / interrupted transition, or None when
        the stream went idle or the current track was skipped (the main loop then
        picks the next track).
        """
        started_at: float | None = None  # wall clock when `cand` position 0 is/was audible

        async def mark_started(t: float, at: int) -> None:
            nonlocal started_at
            if started_at is None:
                started_at = t - at / BYTES_PER_SECOND
                await self._handover(cand, item, started_at)

        while True:
            if not self._stream_active and not self._interrupted():
                await self._begin()
            mode, std_dur = self._mode()
            tail_len = 0
            if mode != "off" and self.build_transition is not None:
                tail_len = seconds_to_bytes(
                    min(self.tail_seconds(mode, std_dur), item.duration / 2)
                )
            # resuming inside the tail (e.g. paused mid-transition): play a little of
            # the outgoing track first so the transition can be re-rendered in time
            lead = seconds_to_bytes(RENDER_BUDGET.get(mode, 2.0)) if pos else 0
            tail_start = max(pos + lead, item.size - tail_len)
            if item.size - tail_start < seconds_to_bytes(MIN_TRANSITION):
                tail_start = item.size  # no (room for a) transition: play through
            lock_at = max(pos, tail_start - seconds_to_bytes(LOCK_AHEAD))
            build: asyncio.Task | None = None
            nxt: Candidate | None = None
            step = seconds_to_bytes(CHUNK_SECONDS)

            # body of the track
            while True:
                if self._interrupted():
                    if build:
                        build.cancel()
                    return await self._interrupt(cand, item, started_at, pos)
                if build is None and nxt is None and pos >= lock_at and self._is_current(cand, started_at):
                    nxt = self._lock_next()
                    if nxt and tail_start < item.size:
                        deadline = time.time() + (tail_start - pos) / BYTES_PER_SECOND
                        build = self._spawn(
                            self._build(item, tail_start, nxt, mode, std_dur, deadline), "transition"
                        )
                if pos >= tail_start:
                    break
                chunk = self._read(item, pos, min(step, tail_start - pos))
                if not chunk:
                    break
                t = await self.output.write(chunk)
                await mark_started(t, pos)
                pos += len(chunk)

            result: Any = None
            if build is not None:
                result = await self._await_build(build)
                if result is _INTERRUPTED:
                    return await self._interrupt(cand, item, started_at, pos)

            if result is None:
                # no transition: play the rest of this track, then the next from 0
                rest = self._read(item, pos, item.size - pos)
                if rest:
                    at = pos
                    _, written, interrupted = await self._write_chunks(
                        rest, lambda t: mark_started(t, at)
                    )
                    pos += written
                    if interrupted:
                        return await self._interrupt(cand, item, started_at, pos)
                if nxt is None:
                    nxt = self._lock_next()
                if nxt is None:
                    return await self._drain_and_idle(started_at, item)
                nitem = await self._ensure_decoded(nxt)
                if nitem is None and not self._requeue and self._seek is None:
                    # try whatever is next now
                    nxt = self._lock_next()
                    if nxt is not None:
                        nitem = await self._ensure_decoded(nxt)
                if self._seek is not None and not self._requeue:
                    # seek pressed while the last buffered audio plays: it is for `cand`
                    return await self._interrupt(cand, item, started_at, pos)
                if nitem is None and not self._requeue:
                    return await self._drain_and_idle(started_at, item)
                if self._requeue:
                    # "previous" changed what comes next while we waited for `nxt`
                    self.locked = None
                    return None
                self.last_transition = "gapless"
                cand, item, pos, started_at = nxt, nitem, 0, None
                continue

            # play rendered transition
            nitem, res = result
            self.last_transition = res.mode_used
            res.pcm = _soft_limit(res.pcm)
            LOGGER.info(
                "Transition %s -> %s: %s, %.1fs",
                item.track.title,
                nitem.track.title,
                res.mode_used,
                len(res.pcm) / BYTES_PER_SECOND,
            )
            scheduled: list[float] = []
            at = tail_start

            async def _on_first(t0: float, nxt=nxt, nitem=nitem, res=res, at=at) -> None:  # noqa: ANN001
                await mark_started(t0, at)
                scheduled.append(t0 + res.in_start_offset)
                self._schedule_handover(nxt, nitem, scheduled[0], t0 + res.handover_offset)

            t0, _, interrupted = await self._write_chunks(res.pcm, _on_first)
            if not scheduled:
                await _on_first(t0 if t0 is not None else time.time())
            next_started = scheduled[0]
            if interrupted:
                switched = bool(self.now and self.now.candidate is nxt)
                self._cancel_handovers()
                if switched:
                    # the incoming track is already "now playing": pause/skip it
                    return await self._interrupt(
                        nxt, nitem, next_started, seconds_to_bytes(res.in_position_at_end)
                    )
                if self._skip.is_set() and not self.paused and not self._requeue:
                    # skip pressed while the outgoing song was still "now playing":
                    # jump straight into the incoming song where it is right now
                    self._skip.clear()
                    await self._end(drain=False)
                    await self._handover(nxt, nitem, next_started)
                    npos = seconds_to_bytes(max(0.0, time.time() - next_started))
                    return _Resume(nxt, nitem, min(npos, nitem.size))
                # pause before the switch: the outgoing track stays current and resumes
                # from where it was heard; its transition is re-rendered on resume
                return await self._interrupt(cand, item, started_at, item.size)
            cand, item = nxt, nitem  # type: ignore[assignment]
            pos = seconds_to_bytes(res.in_position_at_end)
            started_at = next_started
            # handover callback (scheduled) updates now playing at the right moment

    async def _await_build(self, build: asyncio.Task) -> Any:
        """Wait for the transition render without blocking pause/skip or underrunning."""
        grace_end = time.monotonic() + BUILD_GRACE
        try:
            while not build.done():
                if self._interrupted():
                    build.cancel()
                    return _INTERRUPTED
                if time.monotonic() > grace_end:
                    LOGGER.warning("Transition not rendered in time, playing through")
                    build.cancel()
                    return None
                await asyncio.wait({build}, timeout=POLL)
        except asyncio.CancelledError:
            build.cancel()
            raise
        if build.cancelled():
            return None
        if (err := build.exception()) is not None:
            LOGGER.warning("Transition failed, playing through: %s", err)
            return None
        return build.result()

    def _lock_next(self) -> Candidate | None:
        cand = self.hooks.peek_next()
        self.locked = cand
        if cand:
            self.preparer.prepare(cand.track)
        self.schedule_prefetch()
        return cand

    async def _build(
        self,
        out_item: PreparedTrack,
        tail_start: int,
        nxt: Candidate,
        mode: str,
        std_dur: float,
        deadline: float,
    ) -> tuple[PreparedTrack, Any] | None:
        """Render the transition; `deadline` is when the writer reaches the tail."""
        budget = RENDER_BUDGET.get(mode, 2.0)
        nitem = self.preparer.prepare(nxt.track)
        if not await self.preparer.wait_decoded(nitem, max(0.5, deadline - budget - time.time())):
            LOGGER.warning("Next track not ready in time for a crossfade")
            return None
        if mode == "smart" and not nitem.analysed.is_set():
            remaining = max(0.0, deadline - budget - time.time())
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
        self.inflight.append(cand)
        handle = loop.call_later(
            delay, lambda: self._spawn(self._handover(cand, item, started_at), "handover")
        )
        self._handover_handles.append(handle)

    def _cancel_handovers(self) -> None:
        for h in self._handover_handles:
            h.cancel()
        self._handover_handles.clear()
        self.inflight.clear()

    async def _handover(self, cand: Candidate, item: PreparedTrack, started_at: float) -> None:
        is_new = not (self.now and self.now.candidate is cand)
        self.inflight = [c for c in self.inflight if c is not cand]
        if self.locked is cand:
            self.locked = None
        self._paused_pos = None
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

    def _freeze(self, cand: Candidate, item: PreparedTrack, pos_s: float) -> None:
        """Paused at `pos_s` of the current track: freeze position and metadata."""
        self._paused_pos = pos_s
        if self.now:
            # keep started_at consistent with the frozen position for clients
            self.now.started_at = time.time() - pos_s
        t = cand.track
        self.output.set_metadata(
            t.title, t.artist, t.album, t.artwork_url, item.duration, pos_s, False
        )

    async def _interrupt(
        self, cand: Candidate, item: PreparedTrack, started_at: float | None, pos: int
    ) -> _Resume | None:
        """Handle pause or skip while `cand` is being played.

        `started_at` is `cand`'s local clock (None if none of it has been heard since
        this play started) and `pos` the byte offset written so far (an upper bound
        for what was heard). Returns where to resume, or None after a skip.
        """
        current = bool(self.now and self.now.candidate is cand)
        seek, self._seek = self._seek, None
        if self.now and seek is not None and not self._skip.is_set():
            # seek always targets the audible track
            cand, item, current = self.now.candidate, self.now.prepared, True
            pos = seconds_to_bytes(seek)
            if self.paused:
                self._paused_pos = seek  # paused right after seeking: stay there
            else:
                await self._end(drain=False)
                self._cancel_handovers()
                self.locked = None
                return _Resume(cand, item, min(pos, item.size))
        elif current and self.paused and self._paused_pos is not None:
            # pause() snapshotted what was heard at the moment it was pressed
            pos = min(pos, seconds_to_bytes(self._paused_pos))
        elif started_at is not None:
            heard = seconds_to_bytes(max(0.0, time.time() - started_at))
            pos = min(pos, heard)
        pos = max(0, min(pos, item.size))
        await self._end(drain=False)
        self._cancel_handovers()
        self.locked = None
        if self._skip.is_set():
            self._skip.clear()
            if current:
                LOGGER.info("Skipping %s", cand.track.title)
                await self._finish_current()
                return None  # main loop picks up the next track
            # `cand` wasn't heard yet: the skip was meant for the audible track
            if self.now:
                LOGGER.info("Skipping %s", self.now.candidate.track.title)
            await self._finish_current()
            if self._requeue:
                return None
            return _Resume(cand, item, pos)
        # pause: remember the audible position and wait
        if not current:
            # paused at a track boundary before `cand` was heard: it becomes current
            await self._handover(cand, item, time.time() - pos / BYTES_PER_SECOND)
        self._freeze(cand, item, pos / BYTES_PER_SECOND)
        await self.hooks.on_engine_state()
        while self.paused:
            await self._wait_wake()
            if self._skip.is_set():
                self._skip.clear()
                LOGGER.info("Skipping %s", cand.track.title)
                await self._finish_current()
                return None
        if self._paused_pos is not None:
            pos = seconds_to_bytes(self._paused_pos)  # seek() while paused moves it
        self._seek = None
        return _Resume(cand, item, max(0, min(pos, item.size)))

    async def _finish_current(self) -> None:
        self.now = None
        self._paused_pos = None
        await self.hooks.on_engine_state()

    def status(self) -> dict[str, Any]:
        return {
            "paused": self.paused,
            "now_playing": self.now.candidate.track.title if self.now else None,
            "locked_next": self.locked.track.title if self.locked else None,
            "last_transition": self.last_transition,
            "last_error": self.last_error,
        }

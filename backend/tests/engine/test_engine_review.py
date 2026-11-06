"""Regression tests for engine / analysis-worker review findings."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from am_party.models import BYTES_PER_SECOND
from tests.engine.test_engine import FakeOutput, build, levels, media, wait_for  # noqa: F401


class PosOutput(FakeOutput):
    """~2x real time pacing, records metadata positions."""

    def __init__(self) -> None:
        super().__init__()
        self.positions: list[tuple[str, float, bool]] = []

    async def write(self, pcm: bytes) -> float:
        self.data += pcm
        await asyncio.sleep(len(pcm) / BYTES_PER_SECOND / 2)
        return time.time()

    def set_metadata(self, title, artist, album, artwork_url, duration_s, position_s, playing):  # noqa: ANN001
        self.positions.append((title, position_s, playing))


@pytest.mark.asyncio
async def test_pause_at_gapless_boundary_uses_incoming_clock(media) -> None:  # noqa: ANN001
    eng, party, _ = build(media, ["a", "b"], transition=None)
    out = PosOutput()
    eng.output = out
    orig = eng._ensure_decoded

    async def ensure(cand, timeout=60):  # noqa: ANN001, ANN202
        item = await orig(cand, timeout)
        if cand.track.id == "b":
            eng.paused = True  # pause pressed while a's last buffered audio plays
        return item

    eng._ensure_decoded = ensure  # type: ignore[method-assign]
    await eng.start()
    await wait_for(lambda: any(p[0] == "b" and not p[2] for p in out.positions), 30)
    paused_meta = [p for p in out.positions if p[0] == "b" and not p[2]][-1]
    assert eng.now is not None and eng.now.candidate.track.id == "b"
    assert eng.position() == pytest.approx(0.0, abs=0.01)
    await eng.stop()
    # b never played a sample, so its paused position is 0
    assert paused_meta[1] == pytest.approx(0.0, abs=0.5), paused_meta


@pytest.mark.asyncio
async def test_position_frozen_while_paused(media) -> None:  # noqa: ANN001
    eng, party, _ = build(media, ["a", "b"])
    out = PosOutput()
    eng.output = out
    await eng.start()
    await wait_for(lambda: party.started == ["a"])
    await asyncio.sleep(0.5)
    await eng.pause()
    await asyncio.sleep(0.2)
    p1 = eng.position()
    await asyncio.sleep(1.0)
    p2 = eng.position()
    # started_at is kept consistent with the frozen position
    assert eng.now is not None
    await eng.stop()
    assert p2 == pytest.approx(p1, abs=0.1), (p1, p2)


@pytest.mark.asyncio
async def test_pause_resume_does_not_grow_await_chain(media) -> None:  # noqa: ANN001
    eng, party, _ = build(media, ["a", "b"])
    out = PosOutput()
    eng.output = out
    await eng.start()
    await wait_for(lambda: party.started == ["a"])

    def depth() -> int:
        n, c = 0, eng._task.get_coro()  # type: ignore[union-attr]
        while c is not None and hasattr(c, "cr_await"):
            n += 1
            c = c.cr_await
        return n

    d0 = depth()
    for _ in range(20):
        await eng.pause()
        await asyncio.sleep(0.05)
        await eng.resume()
        await asyncio.sleep(0.05)
    d1 = depth()
    await eng.stop()
    assert d1 <= d0 + 2, (d0, d1)


class RealtimeOutput(FakeOutput):
    """Real-time output with a 1s buffer: write() returns when the chunk will be heard."""

    BUFFER = 1.0

    def __init__(self) -> None:
        super().__init__()
        self.base: float | None = None
        self.written = 0

    async def write(self, pcm: bytes) -> float:
        if self.base is None:
            self.base, self.written = time.time() + 0.2, 0
        heard = self.base + self.written / BYTES_PER_SECOND
        self.data += pcm
        self.written += len(pcm)
        await asyncio.sleep(max(0.0, heard - self.BUFFER - time.time()))
        return heard

    async def end_stream(self, drain: bool = True) -> None:
        await super().end_stream(drain)
        self.base = None


@pytest.mark.asyncio
async def test_pause_mid_transition_keeps_outgoing_track(media) -> None:  # noqa: ANN001
    """Pausing during a transition, before the switch, keeps the outgoing track current."""
    eng, party, _ = build(media, ["a", "b"])
    out = RealtimeOutput()
    eng.output = out
    seen = asyncio.Event()

    from tests.engine.test_engine import concat_transition

    async def transition(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        res = await concat_transition(*args, **kwargs)
        res.handover_offset += 5.0  # switch "late" so we can pause before it
        seen.set()
        return res

    eng.build_transition = transition
    await eng.start()
    await asyncio.wait_for(seen.wait(), 30)
    # wait until the transition audio is being written, then pause
    await wait_for(lambda: len(out.data) > 6.5 * BYTES_PER_SECOND, 30)
    await eng.pause()
    await asyncio.sleep(0.3)
    assert eng.now is not None and eng.now.candidate.track.id == "a"
    assert party.started == ["a"]
    paused_at = eng.position()
    assert 5.0 < paused_at <= 10.0, paused_at
    await eng.resume()
    await wait_for(lambda: party.started == ["a", "b"], 30)
    await eng.stop()
    assert levels(out.data)[-1] == 0.2


@pytest.mark.asyncio
async def test_skip_at_boundary_skips_audible_track_not_next(media) -> None:  # noqa: ANN001
    """Skip pressed while the next track is still being decoded skips the audible one."""
    eng, party, out = build(media, ["a", "b", "c"], transition=None)
    orig = eng._ensure_decoded

    async def ensure(cand, timeout=60):  # noqa: ANN001, ANN202
        if cand.track.id == "b":
            eng.skip()  # user skips "a" while b is still being prepared
        return await orig(cand, timeout)

    eng._ensure_decoded = ensure  # type: ignore[method-assign]
    await eng.start()
    await wait_for(lambda: party.started == ["a", "b", "c"] and eng.now is None, 30)
    await eng.stop()
    assert "b" in party.started  # b was played, not skipped unheard
    assert 0.2 in levels(out.data)


@pytest.mark.asyncio
async def test_stop_awaits_tasks(media) -> None:  # noqa: ANN001
    eng, party, _ = build(media, ["a", "b"])
    eng.output = PosOutput()
    await eng.start()
    await wait_for(lambda: party.started == ["a"])
    task = eng._task
    await eng.stop()
    assert task is not None and task.done()
    assert not eng._bg


def _fake_worker():  # noqa: ANN202
    from am_party.smartfades.worker import AnalysisWorker

    w = AnalysisWorker()
    w._started = True
    w.available = True
    replies: asyncio.Queue[bytes] = asyncio.Queue()
    spawned: list[int] = []

    class Stdin:
        def write(self, data: bytes) -> None:
            req = json.loads(data)

            async def answer() -> None:
                await asyncio.sleep(0.3)  # child takes a while
                replies.put_nowait((json.dumps({"id": req["id"], "result": {"bpm": req["id"]}}) + "\n").encode())

            asyncio.get_running_loop().create_task(answer())

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

    class Stdout:
        async def readline(self) -> bytes:
            return await replies.get()

    class Proc:
        stdin = Stdin()
        stdout = Stdout()
        returncode = None

        async def wait(self) -> int:
            return 0

        def kill(self) -> None:
            pass

    w._proc = Proc()  # type: ignore[assignment]

    async def ensure() -> bool:
        if w._proc is None:
            spawned.append(1)
            w._proc = Proc()  # type: ignore[assignment]
        return True

    w._ensure_child = ensure  # type: ignore[method-assign]
    return w, spawned


@pytest.mark.asyncio
async def test_cancelled_analysis_does_not_poison_next_request() -> None:
    w, spawned = _fake_worker()
    t = asyncio.create_task(w.analyze_file("/x.f32"))
    await asyncio.sleep(0.1)
    t.cancel()  # TrackPreparer.retain_only() cancels in-flight analysis
    with pytest.raises(asyncio.CancelledError):
        await t
    res = await w.analyze_file("/y.f32")
    assert res == {"bpm": 2}, res
    assert not spawned  # stale reply drained; models were not reloaded

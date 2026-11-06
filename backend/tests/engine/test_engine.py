"""Engine tests using real ffmpeg decoding of local files and a fake output."""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from am_party.models import BYTES_PER_SECOND, StreamInfo, Track
from am_party.player.engine import Candidate, Engine
from am_party.player.prepare import TrackPreparer

LEVELS = {"a": 0.1, "b": 0.2, "c": 0.3, "d": 0.4}
SECONDS = 10


def make_wav(path: Path, level: float) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
            "-i", f"aevalsrc={level}|{level}:s=48000:d={SECONDS}",
            "-c:a", "pcm_f32le", str(path),
        ],
        check=True,
    )  # fmt: skip


class FakeOutput:
    def __init__(self) -> None:
        self.data = bytearray()
        self.meta: list[tuple[str, bool]] = []
        self.streams = 0
        self.ends: list[bool] = []

    async def begin_stream(self) -> None:
        self.streams += 1

    async def write(self, pcm: bytes) -> float:
        self.data += pcm
        await asyncio.sleep(0)
        return time.time()

    async def end_stream(self, drain: bool = True) -> None:
        self.ends.append(drain)

    def set_metadata(self, title, artist, album, artwork_url, duration_s, position_s, playing):  # noqa: ANN001
        self.meta.append((title, playing))


class FakeParty:
    def __init__(self, ids: list[str]) -> None:
        self.queue = [Candidate(Track(id=i, title=i, artist="x"), n, "admin") for n, i in enumerate(ids)]
        self.started: list[str] = []
        self.failed: list[str] = []

    engine = None

    def _avail(self):  # noqa: ANN202
        infl = self.engine.inflight if self.engine else []
        return [c for c in self.queue if all(c is not i for i in infl)]

    def peek_next(self):  # noqa: ANN201
        a = self._avail()
        return a[0] if a else None

    def peek_upcoming(self, n: int):  # noqa: ANN201
        return self._avail()[:n]

    async def on_track_started(self, now) -> None:  # noqa: ANN001
        self.started.append(now.candidate.track.id)
        self.queue = [c for c in self.queue if c is not now.candidate]

    async def on_track_failed(self, cand, error) -> None:  # noqa: ANN001
        self.failed.append(cand.track.id)
        self.queue = [c for c in self.queue if c is not cand]

    async def on_engine_state(self) -> None:
        pass

    def settings(self):  # noqa: ANN201
        return {"crossfade_mode": "standard", "crossfade_duration": 4}


class Result:
    def __init__(self, pcm: bytes, tail: int, head: int) -> None:
        self.pcm = pcm
        self.handover_offset = tail / BYTES_PER_SECOND
        self.in_start_offset = tail / BYTES_PER_SECOND
        self.in_position_at_end = head / BYTES_PER_SECOND
        self.mode_used = "cut"


async def concat_transition(tail, head, a1, a2, mode, dur, logger=None):  # noqa: ANN001, ANN201
    return Result(tail + head, len(tail), len(head))


@pytest.fixture
def media(tmp_path: Path) -> Path:
    for name, level in LEVELS.items():
        make_wav(tmp_path / f"{name}.wav", level)
    return tmp_path


def build(media: Path, ids: list[str], transition=concat_transition):  # noqa: ANN001, ANN201
    async def resolve(track_id: str) -> StreamInfo:
        if track_id == "bad":
            raise RuntimeError("nope")
        return StreamInfo(url=str(media / f"{track_id}.wav"))

    async def no_analysis(_: str):  # noqa: ANN202
        return None

    async def save(_: str, __: dict) -> None:
        pass

    preparer = TrackPreparer(media / "cache", resolve, None, no_analysis, save, lambda: False, normalize=False)
    party = FakeParty(ids)
    out = FakeOutput()
    eng = Engine(out, preparer, party, transition, lambda m, d: d, lambda m, d: d)
    party.engine = eng
    return eng, party, out


async def wait_for(cond, timeout: float = 45) -> None:  # noqa: ANN001
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met")


def levels(data: bytes) -> list[float]:
    arr = np.frombuffer(bytes(data), dtype="<f4")
    # run-length of levels
    out: list[float] = []
    for v in arr[::4800]:
        r = round(float(v), 3)
        if not out or out[-1] != r:
            out.append(r)
    return out


@pytest.mark.asyncio
async def test_gapless_chain(media: Path) -> None:
    eng, party, out = build(media, ["a", "b", "c"])
    await eng.start()
    await wait_for(lambda: party.started == ["a", "b", "c"] and eng.now is None)
    await eng.stop()
    expected = 3 * SECONDS * BYTES_PER_SECOND
    assert len(out.data) == expected, (len(out.data), expected)
    assert levels(out.data) == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_failed_track_is_skipped(media: Path) -> None:
    eng, party, out = build(media, ["a", "bad", "c"])
    await eng.start()
    await wait_for(lambda: "c" in party.started and eng.now is None)
    await eng.stop()
    assert party.failed == ["bad"]
    assert levels(out.data) == [0.1, 0.3]


@pytest.mark.asyncio
async def test_no_transition_mode(media: Path) -> None:
    eng, party, out = build(media, ["a", "b"], transition=None)
    await eng.start()
    await wait_for(lambda: party.started == ["a", "b"] and eng.now is None)
    await eng.stop()
    assert len(out.data) == 2 * SECONDS * BYTES_PER_SECOND
    assert levels(out.data) == [0.1, 0.2]


class SlowOutput(FakeOutput):
    """Real-time-ish pacing so skip/pause land mid-track."""

    async def write(self, pcm: bytes) -> float:
        self.data += pcm
        await asyncio.sleep(len(pcm) / BYTES_PER_SECOND / 20)  # 20x real time
        return time.time()


@pytest.mark.asyncio
async def test_skip_and_pause(media: Path) -> None:
    eng, party, _ = build(media, ["a", "b", "c", "d"])
    out = SlowOutput()
    eng.output = out
    await eng.start()
    await wait_for(lambda: party.started == ["a"])
    await asyncio.sleep(0.1)
    eng.skip()
    await wait_for(lambda: party.started == ["a", "b"])
    await eng.pause()
    await asyncio.sleep(0.3)
    paused_len = len(out.data)
    await asyncio.sleep(0.3)
    assert len(out.data) == paused_len
    assert False in out.ends  # immediate stop on pause
    await eng.resume()
    await wait_for(lambda: party.started == ["a", "b", "c", "d"] and eng.now is None, 30)
    await eng.stop()
    lv = levels(out.data)
    assert lv[0] == 0.1 and lv[-1] == 0.4
    assert 0.2 in lv and 0.3 in lv

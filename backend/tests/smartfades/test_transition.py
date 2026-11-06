"""am-party-specific tests for the smart fades public API (build_transition / AnalysisWorker)."""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
import subprocess

import numpy as np
import pytest

from am_party.models import BYTES_PER_SECOND, SAMPLE_RATE, seconds_to_bytes
from am_party.smartfades import (
    TAIL_SECONDS_SMART,
    AnalysisWorker,
    TransitionResult,
    build_transition,
    head_seconds,
    tail_seconds,
)

from .conftest import _analysis_with_bands


def _has_rubberband() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, check=False
    ).stdout
    return " rubberband " in out


needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
needs_rubberband = pytest.mark.skipif(not _has_rubberband(), reason="ffmpeg lacks rubberband")
needs_torch = pytest.mark.skipif(
    importlib.util.find_spec("torch") is None, reason="torch not installed"
)


def _track(seconds: float, freq: float, bpm: float) -> bytes:
    """A click+tone stereo f32le track."""
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    period = 60.0 / bpm
    click = np.sin(2 * np.pi * 60 * t) * np.exp(-12 * np.mod(t, period))
    mono = (0.5 * click + 0.15 * np.sin(2 * np.pi * freq * t)).astype("<f4")
    return np.repeat(mono, 2).tobytes()


def _run(coro):  # noqa: ANN001, ANN202
    return asyncio.run(coro)


def _check_gapless(res: TransitionResult, in_head: bytes) -> None:
    """The last 100 ms of the transition must be the last 100 ms of the incoming head."""
    n = seconds_to_bytes(0.1)
    a = np.frombuffer(res.pcm[-n:], "<f4")
    b = np.frombuffer(in_head[-n:], "<f4")
    assert np.max(np.abs(a - b)) < 1e-3
    assert res.in_position_at_end == pytest.approx(len(in_head) / BYTES_PER_SECOND)
    assert res.in_start_offset == pytest.approx(
        len(res.pcm) / BYTES_PER_SECOND - len(in_head) / BYTES_PER_SECOND
    )


def test_window_sizes() -> None:
    assert TAIL_SECONDS_SMART == 45.0
    assert tail_seconds("smart", 8) == 45.0
    assert head_seconds("smart", 8) == 45.0
    assert tail_seconds("standard", 8) == 8.0
    assert head_seconds("standard", 6) == 6.0
    assert tail_seconds("off", 8) == 0.0
    assert head_seconds("off", 8) == 0.0


def test_off_is_a_cut() -> None:
    out_tail, in_head = _track(5, 220, 120), _track(5, 330, 124)
    res = _run(build_transition(out_tail, in_head, None, None, "off", 8))
    assert res.mode_used == "cut"
    assert res.pcm == out_tail + in_head
    assert res.in_start_offset == pytest.approx(5.0)
    assert res.handover_offset == pytest.approx(5.0)


def test_too_short_is_a_cut() -> None:
    out_tail, in_head = _track(2, 220, 120), _track(6, 330, 124)
    res = _run(build_transition(out_tail, in_head, None, None, "standard", 8))
    assert res.mode_used == "cut"
    assert len(res.pcm) == len(out_tail) + len(in_head)


@needs_ffmpeg
def test_standard_crossfade() -> None:
    out_tail, in_head = _track(8, 220, 120), _track(8, 330, 124)
    res = _run(build_transition(out_tail, in_head, None, None, "standard", 8))
    assert res.mode_used == "standard"
    assert 7.5 <= len(res.pcm) / BYTES_PER_SECOND <= 8.5
    _check_gapless(res, in_head)
    assert 0.0 < res.handover_offset < len(res.pcm) / BYTES_PER_SECOND


@needs_ffmpeg
def test_standard_with_shorter_head() -> None:
    # engine could only supply 5 s of the incoming track (short track) for an 8 s fade
    out_tail, in_head = _track(8, 220, 120), _track(5, 330, 124)
    res = _run(build_transition(out_tail, in_head, None, None, "standard", 8))
    assert res.mode_used == "standard"
    assert res.in_position_at_end == pytest.approx(5.0)
    # 4 s fade into a 5 s head leaves a pass-through part to check gaplessness on
    res = _run(build_transition(out_tail, in_head, None, None, "standard", 4))
    assert res.mode_used == "standard"
    _check_gapless(res, in_head)


@needs_ffmpeg
def test_smart_without_analysis_falls_back_to_standard() -> None:
    out_tail, in_head = _track(45, 220, 120), _track(45, 330, 124)
    res = _run(build_transition(out_tail, in_head, None, None, "smart", 8))
    assert res.mode_used == "standard"
    _check_gapless(res, in_head)


@needs_ffmpeg
def test_smart_with_garbage_analysis_falls_back() -> None:
    out_tail, in_head = _track(45, 220, 120), _track(45, 330, 124)
    res = _run(build_transition(out_tail, in_head, {"bpm": "x"}, {"nope": 1}, "smart", 8))
    assert res.mode_used in ("standard", "cut")
    assert len(res.pcm) > 0


@needs_rubberband
def test_smart_crossfade() -> None:
    duration = 120.0
    out_a = _analysis_with_bands(0.5, 0.4, 0.4, 0.3, duration=duration).to_dict()
    in_a = _analysis_with_bands(0.5, 0.4, 0.4, 0.3, duration=duration).to_dict()
    out_track, in_track = _track(duration, 220, 120), _track(duration, 330, 120)
    out_tail = out_track[-seconds_to_bytes(45) :]
    in_head = in_track[: seconds_to_bytes(45)]
    res = _run(build_transition(out_tail, in_head, out_a, in_a, "smart", 8))
    assert res.mode_used == "smart"
    samples = np.frombuffer(res.pcm, "<f4")
    assert np.isfinite(samples).all()
    assert np.abs(samples).max() < 2.0
    assert 45.0 < len(res.pcm) / BYTES_PER_SECOND < 90.0
    _check_gapless(res, in_head)
    assert 0.0 <= res.handover_offset <= len(res.pcm) / BYTES_PER_SECOND


def test_worker_unavailable_when_models_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken analysis stack leaves available False and analyze_file returning None."""
    monkeypatch.setenv("AM_PARTY_BEAT_THIS_CHECKPOINT", "/nonexistent/checkpoint.ckpt")

    async def go() -> tuple[bool, str | None, dict | None]:
        worker = AnalysisWorker(startup_timeout=120)
        await worker.start()
        try:
            return worker.available, worker.error, await worker.analyze_file("/nonexistent")
        finally:
            await worker.stop()

    available, error, result = _run(go())
    assert available is False
    assert error
    assert result is None


@needs_torch
def test_worker_analyzes_and_unloads(tmp_path) -> None:  # noqa: ANN001
    pcm = tmp_path / "t.pcm"
    pcm.write_bytes(_track(40, 220, 120))

    async def go() -> tuple[dict | None, bool]:
        worker = AnalysisWorker(idle_timeout=2)
        await worker.start()
        try:
            result = await worker.analyze_file(str(pcm))
            await asyncio.sleep(4)
            unloaded = worker._proc is None  # noqa: SLF001
            # transparently respawns after an idle unload
            again = await worker.analyze_file(str(pcm))
            assert again is not None
            return result, unloaded
        finally:
            await worker.stop()

    result, unloaded = _run(go())
    assert result is not None
    assert result["bpm"] == pytest.approx(120.0, abs=1.5)
    assert len(result["beats"]) > 40
    assert unloaded

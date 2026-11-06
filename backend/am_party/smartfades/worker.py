"""
Out-of-process smart-fades analysis worker.

The parent side (``AnalysisWorker``) lives in the server's asyncio loop and never
imports torch. It spawns ``python -m am_party.smartfades.worker`` which loads the
Beat This / S-KEY / FireRed models once and answers JSON-line requests over a
private pipe. The child is stopped after ``idle_timeout`` seconds without work
(the only reliable way to hand torch's memory back to the OS) and respawned on
the next request.

Protocol (one JSON object per line):
  child -> parent  {"ready": true}  |  {"ready": false, "error": "..."}
  parent -> child  {"id": 1, "cmd": "analyze", "path": "/x.pcm"}
  child -> parent  {"id": 1, "result": {...} | null, "error": null | "...", "seconds": 1.2}
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

# backend/ directory (parent of the ``am_party`` package) for the child's sys.path
_BACKEND_DIR = str(Path(__file__).resolve().parents[2])

DEFAULT_IDLE_TIMEOUT = 300.0  # seconds without analysis before the models are unloaded
DEFAULT_STARTUP_TIMEOUT = 180.0  # importing torch + loading models on a slow box
DEFAULT_ANALYZE_TIMEOUT = 900.0  # one track (MA caps analysis at 30 min of audio)
_STREAM_LIMIT = 64 * 1024 * 1024  # a result line carries ~4 x 1800-bin series + beats


class AnalysisWorker:
    """Runs full-track analysis in a separate process (torch must not block the asyncio loop; keep one model instance warm, unload after idle)."""

    def __init__(
        self,
        logger: logging.Logger | None = None,
        *,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
        analyze_timeout: float = DEFAULT_ANALYZE_TIMEOUT,
        num_threads: int | None = None,
        nice: int = 10,
    ) -> None:
        """
        Initialize the worker handle (nothing is spawned until ``start``).

        :param logger: Logger for worker diagnostics.
        :param idle_timeout: Seconds without requests before the child (and its models) exits.
        :param startup_timeout: Max seconds for the child to import torch and load models.
        :param analyze_timeout: Max seconds for one track's analysis.
        :param num_threads: torch intra-op threads in the child (default: min(4, cores/2)).
        :param nice: Niceness increment applied to the child process.
        """
        self.logger = logger or logging.getLogger("am_party.smartfades.worker")
        self.idle_timeout = idle_timeout
        self.startup_timeout = startup_timeout
        self.analyze_timeout = analyze_timeout
        self.num_threads = num_threads
        self.nice = nice
        self.available: bool = False
        self.error: str | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._next_id = 0
        self._started = False
        self._last_used = time.monotonic()

    async def start(self) -> None:
        """Spawn the child, load the models and set ``available``/``error``."""
        self._started = True
        async with self._lock:
            await self._ensure_child()
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_monitor())

    async def stop(self) -> None:
        """Stop the child process and the idle monitor."""
        self._started = False
        if self._idle_task is not None:
            self._idle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._idle_task
            self._idle_task = None
        async with self._lock:
            await self._kill_child()

    async def analyze_file(self, pcm_path: str) -> dict | None:
        """
        Analyse a whole track stored as raw f32le/48 kHz/stereo PCM.

        :param pcm_path: Path to the raw PCM file.
        :return: JSON-serialisable AudioAnalysisData dict, or None on failure.
        """
        if not self._started:
            await self.start()
        if not self.available:
            return None
        async with self._lock:
            self._last_used = time.monotonic()
            try:
                if not await self._ensure_child():
                    return None
                self._next_id += 1
                req_id = self._next_id
                reply = await asyncio.wait_for(
                    self._request({"id": req_id, "cmd": "analyze", "path": str(pcm_path)}),
                    timeout=self.analyze_timeout,
                )
            except asyncio.CancelledError:
                # The child keeps working on this request; its reply is skipped by the
                # next _request (ids only grow), so the warm models stay loaded.
                raise
            except TimeoutError:
                self.logger.warning("Smart fades analysis of %s timed out", pcm_path)
                await self._kill_child()
                return None
            except Exception as err:
                self.logger.warning("Smart fades analysis of %s failed: %s", pcm_path, err)
                await self._kill_child()
                return None
            finally:
                self._last_used = time.monotonic()
            if reply is None or reply.get("id") != req_id:
                self.logger.warning("Smart fades worker gave an unexpected reply: %r", reply)
                await self._kill_child()
                return None
            if reply.get("error"):
                self.logger.info("Smart fades analysis of %s failed: %s", pcm_path, reply["error"])
            else:
                self.logger.debug(
                    "Analysed %s in %.1fs", pcm_path, float(reply.get("seconds") or 0.0)
                )
            result = reply.get("result")
            return result if isinstance(result, dict) else None

    # -- internals ---------------------------------------------------------------------

    async def _ensure_child(self) -> bool:
        """Make sure a ready child is running (caller holds the lock)."""
        if self._proc is not None and self._proc.returncode is None:
            return True
        await self._kill_child()
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (_BACKEND_DIR, env.get("PYTHONPATH", "")) if p
        )
        env.setdefault("PYTHONUNBUFFERED", "1")
        if self.num_threads:
            env["AM_PARTY_SMARTFADES_THREADS"] = str(self.num_threads)
        env["AM_PARTY_SMARTFADES_NICE"] = str(self.nice)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "am_party.smartfades.worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=_STREAM_LIMIT,
            )
        except Exception as err:
            self.available = False
            self.error = f"could not spawn analysis worker: {err}"
            self.logger.warning("Smart fades analysis unavailable: %s", self.error)
            return False
        self._stderr_task = asyncio.create_task(self._forward_stderr(self._proc))
        started = time.monotonic()
        try:
            hello = await asyncio.wait_for(self._read_message(), timeout=self.startup_timeout)
        except Exception as err:
            hello = {"ready": False, "error": f"worker did not start: {err!r}"}
        if not hello or not hello.get("ready"):
            self.available = False
            self.error = (hello or {}).get("error") or "worker exited during startup"
            self.logger.warning("Smart fades analysis unavailable: %s", self.error)
            await self._kill_child()
            return False
        self.available = True
        self.error = None
        self._last_used = time.monotonic()
        self.logger.info(
            "Smart fades analysis worker ready (pid %s, %.1fs)",
            self._proc.pid,
            time.monotonic() - started,
        )
        return True

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        proc = self._proc
        assert proc is not None and proc.stdin is not None
        proc.stdin.write((json.dumps(payload) + "\n").encode())
        await proc.stdin.drain()
        while True:
            reply = await self._read_message()
            # A cancelled earlier request (e.g. the track left the queue) still gets
            # answered by the child; drop those stale replies instead of returning one
            # as ours. Replies arrive in request order, so anything older is stale.
            if reply is not None and "id" not in reply and "ready" in reply:
                continue  # hello of a child whose startup wait was cancelled
            rid = reply.get("id") if reply is not None else None
            if isinstance(rid, int) and rid < payload["id"]:
                self.logger.debug("Dropping stale analysis reply %s", rid)
                continue
            return reply

    async def _read_message(self) -> dict[str, Any] | None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        line = await proc.stdout.readline()
        if not line:
            return None
        return json.loads(line)

    async def _forward_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        with contextlib.suppress(Exception):
            async for raw in proc.stderr:
                line = raw.decode(errors="replace").rstrip()
                if line:
                    self.logger.debug("[analysis worker] %s", line)

    async def _kill_child(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.returncode is None:
            with contextlib.suppress(Exception):
                if proc.stdin is not None:
                    proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        if self._stderr_task is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(self._stderr_task, timeout=2)
            self._stderr_task = None

    async def _idle_monitor(self) -> None:
        interval = max(1.0, min(30.0, self.idle_timeout / 4))
        while True:
            await asyncio.sleep(interval)
            if self._proc is None or self._lock.locked():
                continue
            if time.monotonic() - self._last_used < self.idle_timeout:
                continue
            async with self._lock:
                if self._proc is not None:
                    self.logger.debug("Unloading idle smart fades models")
                    await self._kill_child()


# -- child process ---------------------------------------------------------------------


def _child_main() -> int:  # pragma: no cover - exercised via AnalysisWorker
    # Keep the protocol pipe private: anything a library prints goes to stderr.
    proto_fd = os.dup(1)
    os.dup2(2, 1)
    proto = os.fdopen(proto_fd, "w", buffering=1)
    sys.stdout = sys.stderr

    def send(obj: dict[str, Any]) -> None:
        proto.write(json.dumps(obj) + "\n")
        proto.flush()

    logging.basicConfig(
        level=os.environ.get("AM_PARTY_SMARTFADES_LOGLEVEL", "INFO"),
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("am_party.smartfades.analysis")
    with contextlib.suppress(OSError, ValueError):
        os.nice(int(os.environ.get("AM_PARTY_SMARTFADES_NICE") or 0))
    try:
        from am_party.smartfades.analysis.analyzer import SmartFadesAnalyzer  # noqa: PLC0415
        from am_party.smartfades.pcm import am_party_pcm_format  # noqa: PLC0415

        threads = int(os.environ.get("AM_PARTY_SMARTFADES_THREADS") or 0) or None
        SmartFadesAnalyzer.configure_runtime(threads)
        analyzer = SmartFadesAnalyzer(logger)
        analyzer.load_models()
        pcm_format = am_party_pcm_format()
    except BaseException as err:  # noqa: BLE001
        send({"ready": False, "error": f"{type(err).__name__}: {err}"})
        return 1
    send({"ready": True})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        req_id = req.get("id")
        if req.get("cmd") != "analyze":
            send({"id": req_id, "result": None, "error": "unknown command"})
            continue
        started = time.perf_counter()
        try:
            result = asyncio.run(analyzer.analyze_pcm_file(req["path"], pcm_format))
            send(
                {
                    "id": req_id,
                    "result": result.to_dict() if result is not None else None,
                    "error": None if result is not None else "no analysis result",
                    "seconds": time.perf_counter() - started,
                }
            )
        except Exception as err:  # noqa: BLE001
            logger.debug("analysis failed", exc_info=True)
            send(
                {
                    "id": req_id,
                    "result": None,
                    "error": f"{type(err).__name__}: {err}",
                    "seconds": time.perf_counter() - started,
                }
            )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_child_main())

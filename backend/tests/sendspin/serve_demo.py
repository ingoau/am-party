"""Demo/test server: SendspinOutput behind an aiohttp app, streaming a sine+click tone.

  python -m tests.sendspin.serve_demo [--port 8080] [--static DIR] [--seconds N]

Serves GET /sendspin (Sendspin websocket via SendspinOutput.ws_proxy) and, with
--static, the files in DIR at / (used by the headless-browser test page).
Prints one JSON line per event to stdout (listener count, write timings).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from am_party.models import BYTES_PER_SECOND, SAMPLE_RATE
from am_party.player.sendspin_out import SendspinOutput

from .harness import sine_pcm


def log(**kw: object) -> None:
    print(json.dumps({"t": round(time.time(), 3), **kw}), flush=True)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--static", type=Path)
    ap.add_argument("--seconds", type=float, default=3600)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)

    session = aiohttp.ClientSession()
    out = SendspinOutput(session, name="am-party Demo", port=0)
    out.on_listeners_changed = lambda n: log(event="listeners", count=n)
    await out.start()

    app = web.Application()
    app.router.add_get("/sendspin", out.ws_proxy)
    if args.static:
        app.router.add_get("/", lambda _r: web.FileResponse(args.static / "index.html"))
        app.router.add_static("/", args.static)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", args.port).start()
    log(event="ready", port=args.port)

    out.set_metadata("Sine 440", "am-party", "Tests", None, args.seconds, 0.0, True)
    chunk_s = 0.25
    frame = 0
    while frame < args.seconds * SAMPLE_RATE:
        pcm = sine_pcm(chunk_s, start_frame=frame)
        call = time.time()
        play = await out.write(pcm)
        if frame % SAMPLE_RATE == 0:
            log(event="write", ahead=round(play - call, 3), dur=len(pcm) / BYTES_PER_SECOND)
        frame += int(chunk_s * SAMPLE_RATE)
    await out.end_stream(drain=True)
    await out.stop()
    await runner.cleanup()
    await session.close()


if __name__ == "__main__":
    asyncio.run(main())

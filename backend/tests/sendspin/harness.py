"""Shared test harness: SendspinOutput inside an aiohttp app + Python reference clients.

Run inside the test container (see README in this folder / Dockerfile).
"""

from __future__ import annotations

import asyncio
import math
import socket
import struct
import time
from dataclasses import dataclass, field

import aiohttp
from aiohttp import web

from aiosendspin.client import SendspinClient as RefClient
from aiosendspin.models.core import DeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, Roles
from aiosendspin.noise.keys import Identity
from aiosendspin.noise.trust_store import InMemoryClientPairingStore

from am_party.models import BYTES_PER_SECOND, SAMPLE_RATE
from am_party.player.sendspin_out import SendspinOutput


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sine_pcm(seconds: float, freq: float = 440.0, start_frame: int = 0) -> bytes:
    """f32le stereo sine with a loud click at every whole second (for ear tests)."""
    n = int(seconds * SAMPLE_RATE)
    out = bytearray()
    for i in range(start_frame, start_frame + n):
        v = 0.2 * math.sin(2 * math.pi * freq * i / SAMPLE_RATE)
        if i % SAMPLE_RATE < 480:
            v = 0.9
        out += struct.pack("<ff", v, v)
    return bytes(out)


@dataclass
class ClientRecord:
    name: str
    client: RefClient
    chunks: list[tuple[int, int]] = field(default_factory=list)  # (server_ts_us, nbytes)
    stream_starts: int = 0
    stream_ends: int = 0
    stream_clears: int = 0
    metadata: list[object] = field(default_factory=list)
    groups: list[object] = field(default_factory=list)


async def make_client(
    name: str,
    url: str,
    session: aiohttp.ClientSession,
    codec: AudioCodec = AudioCodec.PCM,
    identity: Identity | None = None,
) -> ClientRecord:
    store = InMemoryClientPairingStore()
    client = RefClient(
        identity or Identity.generate(),
        name,
        [Roles.PLAYER, Roles.METADATA, Roles.CONTROLLER],
        pairing_store=store,
        device_info=DeviceInfo(product_name="am_party-test", manufacturer="am_party"),
        player_support=ClientHelloPlayerSupport(
            supported_formats=[
                SupportedAudioFormat(codec=c, channels=2, sample_rate=48000, bit_depth=16)
                for c in dict.fromkeys([codec, AudioCodec.PCM])  # preferred first; PCM is mandatory
            ],
            buffer_capacity=4_000_000,
        ),
        session=session,
    )
    await client.set_unpaired_access(enabled=True)
    rec = ClientRecord(name, client)
    client.add_audio_chunk_listener(
        lambda ts, data, fmt, *_: rec.chunks.append((ts, len(data)))
    )
    client.add_stream_start_listener(lambda _m: setattr(rec, "stream_starts", rec.stream_starts + 1))
    client.add_stream_end_listener(lambda _r: setattr(rec, "stream_ends", rec.stream_ends + 1))
    client.add_stream_clear_listener(
        lambda _r: setattr(rec, "stream_clears", rec.stream_clears + 1)
    )
    client.add_metadata_listener(lambda m: rec.metadata.append(m))
    client.add_group_update_listener(lambda g: rec.groups.append(g))
    await client.connect(url)
    return rec


class Harness:
    def __init__(self, buffer_ahead: float = 3.0) -> None:
        self.buffer_ahead = buffer_ahead
        self.port = free_port()
        self.url = f"ws://127.0.0.1:{self.port}/sendspin"
        self.counts: list[int] = []
        self.play_times: list[tuple[float, float, float]] = []  # (call_wall, returned, dur)

    async def __aenter__(self) -> Harness:
        self.session = aiohttp.ClientSession()
        self.out = SendspinOutput(self.session, name="am-party Test", port=0)
        self.out.buffer_ahead = self.buffer_ahead
        self.out.on_listeners_changed = self.counts.append
        await self.out.start()
        app = web.Application()
        app.router.add_get("/sendspin", self.out.ws_proxy)

        async def with_bitrate(request: web.Request) -> web.StreamResponse:
            return await self.out.ws_proxy(request, opus_kbps=int(request.match_info["kbps"]))

        app.router.add_get(r"/sendspin/{kbps:\d+}/sendspin", with_bitrate)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", self.port)
        await self.site.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.out.stop()
        await self.runner.cleanup()
        await self.session.close()

    async def produce(self, seconds: float, chunk_s: float = 0.5) -> None:
        frame = 0
        while frame < seconds * SAMPLE_RATE:
            pcm = sine_pcm(chunk_s, start_frame=frame)
            t_call = time.time()
            t_play = await self.out.write(pcm)
            self.play_times.append((t_call, t_play, len(pcm) / BYTES_PER_SECOND))
            frame += int(chunk_s * SAMPLE_RATE)


async def wait_for(pred, timeout: float = 5.0, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(step)
    return pred()

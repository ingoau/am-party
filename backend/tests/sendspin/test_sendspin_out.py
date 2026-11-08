"""End-to-end tests for am_party.player.sendspin_out using the aiosendspin reference client.

Run (from backend/):
  docker build -f tests/sendspin/Dockerfile -t am_party-sendspin-test:local .
  docker run --rm --label owner=am_party -v "$PWD":/app am_party-sendspin-test:local \
      python -m pytest -q tests/sendspin
"""

from __future__ import annotations

import asyncio
import time

import pytest

from am_party.player.sendspin_out import ANCHOR_CLIENT_ID

from .harness import Harness, make_client, wait_for

pytestmark = pytest.mark.asyncio


async def test_pacing_without_listeners() -> None:
    async with Harness(buffer_ahead=1.0) as h:
        t0 = time.time()
        await h.produce(4.0, chunk_s=0.5)
        elapsed = time.time() - t0
        for call, play, d in h.play_times:
            print(f"call+{call - t0:.3f} ahead={play - call:.3f} dur={d}")
        # 4 s of audio with ~1 s allowed ahead => ~3 s of real time
        last_call, last_play, last_dur = h.play_times[-1]
        assert 2.4 < elapsed < 4.0, elapsed
        assert abs((last_play + last_dur) - time.time() - h.buffer_ahead) < 0.2
        returned = [p for _, p, _ in h.play_times]
        durs = [d for _, _, d in h.play_times]
        # contiguous timeline: each write starts where the previous ended
        for a, b, d in zip(returned, returned[1:], durs):
            assert abs((b - a) - d) < 0.005, (a, b, d)
        # first audio scheduled shortly in the future
        first_call, first_play, _ = h.play_times[0]
        assert 0 < first_play - first_call < 1.0
        # steady state: never more than buffer_ahead (+ one write) ahead of now
        for call, play, _ in h.play_times:
            assert play - call <= h.buffer_ahead + 0.6
        assert h.out.listener_count() == 0
        await h.out.end_stream(drain=True)


async def test_two_clients_sync_and_late_join() -> None:
    async with Harness(buffer_ahead=2.0) as h:
        producer = asyncio.create_task(h.produce(12.0, chunk_s=0.25))
        a = await make_client("A", h.url, h.session)
        assert await wait_for(lambda: h.out.listener_count() == 1)
        assert await wait_for(lambda: len(a.chunks) > 5)
        assert a.client.is_time_synchronized() or await wait_for(
            a.client.is_time_synchronized, 5
        )

        h.out.set_metadata("Song", "Artist", "Album", "https://x/a.jpg", 200.0, 3.0, True)
        await asyncio.sleep(2.0)

        # B joins mid-song
        b = await make_client("B", h.url, h.session)
        assert await wait_for(lambda: h.out.listener_count() == 2)
        assert await wait_for(lambda: len(b.chunks) > 5)
        assert h.counts[:2] == [1, 2]

        # both in the party group (anchor is the founding member)
        srv = h.out._server
        assert srv is not None
        ga = srv.get_client(a.client._client_id).group  # noqa: SLF001
        gb = srv.get_client(b.client._client_id).group  # noqa: SLF001
        anchor = srv.get_client(ANCHOR_CLIENT_ID)
        assert ga is gb is anchor.group

        await asyncio.sleep(1.5)
        # same audio timeline: B's chunk timestamps line up with A's
        a_ts = {ts for ts, _ in a.chunks}
        b_ts = [ts for ts, _ in b.chunks]
        later_b = [ts for ts in b_ts if ts > max(b_ts) - 1_000_000]
        common = [ts for ts in later_b if ts in a_ts]
        assert len(common) >= len(later_b) * 0.8, (len(common), len(later_b))
        # first B chunk is scheduled close to "now" (joined mid-stream, not from start)
        # and both clients have metadata
        assert await wait_for(lambda: bool(a.metadata) and bool(b.metadata))

        # B leaves; A keeps playing
        n_a = len(a.chunks)
        await b.client.disconnect()
        assert await wait_for(lambda: h.out.listener_count() == 1)
        await asyncio.sleep(1.0)
        assert len(a.chunks) > n_a

        # A leaves too: group must survive, a new client C joins and plays
        await a.client.disconnect()
        assert await wait_for(lambda: h.out.listener_count() == 0)
        await asyncio.sleep(0.5)
        c = await make_client("C", h.url, h.session)
        assert await wait_for(lambda: len(c.chunks) > 5)
        assert srv.get_client(c.client._client_id).group is anchor.group  # noqa: SLF001

        # pause/skip: drop buffers immediately
        producer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await producer
        await h.out.end_stream(drain=False)
        assert await wait_for(lambda: c.stream_clears + c.stream_ends >= 1)

        # restart after stop
        await h.out.begin_stream()
        n_c = len(c.chunks)
        await h.produce(1.0, chunk_s=0.25)
        assert await wait_for(lambda: len(c.chunks) > n_c)
        await c.client.disconnect()
        print("counts", h.counts)


async def test_controller_commands_ignored() -> None:
    from aiosendspin.models.types import MediaCommand

    async with Harness(buffer_ahead=1.0) as h:
        producer = asyncio.create_task(h.produce(6.0, chunk_s=0.25))
        a = await make_client("A", h.url, h.session)
        b = await make_client("B", h.url, h.session)
        assert await wait_for(lambda: h.out.listener_count() == 2)
        srv = h.out._server
        anchor = srv.get_client(ANCHOR_CLIENT_ID)
        # "switch" would move A out of the party; "volume" would change everyone
        await a.client.send_group_command(MediaCommand.SWITCH)
        try:  # only advertised while the group reports a volume
            await a.client.send_group_command(MediaCommand.VOLUME, volume=5)
        except ValueError:
            pass
        await asyncio.sleep(1.0)
        assert srv.get_client(a.client._client_id).group is anchor.group  # noqa: SLF001
        assert srv.get_client(b.client._client_id).group is anchor.group  # noqa: SLF001
        n_a = len(a.chunks)
        assert await wait_for(lambda: len(a.chunks) > n_a)
        producer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await producer
        await a.client.disconnect()
        await b.client.disconnect()


async def test_native_listener_port() -> None:
    """With port != 0 the library's own listener also serves ws://host:port/sendspin."""
    import aiohttp

    from am_party.player.sendspin_out import SendspinOutput

    from .harness import free_port

    port = free_port()
    async with aiohttp.ClientSession() as session:
        out = SendspinOutput(session, name="Native", port=port, host="127.0.0.1")
        await out.start()
        try:
            producer = asyncio.create_task(_produce(out, 4.0))
            a = await make_client("N", f"ws://127.0.0.1:{port}/sendspin", session)
            assert await wait_for(lambda: len(a.chunks) > 5)
            assert out.listener_count() == 1
            await a.client.disconnect()
            producer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await producer
        finally:
            await out.stop()


async def _produce(out, seconds: float) -> None:
    from .harness import sine_pcm

    for i in range(int(seconds / 0.25)):
        await out.write(sine_pcm(0.25, start_frame=i * 12000))


async def test_per_client_events_and_volume_commands() -> None:
    from aiosendspin.models.types import PlayerCommand
    from aiosendspin.noise.keys import Identity

    async with Harness(buffer_ahead=1.0) as h:
        joined: list[tuple[str, str]] = []
        left: list[str] = []
        reported: list[tuple[str, int, bool]] = []
        h.out.on_client_joined = lambda cid, name: joined.append((cid, name))
        h.out.on_client_left = left.append
        h.out.on_client_volume = lambda cid, v, m: reported.append((cid, v, m))
        ident = Identity.generate()
        a = await make_client("Kitchen", h.url, h.session, ident, [PlayerCommand.VOLUME, PlayerCommand.MUTE])
        commands: list[object] = []
        a.client.add_server_command_listener(commands.append)
        cid = a.client._client_id  # noqa: SLF001
        assert await wait_for(lambda: joined == [(cid, "Kitchen")])
        assert await wait_for(lambda: (st := h.out.client_player_state(cid)) is not None and st[2])
        assert cid in h.out.connected_player_ids()

        assert h.out.set_client_volume(cid, 30, True)
        assert await wait_for(lambda: len(commands) >= 2)
        sent = {(c.player.command, c.player.volume, c.player.mute) for c in commands}  # type: ignore[attr-defined]
        assert (PlayerCommand.VOLUME, 30, None) in sent and (PlayerCommand.MUTE, None, True) in sent

        await a.client.send_player_state(available=True, volume=55, muted=False)
        assert await wait_for(lambda: (cid, 55, False) in reported)

        await a.client.disconnect()
        assert await wait_for(lambda: left == [cid])
        assert h.out.client_player_state(cid) is None
        assert not h.out.set_client_volume(cid, 10)

        # same identity => same client_id after a reconnect
        b = await make_client("Kitchen", h.url, h.session, ident, [PlayerCommand.VOLUME, PlayerCommand.MUTE])
        assert b.client._client_id == cid  # noqa: SLF001
        assert await wait_for(lambda: joined == [(cid, "Kitchen"), (cid, "Kitchen")])
        await b.client.disconnect()
        assert await wait_for(lambda: left == [cid, cid])

"""Sendspin output: play one continuous PCM stream in sync on many browsers.

Uses ``aiosendspin`` (server side of the Sendspin protocol) and is meant for
``@sendspin/sendspin-js`` 5.x clients running in web browsers.

Design
------
* One ``SendspinServer`` lives in-process. Browser websockets are handed to it
  directly from the main aiohttp app (``ws_proxy`` mounted at ``GET /sendspin``),
  so browsers only ever talk to the one public origin (``wss://`` works behind an
  HTTPS tunnel). Optionally the library's own listener is started on ``port`` too
  (plain ``ws://<host>:<port>/sendspin`` + mDNS advert) for native LAN clients;
  pass ``port=0`` to disable it.
* A silent, never-connecting "anchor" client (registered as an external player
  with a custom ``player@_am_party_anchor`` role) founds the single *party group*.
  Because it never leaves, the group (and its stream) survives every browser
  leaving and rejoining.
* Every real client that connects is moved into the party group; joining
  mid-stream is handled by the library's late-join catch-up from its PCM cache.
* sendspin-js 5 always encrypts (Noise) and connects *unpaired* with the
  Sentinel PSK. aiosendspin only activates roles for such clients when the
  pairing store lists them as "trusted unpaired", so we use a store that trusts
  every client: no pairing / PIN ever.
* Controller commands from clients are ignored: a browser must never be able to
  change the volume of, pause, or "switch" the whole party. Local volume/mute in
  sendspin-js is applied locally (and only reported to the server).
* Per-speaker volume/mute *can* be set from the server (``set_client_volume``), for
  owners controlling their claimed speakers centrally; ``on_client_joined`` /
  ``on_client_left`` / ``on_client_volume`` report each real player to the app.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from aiohttp import web

from aiosendspin.models.core import ClientHelloPayload, DeviceInfo
from aiosendspin.models.types import PlayerCommand
from aiosendspin.noise.keys import Identity
from aiosendspin.noise.trust_store import InMemoryServerPairingStore, TrustedUnpairedClient
from aiosendspin.server import (
    AudioFormat,
    ClientAddedEvent,
    ClientConnectedEvent,
    ClientDisconnectedEvent,
    ClientRemovedEvent,
    GroupMemberRemovedEvent,
    SendspinServer,
    VolumeChangedEvent,
)
from aiosendspin.server.compliance import ClientComplianceError
from aiosendspin.server.push_stream import MAIN_CHANNEL, PushStream, StreamStoppedError
from aiosendspin.server.roles import Role
from aiosendspin.server.roles.metadata.state import Metadata
from aiosendspin.server.roles.registry import register_role

from am_party.models import BYTES_PER_SECOND, CHANNELS, FRAME_SIZE, SAMPLE_RATE
from am_party.security import ConnectionCounter

if TYPE_CHECKING:
    import aiohttp

    from aiosendspin.server import SendspinClient, SendspinEvent, SendspinGroup

ANCHOR_ROLE_ID = "player@_am_party_anchor"
ANCHOR_CLIENT_ID = "am_party-party-anchor"
# Client ids a connecting client may not claim (they'd hijack the party group's anchor).
RESERVED_CLIENT_IDS = frozenset({ANCHOR_CLIENT_ID})
DEFAULT_MAX_LISTENERS = 100
DEFAULT_MAX_LISTENERS_PER_IP = 50  # party wifi usually shares one public IP

SLICE_SECONDS = 0.1
SLICE_BYTES = int(SAMPLE_RATE * SLICE_SECONDS) * FRAME_SIZE
PCM_FORMAT = AudioFormat(SAMPLE_RATE, 32, CHANNELS, "float")


class _AnchorRole(Role):
    """Player-family role for the anchor client.

    It has no audio requirements, so the push stream never encodes anything for
    it; it only exists so the party group always contains a "player" member and
    is therefore never torn down.
    """

    def __init__(self, client: SendspinClient) -> None:
        self._client = client

    @property
    def role_id(self) -> str:
        return ANCHOR_ROLE_ID

    @property
    def role_family(self) -> str:
        return "player"

    def get_audio_requirements(self) -> None:
        return None

    def has_connection(self) -> bool:
        return True

    def on_connect(self) -> None:
        pass

    def on_disconnect(self) -> None:
        pass


register_role(ANCHOR_ROLE_ID, _AnchorRole)


class _TrustAllPairingStore(InMemoryServerPairingStore):
    """Pairing store that approves every client for unpaired (Sentinel) playback."""

    async def trusted_unpaired(self, client_id: str) -> TrustedUnpairedClient | None:
        # Consulted for every hello right before the connection is bound to a client
        # object (the client_id is only visible inside the Noise-encrypted channel, so
        # it can't be checked earlier). Raising here makes the library drop the
        # connection cleanly instead of attaching it to the anchor.
        if client_id in RESERVED_CLIENT_IDS:
            raise ClientComplianceError(f"reserved client id {client_id!r}")
        found = await super().trusted_unpaired(client_id)
        return found or TrustedUnpairedClient(client_id=client_id)


class SendspinOutput:
    """Single party group streaming one PCM feed to every connected client."""

    def __init__(
        self,
        http: aiohttp.ClientSession,
        name: str = "am-party",
        port: int = 8927,
        logger: logging.Logger | None = None,
        *,
        buffer_ahead: float = 3.0,
        host: str = "0.0.0.0",
        max_listeners: int = DEFAULT_MAX_LISTENERS,
        max_listeners_per_ip: int = DEFAULT_MAX_LISTENERS_PER_IP,
    ) -> None:
        self._http = http
        self._name = name
        self._port = port
        self._host = host
        self._log = logger or logging.getLogger("am_party.sendspin")
        self.buffer_ahead = buffer_ahead
        self.on_listeners_changed: Callable[[int], None] | None = None
        # Per real player (client_id): joined the party (name), left, reported volume/mute.
        self.on_client_joined: Callable[[str, str], None] | None = None
        self.on_client_left: Callable[[str], None] | None = None
        self.on_client_volume: Callable[[str, int, bool], None] | None = None
        # admission control for /sendspin websockets (open connections, not just players)
        self._conns = ConnectionCounter(
            per_key=min(max_listeners_per_ip, max_listeners), total=max_listeners
        )

        self._server: SendspinServer | None = None
        self._anchor: SendspinClient | None = None
        self._push: PushStream | None = None
        self._write_lock = asyncio.Lock()
        self._carry = b""
        # Server-clock (us) timeline position right after the last committed sample.
        self._timeline_end_us: int | None = None
        self._last_count = 0
        self._metadata: Metadata | None = None
        self._unsub: Callable[[], None] | None = None
        self._group_unsub: Callable[[], None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        # volume listener per client_id, with the client object it is attached to (the
        # library replaces that object once a disconnected client is cleaned up)
        self._volume_watch: dict[str, tuple[SendspinClient, Callable[[], None]]] = {}
        # real players announced through on_client_joined and not yet through on_client_left,
        # with the connection they were announced for (a reconnect can replace a still-open
        # connection without any disconnect event)
        self._joined: dict[str, object] = {}

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._server is not None:
            return
        loop = asyncio.get_running_loop()
        # PyAV's first import takes ~1-2 s; do it now rather than on the first write.
        await asyncio.to_thread(importlib.import_module, "av")
        server = SendspinServer(
            loop,
            Identity.generate(),
            self._name,
            self._http,
            pairing_store=_TrustAllPairingStore(),
            # sendspin-js 5 is always encrypted; legacy unencrypted clients are refused
            # (the trust-all store would make the library reject them anyway).
            allow_unencrypted=False,
            allow_noncompliant_clients=True,
        )
        self._server = server
        self._unsub = server.add_event_listener(self._on_server_event)

        hello = ClientHelloPayload(
            client_id=ANCHOR_CLIENT_ID,
            name=self._name,
            version=1,
            supported_roles=[ANCHOR_ROLE_ID],
            device_info=DeviceInfo(product_name="am-party anchor", manufacturer="am-party"),
        )
        self._anchor = server.register_external_player(hello, on_stream_start=_noop_stream_start)
        group = self._anchor.group
        group.set_group_name(self._name)
        self._group_unsub = group.add_event_listener(self._on_group_event)
        self._disable_group_controller(group)

        if self._port:
            try:
                await server.start_server(
                    port=self._port, host=self._host, discover_clients=False
                )
            except OSError as err:
                self._log.warning(
                    "Sendspin native listener on port %s unavailable (%s); "
                    "only the /sendspin proxy route will work",
                    self._port,
                    err,
                )
        self._log.info("Sendspin output started (party group %s)", group.group_id)

    async def stop(self) -> None:
        server = self._server
        if server is None:
            return
        with contextlib.suppress(Exception):
            await self.end_stream(drain=False)
        if self._group_unsub:
            self._group_unsub()
        if self._unsub:
            self._unsub()
        await server.close()
        for _client, unsub in self._volume_watch.values():
            unsub()
        self._volume_watch.clear()
        self._joined.clear()
        for task in list(self._tasks):
            task.cancel()
        self._server = None
        self._anchor = None
        self._set_count(0)

    # ------------------------------------------------------------------ clients

    @property
    def _group(self) -> SendspinGroup:
        assert self._anchor is not None, "SendspinOutput not started"
        return self._anchor.group

    def _real_clients(self) -> list[SendspinClient]:
        if self._server is None:
            return []
        return [
            c
            for c in self._server.connected_clients
            if c.client_id != ANCHOR_CLIENT_ID
            and any(r.startswith("player@") for r in c.negotiated_role_ids)
        ]

    def listener_count(self) -> int:
        return len(self._real_clients())

    def _set_count(self, count: int) -> None:
        if count == self._last_count:
            return
        self._last_count = count
        if self.on_listeners_changed is not None:
            try:
                self.on_listeners_changed(count)
            except Exception:
                self._log.exception("on_listeners_changed callback failed")

    def _refresh_count(self) -> None:
        self._set_count(self.listener_count())

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_server_event(self, _server: SendspinServer, event: SendspinEvent) -> None:
        match event:
            case ClientConnectedEvent(client_id=cid) | ClientAddedEvent(client_id=cid):
                if cid != ANCHOR_CLIENT_ID:
                    # Fires before the connection's roles are attached: defer a tick.
                    self._spawn(self._join_party(cid))
            case ClientDisconnectedEvent(client_id=cid) | ClientRemovedEvent(client_id=cid):
                asyncio.get_running_loop().call_soon(self._refresh_count)
                asyncio.get_running_loop().call_soon(self._maybe_left, cid)
                if isinstance(event, ClientRemovedEvent):
                    self._release_watch(cid)

    def _on_group_event(self, _group: SendspinGroup, event: Any) -> None:
        # A connected member that got moved out (e.g. a "leave") is pulled back in.
        if isinstance(event, GroupMemberRemovedEvent) and event.client_id != ANCHOR_CLIENT_ID:
            self._spawn(self._join_party(event.client_id, delay=0.2))

    async def _join_party(self, client_id: str, delay: float = 0.0) -> None:
        await asyncio.sleep(delay)
        server = self._server
        if server is None or self._anchor is None:
            return
        client = server.get_client(client_id)
        if client is None or not client.is_connected:
            self._refresh_count()
            return
        group = self._group
        if client.group is not group:
            self._log.debug("Adding %s (%s) to party group", client.name, client_id)
            await group.add_client(client)
            if self._metadata is not None:
                self._apply_metadata()
        self._refresh_count()
        if client.is_connected and _player_role(client) is not None:
            self._watch_volume(client)
            conn = client.connection
            if client_id not in self._joined or self._joined[client_id] is not conn:
                # new, or back on a fresh connection: (re)sync it either way
                self._joined[client_id] = conn
                self._notify(self.on_client_joined, client_id, client.name)

    def _maybe_left(self, client_id: str) -> None:
        if client_id not in self._joined:
            return
        client = self._server.get_client(client_id) if self._server else None
        if client is not None and client.is_connected:
            return  # already back (a reconnect replaced the old connection)
        del self._joined[client_id]
        self._notify(self.on_client_left, client_id)

    def _release_watch(self, client_id: str) -> None:
        # The removal event can arrive after the same client_id already reconnected as a new
        # client object; keep the listener that belongs to the current one.
        watch = self._volume_watch.get(client_id)
        current = self._server.get_client(client_id) if self._server else None
        if watch is not None and watch[0] is not current:
            del self._volume_watch[client_id]
            watch[1]()

    def _watch_volume(self, client: SendspinClient) -> None:
        cid = client.client_id
        watched = self._volume_watch.get(cid)
        if watched is not None:
            if watched[0] is client:
                return
            watched[1]()  # stale object from before a cleanup

        def _on_event(_client: SendspinClient, event: Any) -> None:
            if isinstance(event, VolumeChangedEvent) and cid in self._joined:
                self._notify(self.on_client_volume, cid, event.volume, event.muted)

        self._volume_watch[cid] = (client, client.add_event_listener(_on_event))

    def _notify(self, cb: Callable[..., None] | None, *args: Any) -> None:
        if cb is None:
            return
        try:
            cb(*args)
        except Exception:
            self._log.exception("Sendspin client callback failed")

    def connected_player_ids(self) -> set[str]:
        return {c.client_id for c in self._real_clients()}

    def client_player_state(self, client_id: str) -> tuple[int, bool, bool] | None:
        """(volume, muted, settable) of a connected player, or None.

        ``settable`` turns true once this connection's client/state has declared the
        volume and mute commands; until then commands would be silently dropped and the
        reported values are only the library's defaults.
        """
        client = self._server.get_client(client_id) if self._server else None
        if client is None or not client.is_connected:
            return None
        role = _player_role(client)
        if role is None:
            return None
        cmds = set(getattr(role, "state_supported_commands", ()) or ())
        settable = PlayerCommand.VOLUME in cmds and PlayerCommand.MUTE in cmds
        return int(role.volume), bool(role.muted), settable

    def set_client_volume(
        self, client_id: str, volume: int | None = None, muted: bool | None = None
    ) -> bool:
        """Command one connected player's volume and/or mute. False if it isn't reachable."""
        client = self._server.get_client(client_id) if self._server else None
        if client is None or not client.is_connected:
            return False
        role = _player_role(client)
        if role is None:
            return False
        try:
            if volume is not None:
                role.set_volume(max(0, min(100, int(volume))))
            if muted is not None:
                role.set_mute(bool(muted))
        except Exception:
            self._log.exception("Failed to command Sendspin player %s", client_id)
            return False
        return True

    def _disable_group_controller(self, group: SendspinGroup) -> None:
        """Ignore controller commands (volume/mute/switch/...) from clients."""
        role = group.group_role("controller")
        if role is None:
            return

        def _ignore(cmd: Any) -> None:
            self._log.debug("Ignoring Sendspin controller command %s", getattr(cmd, "command", cmd))

        role.handle_command = _ignore  # type: ignore[method-assign]

    # ------------------------------------------------------------------ audio

    def _server_to_wall(self, server_us: int) -> float:
        assert self._server is not None
        return time.time() + (server_us - self._server.clock.now_us()) / 1_000_000

    async def begin_stream(self) -> None:
        if self._server is None:
            raise RuntimeError("SendspinOutput not started")
        if self._push is not None and not self._push.is_stopped:
            return
        self._push = self._group.start_stream()
        self._carry = b""
        self._timeline_end_us = None
        if self._metadata is not None:
            self._apply_metadata()

    async def write(self, pcm: bytes) -> float:
        async with self._write_lock:
            if self._push is None or self._push.is_stopped:
                await self.begin_stream()
            push = self._push
            assert push is not None and self._server is not None

            # Where the first sample of `pcm` lands: right after anything carried over.
            carry_len = len(self._carry)
            data = self._carry + pcm
            usable = len(data) - (len(data) % FRAME_SIZE)
            self._carry = data[usable:]

            first_us: int | None = None
            offset = 0
            limit_us = int(self.buffer_ahead * 1_000_000)
            while offset < usable:
                chunk = data[offset : min(offset + SLICE_BYTES, usable)]
                push.prepare_audio(chunk, PCM_FORMAT, channel_id=MAIN_CHANNEL)
                try:
                    start_us = await push.commit_audio()
                except StreamStoppedError:
                    break
                if first_us is None:
                    first_us = start_us
                self._timeline_end_us = start_us + len(chunk) * 1_000_000 // BYTES_PER_SECOND
                offset += len(chunk)
                await push.sleep_to_limit_buffer(limit_us)

            if first_us is None:
                first_us = self._timeline_end_us or self._server.clock.now_us()
                carry_len = 0
            return self._server_to_wall(first_us + carry_len * 1_000_000 // BYTES_PER_SECOND)

    async def end_stream(self, drain: bool = True) -> None:
        async with self._write_lock:
            push = self._push
            self._push = None
            self._carry = b""
            if push is None or push.is_stopped or self._server is None:
                return
            if drain and self._timeline_end_us is not None:
                remaining = (self._timeline_end_us - self._server.clock.now_us()) / 1_000_000
                if remaining > 0:
                    # stream/end makes clients drop what they buffered, so wait it out.
                    await asyncio.sleep(remaining + 0.1)
            elif not drain:
                push.clear()  # stream/clear: clients drop buffered audio now
            self._timeline_end_us = None
            await self._group.stop(keep_progress=drain is False)

    # ------------------------------------------------------------------ metadata

    def set_metadata(
        self,
        title: str | None,
        artist: str | None,
        album: str | None,
        artwork_url: str | None,
        duration_s: float,
        position_s: float,
        playing: bool,
    ) -> None:
        self._metadata = Metadata(
            title=title,
            artist=artist,
            album=album,
            artwork_url=artwork_url,
            track_duration=max(0, int(duration_s * 1000)) if duration_s else 0,
            track_progress=max(0, int(position_s * 1000)),
            playback_speed=1000 if playing else 0,
        )
        if self._server is not None:
            self._apply_metadata()

    def _apply_metadata(self) -> None:
        role = self._group.group_role("metadata")
        if role is None or self._metadata is None:
            return
        try:
            role.set_metadata(self._metadata)  # type: ignore[attr-defined]
        except Exception:
            self._log.exception("Failed to push Sendspin metadata")

    # ------------------------------------------------------------------ websocket

    async def ws_proxy(
        self, request: web.Request, client_ip: str | None = None
    ) -> web.WebSocketResponse:
        """Handle ``GET /sendspin`` on the main app.

        The websocket is terminated right here and handed to the in-process Sendspin
        server, which is equivalent to (and cheaper than) proxying frames to its own
        listener: browsers see a single origin, ws:// or wss:// as the page.

        Connections are capped per client IP and in total (``on_client_connect``
        runs for the whole lifetime of the connection).
        """
        if self._server is None:
            raise web.HTTPServiceUnavailable(text="Sendspin output not running")
        key = client_ip or request.remote or "unknown"
        if not self._conns.acquire(key):
            self._log.warning("Refusing Sendspin connection from %s: listener limit reached", key)
            raise web.HTTPServiceUnavailable(text="Too many listeners connected")
        try:
            resp = await self._server.on_client_connect(request)
        finally:
            self._conns.release(key)
        assert isinstance(resp, web.WebSocketResponse)
        return resp


def _player_role(client: SendspinClient) -> Any | None:
    """The client's real (audio) player role, if it negotiated one."""
    for role in client.roles_by_family("player"):
        if hasattr(role, "set_volume") and hasattr(role, "set_mute"):
            return role
    return None


def _noop_stream_start(*_args: Any, **_kwargs: Any) -> None:
    """The anchor never connects; nothing to wake up."""

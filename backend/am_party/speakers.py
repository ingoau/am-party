"""Claimable speakers: browsers listening via Sendspin that people can own and control.

* A speaker is identified by its Sendspin ``client_id`` (the device's public key, kept in
  its localStorage), so it keeps its identity across reconnects. The client_id is only
  ever sent by the device itself; owner/admin APIs use an opaque public ``id``.
* Anyone with ``claim_speakers`` can claim a connected speaker by scanning its QR code
  (a claim token that rotates every time the speaker connects). Owners are equal
  co-owners; admins see and control every speaker.
* Volume, mute and "lock local controls" are stored here and reapplied when a speaker
  reconnects. Local changes on the device win (last change wins) unless it is locked,
  in which case the stored values are pushed straight back.
* Shares: a single-use, short-lived token for a set of speakers, either adding the
  accepter as a co-owner ("share") or also removing the sharer ("handover").
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from .db import Database, now
from .party import ApiError

if TYPE_CHECKING:
    from .auth import User
    from .party import Hub

LOGGER = logging.getLogger(__name__)

CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
NAME_MAX = 40
DEFAULT_NAME = "Speaker"
DEFAULT_VOLUME = 80  # assumed for a relative change on a speaker that never reported one
SHARE_TTL = 600.0
MAX_SHARES = 500
MAX_REQUEST_SOURCES = 2000  # rotated claim tokens remembered for tagging requests
MAX_IDS = 200
# How long to wait for a (re)connected speaker to accept volume commands before giving up.
SYNC_TIMEOUT = 8.0
SYNC_POLL = 0.25
NOTIFY_DELAY = 0.2
# Unclaimed speakers nobody has heard from in this long are forgotten.
PRUNE_AFTER = 30 * 24 * 3600
PRUNE_EVERY = 6 * 3600
# Identities are free to make, so keep at most this many unclaimed offline speakers.
MAX_UNCLAIMED = 300
MAX_SHARES_PER_USER = 20


class SpeakerOutput(Protocol):
    def connected_player_ids(self) -> set[str]: ...
    def client_player_state(self, client_id: str) -> tuple[int, bool, bool] | None: ...
    def set_client_volume(
        self, client_id: str, volume: int | None = None, muted: bool | None = None
    ) -> bool: ...


@dataclass
class Share:
    token: str
    by_user_id: int
    by_name: str
    speaker_ids: list[str]
    mode: str
    expires_at: float


@dataclass
class _Live:
    """A currently connected speaker."""

    client_id: str
    claim_token: str
    # False until the stored volume/mute has been applied (or adopted) on this connection;
    # reports before that are the device's pre-sync values and are ignored.
    synced: bool = False
    sync_task: asyncio.Task[Any] | None = field(default=None, repr=False)


def clean_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ApiError(400, "name must be a string")
    name = " ".join(value.split())[:NAME_MAX].strip()
    if not name:
        raise ApiError(400, "Name can't be empty")
    return name


def _bool(data: dict[str, Any], key: str) -> bool | None:
    val = data.get(key)
    if val is None:
        return None
    if not isinstance(val, bool):
        raise ApiError(400, f"{key} must be true or false")
    return val


def _int(data: dict[str, Any], key: str, lo: int, hi: int) -> int | None:
    val = data.get(key)
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, int | float) or val != val:  # noqa: PLR0124
        raise ApiError(400, f"{key} must be a number")
    if not lo <= val <= hi:
        raise ApiError(400, f"{key} must be between {lo} and {hi}")
    return round(val)


def _ids(data: dict[str, Any]) -> list[str]:
    ids = data.get("ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise ApiError(400, "ids must be a non-empty list of speaker ids")
    if len(ids) > MAX_IDS:
        raise ApiError(400, f"At most {MAX_IDS} speakers at once")
    return list(dict.fromkeys(ids))


class SpeakerManager:
    def __init__(self, db: Database, hub: Hub, output: SpeakerOutput | None = None) -> None:
        self.db = db
        self.hub = hub
        self.output = output
        self._live: dict[str, _Live] = {}  # speaker id -> live connection
        self._by_client: dict[str, str] = {}  # client_id -> speaker id (connected only)
        self._claims: dict[str, str] = {}  # claim token -> speaker id
        # Every claim token handed out this run -> speaker id, kept after it rotates so guests
        # who scanned an older QR still tag their requests with that speaker (oldest dropped).
        self._sources: dict[str, str] = {}
        self._shares: dict[str, Share] = {}
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._notify_handle: asyncio.TimerHandle | None = None
        self._notify_list = False
        self._notify_self: set[str] = set()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await self.prune()
        self._spawn(self._prune_loop())

    async def close(self) -> None:
        if self._notify_handle:
            self._notify_handle.cancel()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (err := task.exception()) is not None:
            LOGGER.error("Speaker task failed", exc_info=err)

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.sleep(PRUNE_EVERY)
            await self.prune()

    async def prune(self) -> int:
        """Forget unclaimed speakers that have been offline for a long time."""
        cutoff = now() - PRUNE_AFTER
        live = list(self._live)
        marks = ",".join("?" * len(live))
        sql = (
            "DELETE FROM speakers WHERE COALESCE(last_seen, created_at) < ? "
            "AND id NOT IN (SELECT speaker_id FROM speaker_owners)"
        )
        if live:
            sql += f" AND id NOT IN ({marks})"
        n = await self.db.execute_rowcount(sql, cutoff, *live)
        if n:
            LOGGER.info("Forgot %d unclaimed speakers not seen in %d days", n, PRUNE_AFTER // 86400)
        return n

    async def _evict_unclaimed(self, keep: str) -> None:
        """Drop the oldest unclaimed offline speakers beyond MAX_UNCLAIMED (never `keep`)."""
        live = [*self._live, keep]
        sql = (
            "SELECT id FROM speakers WHERE id NOT IN (SELECT speaker_id FROM speaker_owners)"
            + (f" AND id NOT IN ({','.join('?' * len(live))})" if live else "")
            + " ORDER BY COALESCE(last_seen, created_at) DESC LIMIT -1 OFFSET ?"
        )
        # `keep` (the speaker just added) is one of the MAX_UNCLAIMED that stay
        old = [r["id"] for r in await self.db.fetchall(sql, *live, max(0, MAX_UNCLAIMED - 1))]
        if old:
            await self.db.execute(
                f"DELETE FROM speakers WHERE id IN ({','.join('?' * len(old))})", *old
            )

    def owners_changed(self, speaker_ids: list[str]) -> None:
        """Owners changed outside this manager (e.g. a user was deleted)."""
        for sid in speaker_ids:
            self._changed(sid, device=True)
        self._changed()

    # ------------------------------------------------------------------ notifications
    def _changed(self, speaker_id: str | None = None, *, device: bool = False) -> None:
        """Coalesce change notifications (volume drags report many times a second)."""
        self._notify_list = True
        if device and speaker_id:
            self._notify_self.add(speaker_id)
        if self._notify_handle is None:
            loop = asyncio.get_running_loop()
            self._notify_handle = loop.call_later(NOTIFY_DELAY, self._flush_notify)

    def _flush_notify(self) -> None:
        self._notify_handle = None
        notify_list, self._notify_list = self._notify_list, False
        ids, self._notify_self = self._notify_self, set()
        self._spawn(self._send_notify(notify_list, ids))

    async def _send_notify(self, notify_list: bool, ids: set[str]) -> None:
        if notify_list:
            await self.hub.send_all({"type": "speakers_changed"}, "claim_speakers")
        for sid in ids:
            await self.hub.send_all({"type": "speaker_changed", "id": sid})

    # ------------------------------------------------------------------ sendspin events
    def on_client_joined(self, client_id: str, name: str) -> None:
        self._spawn(self._joined(client_id, name))

    def on_client_left(self, client_id: str) -> None:
        self._spawn(self._left(client_id))

    def on_client_volume(self, client_id: str, volume: int, muted: bool) -> None:
        self._spawn(self._reported(client_id, volume, muted))

    async def _joined(self, client_id: str, hello_name: str) -> None:
        if not CLIENT_ID_RE.match(client_id):
            return
        async with self._lock:
            row = await self.db.fetchone("SELECT * FROM speakers WHERE client_id=?", client_id)
            t = now()
            if row is None:
                sid = secrets.token_urlsafe(9)
                name = _hello_name(hello_name)
                await self.db.execute(
                    "INSERT INTO speakers(id, client_id, name, created_at, last_seen) VALUES(?,?,?,?,?)",
                    sid, client_id, name, t, t,
                )
                await self._evict_unclaimed(keep=sid)
            else:
                sid = row["id"]
                # Until a name was chosen deliberately (and unless the owner locked the
                # speaker's local controls), follow what the device announces.
                if not row["named"] and not row["locked"] and _hello_name(hello_name) != row["name"]:
                    await self.db.execute(
                        "UPDATE speakers SET name=?, last_seen=? WHERE id=?", _hello_name(hello_name), t, sid
                    )
                else:
                    await self.db.execute("UPDATE speakers SET last_seen=? WHERE id=?", t, sid)
            old = self._live.pop(sid, None)
            if old is not None:
                self._claims.pop(old.claim_token, None)
                if old.sync_task:
                    old.sync_task.cancel()
            live = _Live(client_id=client_id, claim_token=secrets.token_urlsafe(12))
            self._live[sid] = live
            self._by_client[client_id] = sid
            self._claims[live.claim_token] = sid
            self._sources[live.claim_token] = sid
            while len(self._sources) > MAX_REQUEST_SOURCES:
                del self._sources[next(iter(self._sources))]
            live.sync_task = self._spawn(self._sync(sid, live))
        self._changed(sid, device=True)

    async def _sync(self, sid: str, live: _Live) -> None:
        """Apply the stored volume/mute to a freshly connected speaker (or adopt its own)."""
        out = self.output
        if out is None:
            live.synced = True
            return
        deadline = time.monotonic() + SYNC_TIMEOUT
        state = out.client_player_state(live.client_id)
        while (state is None or not state[2]) and time.monotonic() < deadline:
            await asyncio.sleep(SYNC_POLL)
            if self._live.get(sid) is not live:
                return
            state = out.client_player_state(live.client_id)
        if self._live.get(sid) is not live:
            return
        async with self._lock:
            if self._live.get(sid) is not live:
                return
            row = await self.db.fetchone("SELECT volume, muted FROM speakers WHERE id=?", sid)
            if row is None:
                return
            # re-read: reports handled while waiting for the lock were dropped (not synced yet)
            state = out.client_player_state(live.client_id)
            # Only a connection that declared the commands has reported real values; before
            # that they are the library's defaults (and commands would be dropped).
            if state is not None and state[2]:
                if row["volume"] is None:
                    await self.db.execute(
                        "UPDATE speakers SET volume=?, muted=? WHERE id=?", state[0], int(state[1]), sid
                    )
                else:
                    _push(out, live.client_id, (row["volume"], bool(row["muted"])), (state[0], state[1]))
            live.synced = True
        self._changed(sid)

    async def _left(self, client_id: str) -> None:
        async with self._lock:
            sid = self._by_client.pop(client_id, None)
            if sid is None:
                return
            live = self._live.pop(sid, None)
            if live is not None:
                self._claims.pop(live.claim_token, None)
                if live.sync_task:
                    live.sync_task.cancel()
            await self.db.execute("UPDATE speakers SET last_seen=? WHERE id=?", now(), sid)
        self._changed(sid, device=True)

    async def _reported(self, client_id: str, volume: int, muted: bool) -> None:
        async with self._lock:
            sid = self._by_client.get(client_id)
            live = self._live.get(sid) if sid else None
            if sid is None or live is None or not live.synced:
                return
            row = await self.db.fetchone("SELECT volume, muted, locked FROM speakers WHERE id=?", sid)
            if row is None:
                return
            stored = (row["volume"], bool(row["muted"]))
            if stored == (volume, muted):
                return
            if row["locked"] and row["volume"] is not None:
                # local controls are locked: put the owner's values straight back
                if self.output is not None:
                    _push(self.output, client_id, (row["volume"], bool(row["muted"])), (volume, muted))
                return
            await self.db.execute(
                "UPDATE speakers SET volume=?, muted=?, last_seen=? WHERE id=?",
                volume, int(muted), now(), sid,
            )
        self._changed(sid)

    # ------------------------------------------------------------------ views
    async def _owners(self, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {i: [] for i in ids}
        if not ids:
            return out
        rows = await self.db.fetchall(
            "SELECT o.speaker_id, u.id, u.name, u.email, u.sub FROM speaker_owners o "
            f"JOIN users u ON u.id = o.user_id WHERE o.speaker_id IN ({','.join('?' * len(ids))}) "
            "ORDER BY o.added_at, u.id",
            *ids,
        )
        for r in rows:
            out[r["speaker_id"]].append({"id": r["id"], "name": r["name"] or r["email"] or r["sub"]})
        return out

    async def _views(self, rows: list[dict[str, Any]], user: User) -> list[dict[str, Any]]:
        owners = await self._owners([r["id"] for r in rows])
        views = [
            {
                "id": r["id"],
                "name": r["name"],
                "connected": r["id"] in self._live,
                "last_seen": r["last_seen"],
                "volume": r["volume"],
                "muted": bool(r["muted"]),
                "locked": bool(r["locked"]),
                "owners": owners[r["id"]],
                "mine": any(o["id"] == user.id for o in owners[r["id"]]),
            }
            for r in rows
        ]
        views.sort(key=lambda v: (not v["connected"], v["name"].casefold(), v["id"]))
        return views

    async def _view(self, sid: str, user: User) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM speakers WHERE id=?", sid)
        return (await self._views([row], user))[0] if row else None

    async def list_for(self, user: User) -> list[dict[str, Any]]:
        if user.is_admin:
            rows = await self.db.fetchall("SELECT * FROM speakers")
        else:
            rows = await self.db.fetchall(
                "SELECT s.* FROM speakers s JOIN speaker_owners o ON o.speaker_id = s.id WHERE o.user_id=?",
                user.id,
            )
        return await self._views(rows, user)

    async def _accessible(self, user: User, ids: list[str]) -> list[dict[str, Any]]:
        """Rows for ids, all of which the user must be able to control."""
        marks = ",".join("?" * len(ids))
        rows = await self.db.fetchall(f"SELECT * FROM speakers WHERE id IN ({marks})", *ids)
        if len(rows) != len(ids):
            raise ApiError(404, "Speaker not found")
        if not user.is_admin:
            owned = await self.db.fetchall(
                f"SELECT speaker_id FROM speaker_owners WHERE user_id=? AND speaker_id IN ({marks})",
                user.id, *ids,
            )
            if len(owned) != len(ids):
                raise ApiError(403, "You don't control that speaker")
        return rows

    # ------------------------------------------------------------------ device
    async def device_view(self, client_id: Any) -> dict[str, Any]:
        row = await self._device_row(client_id)
        live = self._live.get(row["id"])
        owned = await self.db.fetchone("SELECT 1 AS x FROM speaker_owners WHERE speaker_id=? LIMIT 1", row["id"])
        return {
            "id": row["id"],
            "name": row["name"],
            "named": bool(row["named"]),
            "locked": bool(row["locked"]),
            "claimed": owned is not None,
            "connected": live is not None,
            "claim_token": live.claim_token if live else None,
        }

    async def device_rename(self, client_id: Any, name: Any) -> dict[str, Any]:
        new = clean_name(name)
        async with self._lock:
            row = await self._device_row(client_id)
            if row["locked"]:
                raise ApiError(423, "This speaker's controls are locked by its owner")
            await self.db.execute("UPDATE speakers SET name=?, named=1 WHERE id=?", new, row["id"])
        self._changed(row["id"], device=True)
        return await self.device_view(client_id)

    async def _device_row(self, client_id: Any) -> dict[str, Any]:
        if not isinstance(client_id, str) or not CLIENT_ID_RE.match(client_id):
            raise ApiError(400, "Invalid client_id")
        row = await self.db.fetchone("SELECT * FROM speakers WHERE client_id=?", client_id)
        if row is None:
            raise ApiError(404, "This speaker hasn't connected yet")
        return row

    # ------------------------------------------------------------------ claiming
    def _claim_target(self, token: Any) -> str:
        sid = self._claims.get(token) if isinstance(token, str) else None
        if sid is None or sid not in self._live:
            raise ApiError(404, "That speaker code has expired. Scan the speaker again.")
        return sid

    def speaker_for_token(self, token: Any) -> str | None:
        """The speaker a claim token (current or rotated, from its QR) belongs to, or None.

        Only used to tag requests with their source: unlike claiming, an old token still counts.
        """
        return self._sources.get(token) if isinstance(token, str) else None

    async def claim_preview(self, user: User, token: Any) -> dict[str, Any]:
        sid = self._claim_target(token)
        row = await self.db.fetchone("SELECT id, name FROM speakers WHERE id=?", sid)
        if row is None:
            raise ApiError(404, "Speaker not found")
        mine = await self.db.fetchone(
            "SELECT 1 AS x FROM speaker_owners WHERE speaker_id=? AND user_id=?", sid, user.id
        )
        return {
            "speaker": {"id": row["id"], "name": row["name"], "connected": True},
            "already_mine": mine is not None,
        }

    async def claim(self, user: User, token: Any) -> dict[str, Any]:
        async with self._lock:
            sid = self._claim_target(token)
            await self._add_owner(sid, user.id)
        self._changed(sid, device=True)
        view = await self._view(sid, user)
        assert view is not None
        return view

    async def _add_owner(self, sid: str, user_id: int) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO speaker_owners(speaker_id, user_id, added_at) VALUES(?,?,?)",
            sid, user_id, now(),
        )

    # ------------------------------------------------------------------ control
    async def update(self, user: User, sid: str, data: dict[str, Any]) -> dict[str, Any]:
        name = clean_name(data["name"]) if data.get("name") is not None else None
        locked = _bool(data, "locked")
        async with self._lock:
            await self._accessible(user, [sid])
            if name is not None:
                await self.db.execute("UPDATE speakers SET name=?, named=1 WHERE id=?", name, sid)
            if locked is not None:
                await self.db.execute("UPDATE speakers SET locked=? WHERE id=?", int(locked), sid)
        self._changed(sid, device=True)
        view = await self._view(sid, user)
        assert view is not None
        return view

    async def control(self, user: User, data: dict[str, Any]) -> list[dict[str, Any]]:
        ids = _ids(data)
        volume = _int(data, "volume", 0, 100)
        delta = _int(data, "volume_delta", -100, 100)
        muted = _bool(data, "muted")
        locked = _bool(data, "locked")
        if volume is not None and delta is not None:
            raise ApiError(400, "Give either volume or volume_delta, not both")
        if volume is None and delta is None and muted is None and locked is None:
            raise ApiError(400, "Nothing to change")
        async with self._lock:
            rows = await self._accessible(user, ids)
            for r in rows:
                new_vol = r["volume"]
                if volume is not None:
                    new_vol = volume
                elif delta is not None:
                    base = DEFAULT_VOLUME if r["volume"] is None else r["volume"]
                    new_vol = max(0, min(100, base + delta))
                new_muted = bool(r["muted"]) if muted is None else muted
                new_locked = bool(r["locked"]) if locked is None else locked
                await self.db.execute(
                    "UPDATE speakers SET volume=?, muted=?, locked=? WHERE id=?",
                    new_vol, int(new_muted), int(new_locked), r["id"],
                )
                live = self._live.get(r["id"])
                if live is not None and self.output is not None and (volume is not None or delta is not None or muted is not None):
                    self.output.set_client_volume(
                        live.client_id,
                        new_vol if (volume is not None or delta is not None) else None,
                        muted,
                    )
                if locked is not None and locked != bool(r["locked"]):
                    self._notify_self.add(r["id"])
        self._changed()
        views = await self.list_for(user)
        wanted = set(ids)
        return [v for v in views if v["id"] in wanted]

    async def remove_owner(self, user: User, sid: str, user_id: int) -> dict[str, Any] | None:
        async with self._lock:
            await self._accessible(user, [sid])
            n = await self.db.execute_rowcount(
                "DELETE FROM speaker_owners WHERE speaker_id=? AND user_id=?", sid, user_id
            )
            if not n:
                raise ApiError(404, "They don't control that speaker")
        self._changed(sid, device=True)
        if user.id == user_id and not user.is_admin:
            return None
        return await self._view(sid, user)

    async def forget(self, user: User, sid: str) -> None:
        if not user.is_admin:
            raise ApiError(403, "Only admins can forget speakers")
        async with self._lock:
            if sid in self._live:
                raise ApiError(409, "That speaker is connected right now")
            if not await self.db.execute_rowcount("DELETE FROM speakers WHERE id=?", sid):
                raise ApiError(404, "Speaker not found")
        self._changed()

    # ------------------------------------------------------------------ sharing
    def _prune_shares(self) -> None:
        t = time.time()
        for tok in [k for k, s in self._shares.items() if s.expires_at <= t]:
            del self._shares[tok]

    async def create_share(self, user: User, data: dict[str, Any]) -> dict[str, Any]:
        ids = _ids(data)
        mode = data.get("mode", "share")
        if mode not in ("share", "handover"):
            raise ApiError(400, "mode must be share or handover")
        await self._accessible(user, ids)
        self._prune_shares()
        if (
            len(self._shares) >= MAX_SHARES
            or sum(sh.by_user_id == user.id for sh in self._shares.values()) >= MAX_SHARES_PER_USER
        ):
            raise ApiError(429, "Too many open share codes; try again in a few minutes")
        share = Share(
            token=secrets.token_urlsafe(16),
            by_user_id=user.id,
            by_name=user.name or user.email or "Someone",
            speaker_ids=ids,
            mode=mode,
            expires_at=time.time() + SHARE_TTL,
        )
        self._shares[share.token] = share
        return {"token": share.token, "expires_at": share.expires_at, "mode": mode}

    def _share(self, token: Any) -> Share:
        self._prune_shares()
        share = self._shares.get(token) if isinstance(token, str) else None
        if share is None:
            raise ApiError(404, "That share code has expired or was already used")
        return share

    async def _still_shareable(self, share: Share) -> list[str]:
        """The shared speakers the sharer still controls (they may have been removed since)."""
        from .auth import User

        row = await self.db.fetchone("SELECT * FROM users WHERE id=?", share.by_user_id)
        sharer = User.from_row(row) if row else None
        if sharer is None or not sharer.has("claim_speakers"):
            return []
        marks = ",".join("?" * len(share.speaker_ids))
        if sharer.is_admin:
            rows = await self.db.fetchall(
                f"SELECT id FROM speakers WHERE id IN ({marks})", *share.speaker_ids
            )
        else:
            rows = await self.db.fetchall(
                f"SELECT speaker_id AS id FROM speaker_owners WHERE user_id=? AND speaker_id IN ({marks})",
                sharer.id, *share.speaker_ids,
            )
        return [r["id"] for r in rows]

    async def share_preview(self, token: Any) -> dict[str, Any]:
        share = self._share(token)
        ids = await self._still_shareable(share)
        if not ids:
            raise ApiError(410, "Whoever shared these speakers no longer controls them")
        marks = ",".join("?" * len(ids))
        rows = await self.db.fetchall(f"SELECT id, name FROM speakers WHERE id IN ({marks})", *ids)
        speakers = sorted(
            ({"id": r["id"], "name": r["name"], "connected": r["id"] in self._live} for r in rows),
            key=lambda s: s["name"].casefold(),
        )
        return {"mode": share.mode, "from": share.by_name, "expires_at": share.expires_at, "speakers": speakers}

    async def accept_share(self, user: User, token: Any) -> list[dict[str, Any]]:
        async with self._lock:
            share = self._share(token)
            del self._shares[share.token]  # single use
            ids = await self._still_shareable(share)
            if not ids:
                raise ApiError(410, "Whoever shared these speakers no longer controls them")
            async with self.db.transaction():
                for sid in ids:
                    await self._add_owner(sid, user.id)
                    if share.mode == "handover" and share.by_user_id != user.id:
                        await self.db.execute(
                            "DELETE FROM speaker_owners WHERE speaker_id=? AND user_id=?", sid, share.by_user_id
                        )
        for sid in ids:
            self._changed(sid, device=True)
        wanted = set(ids)
        return [v for v in await self.list_for(user) if v["id"] in wanted]


def _push(out: SpeakerOutput, client_id: str, want: tuple[int, bool], have: tuple[int, bool]) -> None:
    """Command only what differs (each command is acknowledged with its own state report)."""
    volume = want[0] if want[0] != have[0] else None
    muted = want[1] if want[1] != have[1] else None
    if volume is not None or muted is not None:
        out.set_client_volume(client_id, volume, muted)


def _hello_name(name: Any) -> str:
    if isinstance(name, str):
        with contextlib.suppress(ApiError):
            return clean_name(name)
    return DEFAULT_NAME

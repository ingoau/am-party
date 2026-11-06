"""Party state: queue, requests, fallback playlist, users, live updates."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import random
import time
from collections import OrderedDict, deque
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .db import Database, now
from .models import NotConfigured, ProviderError, Track
from .player.engine import Candidate, NowPlaying
from .security import SlidingWindow, TTLCache, valid_track_id

if TYPE_CHECKING:
    from .auth import User
    from .player.engine import Engine

LOGGER = logging.getLogger(__name__)

TRACK_CACHE_SIZE = 5000
RECENT_FALLBACK = 30
FALLBACK_REFRESH = 3600
HISTORY_KEEP = 1000  # play history rows kept
PREVIOUS_RESTART = 3.0  # "previous" restarts the song once it has played this long
WS_SEND_TIMEOUT = 5.0  # a client that can't take a message this fast is dropped
MIN_POSITION_GAP = 1e-6  # renumber queue positions before floats collapse

# Guest abuse limits. Per-IP limits are deliberately looser than per-guest ones:
# everyone on the party wifi usually shares one public IP.
GUEST_SEARCH_PER_MIN = 30
IP_SEARCH_PER_MIN = 120
TOTAL_GUEST_SEARCH_PER_MIN = 600
IP_MAX_PENDING = 20
IP_REQUEST_COOLDOWN = 2.0
MAX_PENDING_TOTAL = 200
RATE_KEYS_MAX = 20_000  # bound on remembered guests / IPs per limiter


class ApiError(Exception):
    """Error returned to the client as JSON ({"error": message}) with a status code."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Hub:
    """WebSocket fan-out."""

    def __init__(self) -> None:
        self.clients: dict[web.WebSocketResponse, User | None] = {}
        self._closing: set[asyncio.Task] = set()

    async def send_all(self, msg: dict[str, Any], permission: str | None = None) -> None:
        """Send to every allowed client concurrently; one slow client can't stall the rest."""
        data = json.dumps(msg)
        targets = [ws for ws, user in list(self.clients.items()) if _allowed(user, permission)]
        if targets:
            await asyncio.gather(*(self._send(ws, data) for ws in targets))

    async def _send(self, ws: web.WebSocketResponse, data: str) -> None:
        try:
            await asyncio.wait_for(ws.send_str(data), WS_SEND_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - dead or stuck socket
            self.clients.pop(ws, None)
            if isinstance(err, TimeoutError) and not ws.closed:
                # stuck client: close it so it reconnects and gets a fresh state
                task = asyncio.create_task(self._close(ws))
                self._closing.add(task)
                task.add_done_callback(self._closing.discard)

    @staticmethod
    async def _close(ws: web.WebSocketResponse) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.close(code=1013, message=b"too slow"), WS_SEND_TIMEOUT)


def _allowed(user: User | None, permission: str | None) -> bool:
    if permission == "signed_in":
        return user is not None
    if permission:
        return bool(user and user.has(permission))
    return True


class Party:
    def __init__(self, db: Database, apple: Any, hub: Hub) -> None:
        self.db = db
        self.apple = apple
        self.hub = hub
        self.engine: Engine | None = None
        self._settings: dict[str, Any] = {}
        self.queue: list[dict[str, Any]] = []  # [{id, position, track: Track, source, request_id}]
        self.track_cache: OrderedDict[str, Track] = OrderedDict()
        self.fallback_tracks: list[Track] = []
        self.fallback_order: list[int] = []
        self.fallback_ptr = 0
        self.fallback_loaded_at = 0.0
        self.fallback_error: str | None = None
        self.recent_ids: deque[str] = deque(maxlen=RECENT_FALLBACK)
        self.listeners = 0
        self._state_pending = False
        # rate limiting (all bounded LRU/TTL maps; keys are guest ids or client IPs)
        self.ip_search_per_min = IP_SEARCH_PER_MIN
        self.ip_max_pending = IP_MAX_PENDING
        self._guest_last_request: TTLCache[str, float] = TTLCache(RATE_KEYS_MAX, 3600)
        self._ip_last_request: TTLCache[str, float] = TTLCache(RATE_KEYS_MAX, 3600)
        self._ip_requests: TTLCache[str, list[int]] = TTLCache(RATE_KEYS_MAX, 24 * 3600)
        self._guest_searches: SlidingWindow[str] = SlidingWindow(GUEST_SEARCH_PER_MIN, 60, RATE_KEYS_MAX)
        self._ip_searches: SlidingWindow[str] = SlidingWindow(IP_SEARCH_PER_MIN, 60, RATE_KEYS_MAX)
        self._all_searches: SlidingWindow[str] = SlidingWindow(TOTAL_GUEST_SEARCH_PER_MIN, 60, 1)
        self.apple_error: str | None = None
        self.public_url = ""
        self._fallback_lock = asyncio.Lock()
        # fallback tracks played in the current pass through the (shuffled) playlist
        self.fallback_played: set[str] = set()
        self._tasks: set[asyncio.Task] = set()  # background work, awaited on close()

    # -- lifecycle --------------------------------------------------------------
    async def load(self) -> None:
        self._settings = await self.db.get_settings()
        rows = await self.db.fetchall("SELECT * FROM queue ORDER BY position, id")
        self.queue = [self._queue_row(r) for r in rows]
        for q in self.queue:
            self._cache_track(q["track"])
        for r in await self.db.fetchall("SELECT track_id FROM history ORDER BY id DESC LIMIT ?", RECENT_FALLBACK):
            self.recent_ids.appendleft(r["track_id"])

    def _spawn(self, coro: Any, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (err := task.exception()) is not None:
            LOGGER.error("Background task %s failed", task.get_name(), exc_info=err)

    async def close(self) -> None:
        """Cancel and await background work (call before closing the database)."""
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def connect_apple(self) -> None:
        try:
            await self.apple.setup()
            self.apple_error = None
        except NotConfigured as err:
            self.apple_error = str(err) or "Apple Music is not configured"
        except Exception as err:  # noqa: BLE001
            LOGGER.warning("Apple Music setup failed: %s", err)
            self.apple_error = str(err) or err.__class__.__name__
        if not self.apple_error:
            await self.reload_fallback()
        self.changed()

    @staticmethod
    def _queue_row(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": r["id"],
            "position": r["position"],
            "track": Track.from_dict(json.loads(r["track"])),
            "source": r["source"],
            "request_id": r.get("request_id"),
        }

    def settings(self) -> dict[str, Any]:
        return self._settings

    # -- engine hooks -----------------------------------------------------------
    def _inflight(self) -> tuple[set[int], set[str]]:
        infl = self.engine.inflight if self.engine else []
        return (
            {c.queue_item_id for c in infl if c.queue_item_id is not None},
            {c.track.id for c in infl},
        )

    def _available_queue(self) -> list[dict[str, Any]]:
        ids, _ = self._inflight()
        return [q for q in self.queue if q["id"] not in ids]

    def peek_next(self) -> Candidate | None:
        if avail := self._available_queue():
            q = avail[0]
            return Candidate(q["track"], q["id"], q["source"])
        if t := self._peek_fallback():
            return Candidate(t, None, "fallback")
        return None

    def peek_upcoming(self, n: int) -> list[Candidate]:
        out = [Candidate(q["track"], q["id"], q["source"]) for q in self._available_queue()[:n]]
        if len(out) < n and (t := self._peek_fallback()):
            out.append(Candidate(t, None, "fallback"))
        return out

    async def on_track_started(self, np: NowPlaying) -> None:
        cand = np.candidate
        await self._consume(cand)
        await self.db.execute(
            "INSERT INTO history(track_id, track, source, played_at) VALUES(?,?,?,?)",
            cand.track.id,
            json.dumps(cand.track.to_dict()),
            cand.source,
            now(),
        )
        await self.db.execute(
            "DELETE FROM history WHERE id <= "
            "(SELECT id FROM history ORDER BY id DESC LIMIT 1 OFFSET ?)",
            HISTORY_KEEP,
        )
        self.recent_ids.append(cand.track.id)
        LOGGER.info("Now playing: %s - %s [%s]", cand.track.artist, cand.track.title, cand.source)
        self.changed()

    async def on_track_failed(self, cand: Candidate, error: str) -> None:
        await self._consume(cand)
        self.changed()

    async def on_engine_state(self) -> None:
        self.changed()

    async def _consume(self, cand: Candidate) -> None:
        if cand.queue_item_id is not None:
            await self.db.execute("DELETE FROM queue WHERE id=?", cand.queue_item_id)
            self.queue = [q for q in self.queue if q["id"] != cand.queue_item_id]
        elif cand.source == "fallback":
            self._advance_fallback(cand.track.id)

    # -- fallback playlist ------------------------------------------------------
    async def reload_fallback(self) -> None:
        async with self._fallback_lock:
            pid = self._settings.get("fallback_playlist_id")
            if not pid:
                self.fallback_tracks, self.fallback_order = [], []
                self.fallback_error = None
                return
            try:
                tracks = await self.apple.get_playlist_tracks(pid)
                self.fallback_error = None
            except Exception as err:  # noqa: BLE001
                LOGGER.warning("Loading fallback playlist failed: %s", err)
                self.fallback_error = str(err)
                return
            for t in tracks:
                self._cache_track(t)
            self._set_fallback_tracks(tracks)
            self.fallback_loaded_at = time.time()
            LOGGER.info("Loaded fallback playlist with %d tracks", len(tracks))
        if self.engine:
            self.engine.poke()

    def _set_fallback_tracks(self, tracks: list[Track]) -> None:
        """Install a (re)loaded playlist without disturbing the current pass.

        The hourly refresh usually returns the same tracks: then only the Track objects
        are swapped. Otherwise tracks already played this pass stay "played", removed
        ones drop out and new ones join the not-yet-played part of the order.
        """
        old_ids = [t.id for t in self.fallback_tracks]
        new_ids = [t.id for t in tracks]
        if new_ids == old_ids:
            self.fallback_tracks = tracks
            return
        if not old_ids:
            self.fallback_tracks = tracks
            self.fallback_played.clear()
            self._reshuffle()
            return
        index = {tid: i for i, tid in enumerate(new_ids)}
        old_order = [old_ids[i] for i in self.fallback_order]
        done = [tid for tid in old_order[: self.fallback_ptr] if tid in index]
        todo = [tid for tid in old_order[self.fallback_ptr :] if tid in index]
        known = set(done) | set(todo)
        fresh = [tid for tid in new_ids if tid not in known]
        self.fallback_tracks = tracks
        self.fallback_played &= set(new_ids)
        if self._settings.get("fallback_shuffle", True):
            todo += fresh
            random.shuffle(todo)
            order = done + todo
        else:
            order = list(new_ids)  # playlist order; played ones are skipped by the set
        self.fallback_order = [index[tid] for tid in order]
        self.fallback_ptr = len(done) if self._settings.get("fallback_shuffle", True) else 0

    def _reshuffle(self) -> None:
        self.fallback_order = list(range(len(self.fallback_tracks)))
        if self._settings.get("fallback_shuffle", True):
            random.shuffle(self.fallback_order)
        self.fallback_ptr = 0

    def _peek_fallback(self) -> Track | None:
        n = len(self.fallback_order)
        if not n:
            return None
        queued = {q["track"].id for q in self.queue} | self._inflight()[1]
        current = self.engine.now.candidate.track.id if self.engine and self.engine.now else None
        recent = set(self.recent_ids) if n > len(self.recent_ids) + 1 else set()
        played = self.fallback_played
        for i in range(n):
            t = self.fallback_tracks[self.fallback_order[(self.fallback_ptr + i) % n]]
            if t.id in queued or t.id == current or t.id in recent or t.id in played:
                continue
            return t
        for i in range(n):
            t = self.fallback_tracks[self.fallback_order[(self.fallback_ptr + i) % n]]
            if t.id not in queued and t.id != current and t.id not in played:
                return t
        # everything filtered: just take the next one that isn't playing right now
        for i in range(n):
            t = self.fallback_tracks[self.fallback_order[(self.fallback_ptr + i) % n]]
            if t.id != current:
                return t
        return None

    def _advance_fallback(self, track_id: str) -> None:
        n = len(self.fallback_order)
        ids = {t.id for t in self.fallback_tracks}
        if track_id not in ids:
            return  # removed from the playlist by a reload meanwhile
        self.fallback_played.add(track_id)
        if ids <= self.fallback_played:
            # every track had its turn: start a new pass in a new order
            self.fallback_played.clear()
            self._reshuffle()
            return
        for i in range(n):
            idx = (self.fallback_ptr + i) % n
            if self.fallback_tracks[self.fallback_order[idx]].id == track_id:
                self.fallback_ptr = (idx + 1) % n
                return

    async def fallback_refresher(self) -> None:
        while True:
            await asyncio.sleep(FALLBACK_REFRESH)
            if not self.apple_error:
                await self.reload_fallback()

    # -- state ---------------------------------------------------------------
    def changed(self) -> None:
        """Coalesce state broadcasts."""
        if self._state_pending:
            return
        self._state_pending = True
        asyncio.get_running_loop().call_later(0.05, lambda: self._spawn(self._flush(), "state-flush"))

    async def _flush(self) -> None:
        self._state_pending = False
        await self.hub.send_all({"type": "state", "data": self.state()})

    def state(self) -> dict[str, Any]:
        eng = self.engine
        np_obj = None
        if eng and eng.now:
            c = eng.now.candidate
            np_obj = {
                **c.track.to_dict(),
                "duration": eng.now.prepared.duration or c.track.duration,
                "queue_item_id": c.queue_item_id,
                "source": c.source,
                "started_at": eng.now.started_at,
                "position": round(eng.position(), 3),  # frozen while paused
            }
        upcoming = [
            {
                "id": q["id"],
                "track": q["track"].to_dict(),
                "source": q["source"],
                "request_id": q.get("request_id"),
            }
            for q in self.queue
        ]
        # the locked (already mixing) item first
        if eng and eng.locked and eng.locked.queue_item_id is not None:
            upcoming.sort(key=lambda u: 0 if u["id"] == eng.locked.queue_item_id else 1)
        fb = None
        if eng and eng.locked and eng.locked.source == "fallback" and not self.queue:
            fb = eng.locked.track.to_dict()
        elif t := self._peek_fallback():
            fb = t.to_dict()
        return {
            "party_name": self._settings.get("party_name", "Party"),
            "server_time": time.time(),
            "now_playing": np_obj,
            "upcoming": upcoming,
            "up_next_fallback": fb,
            "paused": bool(eng and eng.paused),
            "requests_open": bool(self._settings.get("requests_open", True)),
            "require_approval": bool(self._settings.get("require_approval", True)),
            "listeners": self.listeners,
            "public_url": self.public_url,
        }

    def set_listeners(self, n: int) -> None:
        if n != self.listeners:
            self.listeners = n
            self.changed()

    # -- search / tracks ---------------------------------------------------------
    def _cache_track(self, t: Track) -> None:
        self.track_cache[t.id] = t
        self.track_cache.move_to_end(t.id)
        while len(self.track_cache) > TRACK_CACHE_SIZE:
            self.track_cache.popitem(last=False)

    def _require_apple(self) -> None:
        if not self.apple.configured or self.apple_error:
            raise ApiError(503, "Music search isn't available right now")

    def set_rate_limits(self, ip_search_per_min: int, ip_max_pending: int) -> None:
        self.ip_search_per_min = ip_search_per_min
        self.ip_max_pending = ip_max_pending
        self._ip_searches = SlidingWindow(ip_search_per_min, 60, RATE_KEYS_MAX)

    def _check_search_rate(self, guest: str | None, ip: str | None) -> None:
        """Guest searches: per guest, per client IP and in total (all or nothing)."""
        if not guest and not ip:
            return
        checks: list[tuple[SlidingWindow[str], str]] = [(self._all_searches, "*")]
        if guest:
            checks.append((self._guest_searches, guest))
        if ip:
            checks.append((self._ip_searches, ip))
        if not all(limiter.allowed(key) for limiter, key in checks):
            raise ApiError(429, "Slow down a little, try again in a minute")
        for limiter, key in checks:
            limiter.hit(key)

    async def search(self, q: str, guest: str | None = None, ip: str | None = None) -> list[Track]:
        q = q.strip()[:100]
        if not q:
            return []
        self._require_apple()
        self._check_search_rate(guest, ip)
        try:
            tracks = await self.apple.search_tracks(q, limit=20)
        except ProviderError as err:
            LOGGER.warning("Search failed: %s", err)
            raise ApiError(502, "Search failed, please try again") from err
        for t in tracks:
            self._cache_track(t)
        return tracks

    async def get_track(self, track_id: str) -> Track:
        if t := self.track_cache.get(track_id):
            return t
        if not valid_track_id(track_id):
            raise ApiError(400, "Invalid track id")
        self._require_apple()
        try:
            t = await self.apple.get_track(track_id)
        except ProviderError as err:
            raise ApiError(404, "Song not found") from err
        self._cache_track(t)
        return t

    # -- requests ------------------------------------------------------------------
    @staticmethod
    def guest_label(guest_id: str) -> str:
        return "Guest " + hashlib.sha256(guest_id.encode()).hexdigest()[:4].upper()

    def _request_out(self, r: dict[str, Any], admin: bool) -> dict[str, Any]:
        out = {
            "id": r["id"],
            "track": json.loads(r["track"]),
            "status": r["status"],
            "created_at": r["created_at"],
            "decided_at": r.get("decided_at"),
        }
        if admin:
            out["decided_by"] = r.get("decider_name")
            out["guest_label"] = self.guest_label(r["guest_id"])
        return out

    async def _ip_pending(self, ip: str) -> list[int]:
        """Ids of this IP's requests that are still pending."""
        ids = self._ip_requests.get(ip) or []
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = await self.db.fetchall(
            f"SELECT id FROM requests WHERE status='pending' AND id IN ({marks})", *ids
        )
        return [r["id"] for r in rows]

    async def create_request(
        self, guest_id: str, track_id: str, ip: str | None = None
    ) -> dict[str, Any]:
        s = self._settings
        if not s.get("requests_open", True):
            raise ApiError(403, "Requests are closed right now")
        track = self.track_cache.get(str(track_id))
        if not track:
            raise ApiError(400, "Please search for the song again")
        if any(q["track"].id == track.id for q in self.queue):
            raise ApiError(409, "That song is already in the queue")
        eng = self.engine
        if eng and eng.now and eng.now.candidate.track.id == track.id:
            raise ApiError(409, "That song is playing right now")
        dup = await self.db.fetchone(
            "SELECT id FROM requests WHERE track_id=? AND status='pending'", track.id
        )
        if dup:
            raise ApiError(409, "Someone already requested that song")
        pending = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM requests WHERE guest_id=? AND status='pending'", guest_id
        )
        if pending and pending["n"] >= int(s.get("max_pending_per_guest", 3)):
            raise ApiError(429, "You have too many requests waiting, hang tight")
        total = await self.db.fetchone("SELECT COUNT(*) AS n FROM requests WHERE status='pending'")
        if total and total["n"] >= MAX_PENDING_TOTAL:
            raise ApiError(429, "Lots of requests are waiting already, try again in a bit")
        ip_pending: list[int] = []
        if ip:
            ip_pending = await self._ip_pending(ip)
            if len(ip_pending) >= self.ip_max_pending:
                raise ApiError(429, "Too many requests waiting from your network, hang tight")
        cooldown = float(s.get("request_cooldown_seconds", 20))
        last = self._guest_last_request.get(guest_id) or 0.0
        if time.time() - last < cooldown:
            wait = int(cooldown - (time.time() - last)) + 1
            raise ApiError(429, f"Please wait {wait}s before requesting another song")
        if ip and time.time() - (self._ip_last_request.get(ip) or 0.0) < IP_REQUEST_COOLDOWN:
            raise ApiError(429, "Please wait a moment before requesting another song")
        self._guest_last_request.set(guest_id, time.time())
        if ip:
            self._ip_last_request.set(ip, time.time())
        auto = not s.get("require_approval", True)
        rid = await self.db.execute(
            "INSERT INTO requests(track_id, track, guest_id, status, created_at, decided_at) "
            "VALUES(?,?,?,?,?,?)",
            track.id,
            json.dumps(track.to_dict()),
            guest_id,
            "approved" if auto else "pending",
            now(),
            now() if auto else None,
        )
        if ip and not auto:
            self._ip_requests.set(ip, [*ip_pending[-(self.ip_max_pending * 2) :], rid])
        if auto:
            await self._insert_queue(track, "request", "end", request_id=rid)
        row = await self.db.fetchone("SELECT * FROM requests WHERE id=?", rid)
        assert row
        await self.hub.send_all({"type": "requests_changed"}, "approve_requests")
        return self._request_out(row, admin=False)

    def _queue_positions(self) -> dict[int, tuple[int, float]]:
        """request_id -> (1-based position in upcoming, seconds until it starts)."""
        eng = self.engine
        wait = 0.0
        if eng and eng.now:
            wait = max(0.0, eng.now.prepared.duration - eng.position())
        order = list(self.queue)
        locked = self._locked_id()
        order.sort(key=lambda q: 0 if q["id"] == locked else 1)
        out: dict[int, tuple[int, float]] = {}
        for i, q in enumerate(order):
            if q.get("request_id") is not None:
                out[q["request_id"]] = (i + 1, wait)
            wait += q["track"].duration or 0
        return out

    async def my_requests(self, guest_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM requests WHERE guest_id=? ORDER BY id DESC LIMIT 50", guest_id
        )
        positions = self._queue_positions()
        out = []
        for r in rows:
            item = self._request_out(r, admin=False)
            if r["id"] in positions:
                pos, wait = positions[r["id"]]
                item["queue_position"] = pos
                item["eta_seconds"] = round(wait)
            item["can_withdraw"] = r["status"] == "pending" or (
                r["status"] == "approved" and r["id"] in positions and not self._is_locked_request(r["id"])
            )
            out.append(item)
        return out

    def _is_locked_request(self, request_id: int) -> bool:
        locked = self._locked_id()
        return any(q["id"] == locked and q.get("request_id") == request_id for q in self.queue)

    async def withdraw_request(self, guest_id: str, rid: int) -> None:
        row = await self.db.fetchone("SELECT * FROM requests WHERE id=? AND guest_id=?", rid, guest_id)
        if not row:
            raise ApiError(404, "Request not found")
        if row["status"] == "approved":
            item = next((q for q in self.queue if q.get("request_id") == rid), None)
            if not item:
                raise ApiError(409, "That song has already played")
            if item["id"] == self._locked_id():
                raise ApiError(409, "Too late, that song is about to start")
            async with self.db.transaction():
                await self.db.execute("DELETE FROM queue WHERE id=?", item["id"])
                await self.db.execute(
                    "UPDATE requests SET status='withdrawn', decided_at=? WHERE id=?", now(), rid
                )
            self.queue = [q for q in self.queue if q["id"] != item["id"]]
            self._after_queue_change()
        elif row["status"] != "pending":
            raise ApiError(409, f"Request was already {row['status']}")
        elif not await self.db.execute_rowcount(
            "UPDATE requests SET status='withdrawn', decided_at=? WHERE id=? AND status='pending'",
            now(),
            rid,
        ):
            raise ApiError(409, "That request was just decided, please refresh")
        await self.hub.send_all({"type": "requests_changed"}, "approve_requests")

    async def list_requests(self, status: str | None) -> list[dict[str, Any]]:
        sql = (
            "SELECT r.*, u.name AS decider_name FROM requests r "
            "LEFT JOIN users u ON u.id = r.decided_by"
        )
        args: list[Any] = []
        if status and status != "all":
            sql += " WHERE r.status=?"
            args.append(status)
        order = "ASC" if status == "pending" else "DESC"
        sql += f" ORDER BY r.id {order} LIMIT 200"
        rows = await self.db.fetchall(sql, *args)
        return [self._request_out(r, admin=True) for r in rows]

    async def approve_all(self, user: User | None, position: str = "end") -> int:
        """Approve every pending request, oldest first."""
        rows = await self.db.fetchall("SELECT * FROM requests WHERE status='pending' ORDER BY id")
        if position == "next":
            rows.reverse()  # inserting each at "next" keeps oldest first
        approved = 0
        for row in rows:
            async with self.db.transaction():
                # only if still pending: the guest may have withdrawn it meanwhile
                if not await self.db.execute_rowcount(
                    "UPDATE requests SET status='approved', decided_by=?, decided_at=? "
                    "WHERE id=? AND status='pending'",
                    user.id if user else None,
                    now(),
                    row["id"],
                ):
                    continue
                approved += 1
                track = Track.from_dict(json.loads(row["track"]))
                if not any(q["track"].id == track.id for q in self.queue):
                    await self._insert_queue(track, "request", position, request_id=row["id"], user=user)
        if rows:
            await self.hub.send_all({"type": "requests_changed"}, "approve_requests")
        return approved

    async def set_require_approval(self, enabled: bool, user: User | None) -> int:
        """Turn approval on/off. Turning it off approves everything waiting."""
        self._settings = await self.db.set_settings({"require_approval": bool(enabled)})
        approved = 0 if enabled else await self.approve_all(user)
        self.changed()
        await self.hub.send_all({"type": "requests_changed"}, "approve_requests")
        return approved

    async def decide_request(self, rid: int, user: User, approve: bool, position: str = "end") -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM requests WHERE id=?", rid)
        if not row:
            raise ApiError(404, "Request not found")
        if row["status"] != "pending":
            raise ApiError(409, f"Request was already {row['status']}")
        status = "approved" if approve else "rejected"
        async with self.db.transaction():
            if not await self.db.execute_rowcount(
                "UPDATE requests SET status=?, decided_by=?, decided_at=? "
                "WHERE id=? AND status='pending'",
                status,
                user.id,
                now(),
                rid,
            ):
                cur = await self.db.fetchone("SELECT status FROM requests WHERE id=?", rid)
                raise ApiError(409, f"Request was already {(cur or {}).get('status', 'removed')}")
            if approve:
                track = Track.from_dict(json.loads(row["track"]))
                if not any(q["track"].id == track.id for q in self.queue):
                    await self._insert_queue(track, "request", position, request_id=rid, user=user)
        await self.hub.send_all({"type": "requests_changed"}, "approve_requests")
        out = await self.db.fetchone(
            "SELECT r.*, u.name AS decider_name FROM requests r "
            "LEFT JOIN users u ON u.id = r.decided_by WHERE r.id=?",
            rid,
        )
        assert out
        return self._request_out(out, admin=True)

    # -- queue -------------------------------------------------------------------
    def _locked_id(self) -> int | None:
        eng = self.engine
        return eng.locked.queue_item_id if eng and eng.locked else None

    async def _insert_queue(
        self,
        track: Track,
        source: str,
        position: str,
        request_id: int | None = None,
        user: User | None = None,
    ) -> None:
        async with self.db.transaction():
            if position == "next" and self.queue:
                locked = self._locked_id()
                # never jump ahead of the track that is already being mixed in
                first = self.queue[0]
                if locked == first["id"] and len(self.queue) > 1:
                    if self.queue[1]["position"] - first["position"] < MIN_POSITION_GAP:
                        await self._renumber_queue()
                    pos = (first["position"] + self.queue[1]["position"]) / 2
                elif locked == first["id"]:
                    pos = first["position"] + 1
                else:
                    pos = first["position"] - 1
            else:
                pos = (self.queue[-1]["position"] + 1) if self.queue else 1.0
            qid = await self.db.execute(
                "INSERT INTO queue(position, track_id, track, source, request_id, added_by, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                pos,
                track.id,
                json.dumps(track.to_dict()),
                source,
                request_id,
                user.id if user else None,
                now(),
            )
        self.queue.append(
            {"id": qid, "position": pos, "track": track, "source": source, "request_id": request_id}
        )
        self.queue.sort(key=lambda q: (q["position"], q["id"]))
        self._after_queue_change()

    async def _renumber_queue(self) -> None:
        """Reset positions to 1..n (repeated "play next" halves the same gap)."""
        async with self.db.transaction():
            for i, q in enumerate(self.queue):
                q["position"] = float(i + 1)
                await self.db.execute("UPDATE queue SET position=? WHERE id=?", q["position"], q["id"])

    def _after_queue_change(self) -> None:
        if self.engine:
            self.engine.poke()
        self.changed()

    async def add_to_queue(self, track_id: str, position: str, user: User) -> None:
        track = await self.get_track(track_id)
        await self._insert_queue(track, "admin", position, user=user)

    async def previous(self) -> str:
        """Restart the current song, or go back to the one before it.

        Within PREVIOUS_RESTART seconds of a song's start this goes back: the previous
        song and then the current one are put at the front of the queue, so "skip"
        afterwards returns to where the party was. Their history rows are dropped
        (they are logged again when they play), so repeated presses keep going back.
        Returns "restart" or "previous".
        """
        eng = self.engine
        if not eng or not eng.now:
            raise ApiError(409, "Nothing is playing")
        rows = await self.db.fetchall("SELECT * FROM history ORDER BY id DESC LIMIT 2")
        cur = eng.now.candidate
        if eng.position() > PREVIOUS_RESTART or len(rows) < 2 or rows[0]["track_id"] != cur.track.id:
            eng.seek(0)
            self.changed()
            return "restart"
        prev = Track.from_dict(json.loads(rows[1]["track"]))
        first = self.queue[0]["position"] if self.queue else 1.0
        items = [(prev, rows[1]["source"], first - 2), (cur.track, cur.source, first - 1)]
        added: list[dict[str, Any]] = []
        async with self.db.transaction():
            await self.db.execute("DELETE FROM history WHERE id IN (?,?)", rows[0]["id"], rows[1]["id"])
            for track, source, pos in items:
                qid = await self.db.execute(
                    "INSERT INTO queue(position, track_id, track, source, request_id, added_by, created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    pos,
                    track.id,
                    json.dumps(track.to_dict()),
                    source,
                    None,
                    None,
                    now(),
                )
                added.append({"id": qid, "position": pos, "track": track, "source": source, "request_id": None})
        self.queue += added
        self.queue.sort(key=lambda q: (q["position"], q["id"]))
        # skip even past a track that is already locked in / being mixed in
        eng.skip(requeue=True)
        self._after_queue_change()
        return "previous"

    async def remove_from_queue(self, qid: int) -> None:
        if not any(q["id"] == qid for q in self.queue):
            raise ApiError(404, "Queue item not found")
        if qid == self._locked_id():
            raise ApiError(409, "That song is already being mixed in; skip it once it starts")
        await self.db.execute("DELETE FROM queue WHERE id=?", qid)
        self.queue = [q for q in self.queue if q["id"] != qid]
        self._after_queue_change()

    async def move_in_queue(self, qid: int, index: int) -> None:
        item = next((q for q in self.queue if q["id"] == qid), None)
        if not item:
            raise ApiError(404, "Queue item not found")
        locked = self._locked_id()
        if qid == locked:
            raise ApiError(409, "That song is already being mixed in")
        rest = [q for q in self.queue if q["id"] != qid]
        min_index = 1 if rest and rest[0]["id"] == locked else 0
        index = max(min_index, min(index, len(rest)))
        rest.insert(index, item)
        async with self.db.transaction():
            for i, q in enumerate(rest):
                await self.db.execute("UPDATE queue SET position=? WHERE id=?", float(i + 1), q["id"])
        for i, q in enumerate(rest):
            q["position"] = float(i + 1)
        self.queue = rest
        self._after_queue_change()

    # -- settings ----------------------------------------------------------------
    async def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        for key, val in values.items():
            if key == "crossfade_mode":
                if val not in ("smart", "standard", "off"):
                    raise ApiError(400, "Invalid crossfade mode")
            elif key == "crossfade_duration":
                val = _number(key, val, 1, 15)
            elif key == "max_pending_per_guest":
                val = _number(key, val, 1, 100)  # 0 would silently block every guest
            elif key == "request_cooldown_seconds":
                val = _number(key, val, 0, 3600)
            elif key in ("requests_open", "fallback_shuffle", "require_approval"):
                if not isinstance(val, bool):
                    raise ApiError(400, f"{key} must be true or false")
            elif key == "party_name":
                if val is not None and not isinstance(val, str):
                    raise ApiError(400, "party_name must be a string")
                val = (val or "").strip()[:60] or "Party"
            elif key in ("fallback_playlist_id", "fallback_playlist_name"):
                if val is not None and not isinstance(val, str):
                    raise ApiError(400, f"{key} must be a string or null")
                val = (val.strip()[:200] or None) if val else None
            clean[key] = val
        if clean.get("require_approval") is False and self._settings.get("require_approval", True):
            clean.pop("require_approval")
            await self.set_require_approval(False, None)
        old_pl = self._settings.get("fallback_playlist_id")
        old_shuffle = self._settings.get("fallback_shuffle")
        self._settings = await self.db.set_settings(clean)
        if self._settings.get("fallback_playlist_id") != old_pl:
            self.fallback_tracks, self.fallback_order, self.fallback_ptr = [], [], 0
            self.fallback_played.clear()
            self._spawn(self.reload_fallback(), "reload-fallback")
        elif self._settings.get("fallback_shuffle") != old_shuffle:
            self._reshuffle()
        if self.engine:
            self.engine.poke()
        self.changed()
        return self._settings


def _number(key: str, val: Any, lo: int, hi: int) -> int:
    """Strict numeric setting: a finite JSON number (not a bool/string/null) in [lo, hi]."""
    if isinstance(val, bool) or not isinstance(val, int | float) or not math.isfinite(val):
        raise ApiError(400, f"{key} must be a number")
    if not lo <= val <= hi:
        raise ApiError(400, f"{key} must be between {lo} and {hi}")
    return round(val)

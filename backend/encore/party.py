"""Party state: queue, requests, fallback playlist, users, live updates."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from collections import OrderedDict, deque
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .db import Database, now
from .models import NotConfigured, ProviderError, Track
from .player.engine import Candidate, NowPlaying

if TYPE_CHECKING:
    from .auth import User
    from .player.engine import Engine

LOGGER = logging.getLogger(__name__)

TRACK_CACHE_SIZE = 5000
RECENT_FALLBACK = 30
FALLBACK_REFRESH = 3600


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

    async def send_all(self, msg: dict[str, Any], permission: str | None = None) -> None:
        data = json.dumps(msg)
        for ws, user in list(self.clients.items()):
            if permission and not (user and getattr(user, permission)):
                continue
            try:
                await ws.send_str(data)
            except (ConnectionResetError, RuntimeError):
                self.clients.pop(ws, None)


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
        self._guest_last_request: dict[str, float] = {}
        self._guest_searches: dict[str, deque[float]] = {}
        self.apple_error: str | None = None
        self.public_url = ""
        self._fallback_lock = asyncio.Lock()

    # -- lifecycle --------------------------------------------------------------
    async def load(self) -> None:
        self._settings = await self.db.get_settings()
        rows = await self.db.fetchall("SELECT * FROM queue ORDER BY position, id")
        self.queue = [self._queue_row(r) for r in rows]
        for q in self.queue:
            self._cache_track(q["track"])
        for r in await self.db.fetchall("SELECT track_id FROM history ORDER BY id DESC LIMIT ?", RECENT_FALLBACK):
            self.recent_ids.appendleft(r["track_id"])

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
            self.fallback_tracks = tracks
            for t in tracks:
                self._cache_track(t)
            self._reshuffle()
            self.fallback_loaded_at = time.time()
            LOGGER.info("Loaded fallback playlist with %d tracks", len(tracks))
        if self.engine:
            self.engine.poke()

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
        for i in range(n):
            t = self.fallback_tracks[self.fallback_order[(self.fallback_ptr + i) % n]]
            if t.id in queued or t.id == current or t.id in recent:
                continue
            return t
        # everything filtered: just take the next one that isn't playing right now
        for i in range(n):
            t = self.fallback_tracks[self.fallback_order[(self.fallback_ptr + i) % n]]
            if t.id != current:
                return t
        return None

    def _advance_fallback(self, track_id: str) -> None:
        n = len(self.fallback_order)
        for i in range(n):
            idx = (self.fallback_ptr + i) % n
            if self.fallback_tracks[self.fallback_order[idx]].id == track_id:
                self.fallback_ptr = idx + 1
                if self.fallback_ptr >= n:
                    self._reshuffle()
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
        asyncio.get_running_loop().call_later(0.05, lambda: asyncio.ensure_future(self._flush()))

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
            }
        upcoming = [
            {"id": q["id"], "track": q["track"].to_dict(), "source": q["source"]}
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
            "party_name": self._settings.get("party_name", "Encore"),
            "server_time": time.time(),
            "now_playing": np_obj,
            "upcoming": upcoming,
            "up_next_fallback": fb,
            "paused": bool(eng and eng.paused),
            "requests_open": bool(self._settings.get("requests_open", True)),
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

    async def search(self, q: str, guest: str | None = None) -> list[Track]:
        q = q.strip()[:100]
        if not q:
            return []
        self._require_apple()
        if guest:
            win = self._guest_searches.setdefault(guest, deque(maxlen=30))
            t = time.time()
            while win and t - win[0] > 60:
                win.popleft()
            if len(win) >= 30:
                raise ApiError(429, "Slow down a little, try again in a minute")
            win.append(t)
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

    async def create_request(self, guest_id: str, track_id: str) -> dict[str, Any]:
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
        cooldown = float(s.get("request_cooldown_seconds", 20))
        last = self._guest_last_request.get(guest_id, 0)
        if time.time() - last < cooldown:
            wait = int(cooldown - (time.time() - last)) + 1
            raise ApiError(429, f"Please wait {wait}s before requesting another song")
        self._guest_last_request[guest_id] = time.time()
        rid = await self.db.execute(
            "INSERT INTO requests(track_id, track, guest_id, status, created_at) VALUES(?,?,?,?,?)",
            track.id,
            json.dumps(track.to_dict()),
            guest_id,
            "pending",
            now(),
        )
        row = await self.db.fetchone("SELECT * FROM requests WHERE id=?", rid)
        assert row
        await self.hub.send_all({"type": "requests_changed"}, "can_approve")
        return self._request_out(row, admin=False)

    async def my_requests(self, guest_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM requests WHERE guest_id=? ORDER BY id DESC LIMIT 50", guest_id
        )
        return [self._request_out(r, admin=False) for r in rows]

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

    async def decide_request(self, rid: int, user: User, approve: bool, position: str = "end") -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM requests WHERE id=?", rid)
        if not row:
            raise ApiError(404, "Request not found")
        if row["status"] != "pending":
            raise ApiError(409, f"Request was already {row['status']}")
        status = "approved" if approve else "rejected"
        await self.db.execute(
            "UPDATE requests SET status=?, decided_by=?, decided_at=? WHERE id=?",
            status,
            user.id,
            now(),
            rid,
        )
        if approve:
            track = Track.from_dict(json.loads(row["track"]))
            if not any(q["track"].id == track.id for q in self.queue):
                await self._insert_queue(track, "request", position, request_id=rid, user=user)
        await self.hub.send_all({"type": "requests_changed"}, "can_approve")
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
        if position == "next" and self.queue:
            locked = self._locked_id()
            # never jump ahead of the track that is already being mixed in
            first = self.queue[0]
            if locked == first["id"] and len(self.queue) > 1:
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

    def _after_queue_change(self) -> None:
        if self.engine:
            self.engine.poke()
        self.changed()

    async def add_to_queue(self, track_id: str, position: str, user: User) -> None:
        track = await self.get_track(track_id)
        await self._insert_queue(track, "admin", position, user=user)

    async def remove_from_queue(self, qid: int) -> None:
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
        for i, q in enumerate(rest):
            q["position"] = float(i + 1)
            await self.db.c.execute("UPDATE queue SET position=? WHERE id=?", (q["position"], q["id"]))
        await self.db.c.commit()
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
                val = max(1, min(15, int(val)))
            elif key in ("max_pending_per_guest", "request_cooldown_seconds"):
                val = max(0, min(3600, int(val)))
            elif key in ("requests_open", "fallback_shuffle"):
                val = bool(val)
            elif key == "party_name":
                val = str(val).strip()[:60] or "Encore"
            elif key in ("fallback_playlist_id", "fallback_playlist_name"):
                val = (str(val).strip() or None) if val else None
            clean[key] = val
        old_pl = self._settings.get("fallback_playlist_id")
        old_shuffle = self._settings.get("fallback_shuffle")
        self._settings = await self.db.set_settings(clean)
        if self._settings.get("fallback_playlist_id") != old_pl:
            asyncio.create_task(self.reload_fallback())
        elif self._settings.get("fallback_shuffle") != old_shuffle:
            self._reshuffle()
        if self.engine:
            self.engine.poke()
        self.changed()
        return self._settings

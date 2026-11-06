"""Application wiring."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from . import api
from .auth import AuthManager
from .config import Config
from .db import Database, now
from .party import Hub, Party
from .security import ConnectionCounter
from .player.engine import Engine
from .player.prepare import TrackPreparer, dumps_analysis

LOGGER = logging.getLogger("encore")

ANALYSIS_VERSION = 1
# Live-update sockets. Every guest phone holds one and a whole party on one wifi
# usually shares a public IP, so the per-IP cap is generous.
WS_MAX_PER_IP = 100
WS_MAX_TOTAL = 500


class Encore:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.db = Database(config.data_dir / "encore.db")
        self.hub = Hub()
        self.http: aiohttp.ClientSession
        self.auth: AuthManager
        self.apple: Any
        self.party: Party
        self.output: Any
        self.engine: Engine
        self.analysis_worker: Any = None
        self.smartfades_error: str | None = None
        self._tasks: list[asyncio.Task] = []
        # /api/ws admission (per client IP, total) and which session each socket uses
        self.ws_conns = ConnectionCounter(per_key=WS_MAX_PER_IP, total=WS_MAX_TOTAL)
        self.ws_sessions: dict[web.WebSocketResponse, str] = {}

    # -- lifecycle ---------------------------------------------------------------
    async def start(self) -> None:
        await self.db.open()
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        self.auth = AuthManager(self.config, self.db, self.http)
        await self.auth.warn_if_unclaimed()

        from .providers.apple_music import AppleMusicClient

        self.apple = AppleMusicClient(
            self.http,
            developer_token=await self.developer_token(),
            user_token=await self.user_token(),
            widevine_client_id=_read_bytes(self.config.widevine_client_id_path),
            widevine_private_key=_read_bytes(self.config.widevine_private_key_path),
            logger=logging.getLogger("encore.apple_music"),
        )
        self.party = Party(self.db, self.apple, self.hub)
        self.party.public_url = self.config.public_url
        self.party.set_rate_limits(self.config.ip_search_per_min, self.config.ip_max_pending)
        await self.party.load()

        build_transition = None
        tail_seconds = head_seconds = lambda mode, dur: 0.0  # noqa: E731
        try:
            from . import smartfades

            build_transition = smartfades.build_transition
            tail_seconds = smartfades.tail_seconds
            head_seconds = smartfades.head_seconds
            self.analysis_worker = smartfades.AnalysisWorker(logger=logging.getLogger("encore.smartfades"))
            # loading torch + models takes a while: don't hold up readiness for it
            self._spawn(self._start_analysis(), "analysis-start")
        except Exception as err:  # noqa: BLE001
            LOGGER.exception("Smart fades unavailable")
            self.smartfades_error = str(err)

        from .player.sendspin_out import SendspinOutput

        self.output = SendspinOutput(
            self.http,
            name=self.party.settings().get("party_name") or "Encore",
            port=self.config.sendspin_port,  # native LAN listener only if configured
            max_listeners=self.config.max_listeners,
        )
        self.output.on_listeners_changed = self.party.set_listeners
        await self.output.start()

        preparer = TrackPreparer(
            self.config.data_dir / "cache",
            resolve_stream=self.apple.get_stream,
            analysis_worker=self.analysis_worker,
            load_analysis=self._load_analysis,
            save_analysis=self._save_analysis,
            want_analysis=lambda: self.party.settings().get("crossfade_mode") == "smart",
        )
        self.engine = Engine(
            self.output,
            preparer,
            self.party,
            build_transition,
            tail_seconds,
            head_seconds,
        )
        self.party.engine = self.engine
        await self.engine.start()
        self._spawn(self.party.connect_apple(), "connect-apple")
        self._spawn(self.party.fallback_refresher(), "fallback-refresher")
        self._spawn(self._housekeeping(), "housekeeping")
        if not self.config.oidc_enabled:
            LOGGER.warning("OIDC is not configured (OIDC_ISSUER / OIDC_CLIENT_ID): nobody can sign in to the admin UI")
        if not self.apple.configured:
            LOGGER.warning("Apple Music is not configured: search and playback are unavailable until tokens are set")
        LOGGER.info("Encore running on %s", self.config.public_url)

    def _spawn(self, coro: Any, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._tasks.append(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        if task in self._tasks:
            self._tasks.remove(task)
        if not task.cancelled() and (err := task.exception()) is not None:
            LOGGER.error("Task %s failed", task.get_name(), exc_info=err)

    async def _start_analysis(self) -> None:
        worker = self.analysis_worker
        await worker.start()
        if not worker.available:
            self.smartfades_error = worker.error

    async def stop(self) -> None:
        # cancel and *await* background work before tearing down what it uses
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if getattr(self, "party", None):
            await self.party.close()
        if getattr(self, "engine", None):
            await self.engine.stop()
            await self.engine.preparer.close()  # kills ffmpeg, then deletes cache files
        if getattr(self, "output", None):
            await self.output.stop()
        if self.analysis_worker:
            await self.analysis_worker.stop()
        for ws in list(self.hub.clients):
            await ws.close()
        await self.http.close()
        await self.db.close()

    async def _housekeeping(self) -> None:
        while True:
            try:
                await self.db.execute("DELETE FROM sessions WHERE expires_at < ?", now())
                await self.db.execute(
                    "DELETE FROM requests WHERE status != 'pending' AND created_at < ?",
                    now() - 30 * 86400,
                )
            except Exception:  # noqa: BLE001 - keep the loop alive
                LOGGER.exception("Housekeeping failed")
            await asyncio.sleep(3600)

    # -- analysis cache ------------------------------------------------------------
    async def _load_analysis(self, track_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT data FROM analysis WHERE track_id=? AND version=?", track_id, ANALYSIS_VERSION
        )
        return json.loads(row["data"]) if row else None

    async def _save_analysis(self, track_id: str, data: dict[str, Any]) -> None:
        await self.db.execute(
            "INSERT INTO analysis(track_id, version, data, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(track_id) DO UPDATE SET version=excluded.version, data=excluded.data, "
            "created_at=excluded.created_at",
            track_id,
            ANALYSIS_VERSION,
            dumps_analysis(data),
            now(),
        )

    # -- apple music credentials -----------------------------------------------------
    # Tokens set from the admin UI (stored in the DB) win over env/secret files, so a
    # fresh MusicKit sign-in always takes effect.
    async def developer_token(self) -> str | None:
        row = await self.db.get_credential("apple_developer_token")
        return row[0] if row else self.config.apple_music_developer_token

    async def user_token(self) -> str | None:
        row = await self.db.get_credential("apple_user_token")
        return row[0] if row else self.config.apple_music_user_token

    async def update_apple_credentials(
        self, developer_token: str | None = None, user_token: str | None = None
    ) -> None:
        for value in (developer_token, user_token):
            if value is not None and not isinstance(value, str):
                raise TypeError("Apple Music tokens must be strings")
        if developer_token is not None:
            await self.db.set_credential("apple_developer_token", developer_token.strip() or None)
        if user_token is not None:
            await self.db.set_credential("apple_user_token", user_token.strip() or None)
        await self.apple.set_credentials(
            developer_token=await self.developer_token(), user_token=await self.user_token()
        )
        await self.party.connect_apple()

    async def apple_status(self) -> dict[str, Any]:
        from .providers.apple_music import developer_token_expiry

        dev_row = await self.db.get_credential("apple_developer_token")
        user_row = await self.db.get_credential("apple_user_token")
        dev = await self.developer_token()
        return {
            "configured": bool(self.apple.configured),
            "ok": bool(self.apple.configured) and not self.party.apple_error,
            "error": self.party.apple_error,
            "storefront": getattr(self.apple, "storefront", None),
            "developer_token_source": "db"
            if dev_row
            else ("env" if self.config.apple_music_developer_token else None),
            "developer_token_expires_at": developer_token_expiry(dev) if dev else None,
            "user_token_source": "db"
            if user_row
            else ("env" if self.config.apple_music_user_token else None),
            "user_token_set_at": user_row[1] if user_row else None,
            "widevine": bool(
                self.config.widevine_client_id_path and self.config.widevine_private_key_path
            ),
        }

    def status(self) -> dict[str, Any]:
        worker = self.analysis_worker
        return {
            "apple_music": {
                "configured": bool(self.apple.configured),
                "ok": bool(self.apple.configured) and not self.party.apple_error,
                "error": self.party.apple_error,
            },
            "smart_fades": {
                "available": bool(worker and worker.available),
                "error": self.smartfades_error or (worker.error if worker else None),
            },
            "fallback": {
                "tracks": len(self.party.fallback_tracks),
                "error": self.party.fallback_error,
            },
            "listeners": self.party.listeners,
            "engine": self.engine.status(),
        }

    async def close_session_sockets(self, session_id: str) -> None:
        """Close live-update sockets opened with a session that just ended (logout)."""
        for ws, sid in list(self.ws_sessions.items()):
            if sid and secrets.compare_digest(sid, session_id):
                self.hub.clients.pop(ws, None)
                self.ws_sessions.pop(ws, None)
                await ws.close(code=4001, message=b"signed out")

    async def refresh_ws_users(self) -> None:
        """Re-resolve permissions of connected websocket users after a grant change."""
        from .auth import User

        for ws, user in list(self.hub.clients.items()):
            if user:
                row = await self.db.fetchone("SELECT * FROM users WHERE id=?", user.id)
                self.hub.clients[ws] = User.from_row(row) if row else None


def _read_bytes(path: Path | None) -> bytes | None:
    if path and path.is_file():
        return path.read_bytes()
    return None


# -- static SPA ------------------------------------------------------------------------
def static_routes(app: web.Application, static_dir: Path) -> None:
    index = static_dir / "index.html"

    async def spa(request: web.Request) -> web.StreamResponse:
        rel = request.match_info.get("path", "")
        if rel.startswith(("api/", "auth/", "sendspin")):
            raise web.HTTPNotFound()
        candidate = (static_dir / rel).resolve()
        if rel and candidate.is_file() and static_dir.resolve() in candidate.parents:
            headers = {}
            if "/assets/" in f"/{rel}":
                headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return web.FileResponse(candidate, headers=headers)
        if index.is_file():
            return web.FileResponse(index, headers={"Cache-Control": "no-cache"})
        return web.Response(text="Encore frontend not built", status=503)

    app.router.add_get("/{path:.*}", spa)


def create_app(config: Config) -> web.Application:
    app = web.Application(middlewares=[api.error_middleware, api.security_headers])
    encore = Encore(config)
    app["encore"] = encore
    app.add_routes(api.routes)
    static_routes(app, config.static_dir)

    async def on_startup(_: web.Application) -> None:
        await encore.start()

    async def on_cleanup(_: web.Application) -> None:
        await encore.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app

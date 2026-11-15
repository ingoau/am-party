"""HTTP + WebSocket routes."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import re
import secrets
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from .audit import ACTOR_KINDS, track_label, track_ref
from .auth import SESSION_COOKIE, client_ip, guest_id, set_guest_cookie
from .party import ApiError
from .security import valid_track_id

if TYPE_CHECKING:
    from .app import AmParty

LOGGER = logging.getLogger(__name__)

routes = web.RouteTableDef()


def enc(request: web.Request) -> AmParty:
    return request.app["am_party"]


async def body(request: web.Request) -> dict[str, Any]:
    if request.content_type != "application/json":
        raise ApiError(415, "Content-Type must be application/json")
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise ApiError(400, "Invalid JSON") from err
    if not isinstance(data, dict):
        raise ApiError(400, "Expected a JSON object")
    return data


def int_param(request: web.Request, name: str) -> int:
    try:
        return int(request.match_info[name])
    except ValueError as err:
        raise ApiError(400, f"Invalid {name}") from err


@web.middleware
async def error_middleware(request: web.Request, handler):  # type: ignore[no-untyped-def]
    is_api = request.path.startswith("/api/")
    try:
        return await handler(request)
    except ApiError as err:
        return web.json_response({"error": err.message}, status=err.status)
    except web.HTTPException as err:
        if is_api and err.status >= 400:
            return web.json_response({"error": err.text or err.reason}, status=err.status)
        raise
    except Exception:
        LOGGER.exception("Unhandled error on %s %s", request.method, request.path)
        if is_api:
            return web.json_response({"error": "Something went wrong"}, status=500)
        raise


# The SPA: no inline scripts (vite emits a single module script), self-hosted fonts,
# QR codes are inline SVG, artwork from Apple's CDN. The Opus decoder is wasm2js
# (plain JS), so no 'wasm-unsafe-eval'. Tv's iOS audio unlock uses a data: URI.
CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: https://*.mzstatic.com",
        "font-src 'self'",
        "media-src 'self' data: blob:",
        "connect-src 'self' ws: wss:",
        "worker-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)
PERMISSIONS_POLICY = "camera=(self), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()"


def _apply_security_headers(request: web.Request, headers: Any) -> None:
    headers.setdefault("Content-Security-Policy", CSP)
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "same-origin")
    headers.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
    if enc(request).config.public_url.startswith("https://"):
        headers.setdefault("Strict-Transport-Security", "max-age=31536000")


@web.middleware
async def security_headers(request: web.Request, handler):  # type: ignore[no-untyped-def]
    try:
        resp = await handler(request)
    except web.HTTPException as err:
        _apply_security_headers(request, err.headers)
        raise
    if not resp.prepared:  # websockets / streamed responses already sent their headers
        _apply_security_headers(request, resp.headers)
    return resp


def normalise_origin(value: str | None) -> str | None:
    """``scheme://host[:port]`` lower-cased, default ports dropped; None if not http(s)."""
    if not value:
        return None
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https", "ws", "wss") or not parts.hostname:
        return None
    scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port and (scheme, port) not in (("http", 80), ("https", 443)):
        host = f"{host}:{port}"
    return f"{scheme}://{host}"


def allowed_origins(request: web.Request) -> set[str]:
    """The configured public origin, plus this request's own origin (LAN access by IP).

    The request's own origin only counts when its host differs from the public one, so
    e.g. ``http://party.example.com`` isn't accepted for ``https://party.example.com``
    just because a TLS-terminating proxy forwards plain http.
    """
    out = set()
    pub = normalise_origin(enc(request).config.public_url)
    if pub:
        out.add(pub)
    own = normalise_origin(f"{request.scheme}://{request.host}")
    if own and (not pub or urlsplit(own).netloc != urlsplit(pub).netloc):
        out.add(own)
    return out


def origin_allowed(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    norm = normalise_origin(origin)
    return norm is not None and norm in allowed_origins(request)


def csrf_check(request: web.Request) -> None:
    """Reject cross-site state-changing requests (cookies are SameSite=Lax, belt and braces).

    The Origin must exactly match the public origin or the request's own origin. Without
    an Origin header, the browser's Sec-Fetch-Site must say same-origin (or "none", a
    user-initiated navigation); requests carrying neither header are refused.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if "Origin" in request.headers:
        if not origin_allowed(request):
            raise ApiError(403, "Cross-site request blocked")
        return
    if request.headers.get("Sec-Fetch-Site") not in ("same-origin", "none"):
        raise ApiError(403, "Cross-site request blocked")


def track_id_param(data: dict[str, Any]) -> str:
    track_id = data.get("track_id")
    if isinstance(track_id, int) and not isinstance(track_id, bool):
        track_id = str(track_id)
    if not track_id:
        raise ApiError(400, "track_id is required")
    if not valid_track_id(track_id):
        raise ApiError(400, "Invalid track_id")
    return track_id


def guest(request: web.Request) -> tuple[str, bool]:
    return guest_id(request, enc(request).config.session_secret)


def set_guest(resp: web.StreamResponse, request: web.Request, gid: str) -> None:
    cfg = enc(request).config
    set_guest_cookie(resp, gid, cfg.cookie_secure, cfg.session_secret)


def ip_of(request: web.Request) -> str:
    return client_ip(request, enc(request).config.trusted_proxy)


async def audit(request: web.Request, action: str, user: Any, **kw: Any) -> None:
    """Record a staff action in the audit log."""
    await enc(request).party.audit.record(action, user=user, **kw)


def speaker_targets(speakers: list[dict[str, Any]]) -> dict[str, Any]:
    names = [s["name"] for s in speakers]
    return {
        "target_type": "speaker",
        "target_id": ",".join(s["id"] for s in speakers) or None,
        "target_label": ", ".join(names) if len(names) <= 5 else f"{len(names)} speakers",
    }


WS_MAX_MSG = 4096
WS_PING_INTERVAL = 0.5  # pings faster than 2/s are ignored


# --------------------------------------------------------------------------- public
@routes.get("/api/state")
async def get_state(request: web.Request) -> web.Response:
    return web.json_response(enc(request).party.state())


@routes.get("/api/ws")
async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    e = enc(request)
    if not origin_allowed(request):
        raise web.HTTPForbidden(text="Cross-site WebSocket blocked")
    ip = ip_of(request)
    if not e.ws_conns.acquire(ip):
        raise web.HTTPTooManyRequests(text="Too many live connections")
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=WS_MAX_MSG)
    try:
        await ws.prepare(request)
        user = await e.auth.current_user(request)
        e.hub.clients[ws] = user
        if user is not None:
            e.ws_sessions[ws] = request.cookies.get(SESSION_COOKIE, "")
        await ws.send_str(json.dumps({"type": "state", "data": e.party.state()}))
        last_ping = 0.0
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict) and data.get("type") == "ping":
                    t = time.monotonic()
                    if t - last_ping < WS_PING_INTERVAL:
                        continue
                    last_ping = t
                    await ws.send_str(json.dumps({"type": "pong", "server_time": time.time()}))
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        e.hub.clients.pop(ws, None)
        e.ws_sessions.pop(ws, None)
        e.ws_conns.release(ip)
    return ws


@routes.get("/api/search")
async def search(request: web.Request) -> web.Response:
    gid, new = guest(request)
    tracks = await enc(request).party.search(
        request.query.get("q", ""), guest=gid, ip=ip_of(request)
    )
    resp = web.json_response({"tracks": [t.to_dict() for t in tracks]})
    if new:
        set_guest(resp, request, gid)
    return resp


@routes.post("/api/requests")
async def create_request(request: web.Request) -> web.Response:
    csrf_check(request)
    data = await body(request)
    gid, new = guest(request)
    track_id = track_id_param(data)
    e = enc(request)
    speaker_id = e.speakers.speaker_for_token(data.get("speaker"))
    req = await e.party.create_request(gid, track_id, ip=ip_of(request), speaker_id=speaker_id)
    resp = web.json_response({"request": req}, status=201)
    if new:
        set_guest(resp, request, gid)
    return resp


@routes.delete("/api/requests/{id}")
async def withdraw_request(request: web.Request) -> web.Response:
    csrf_check(request)
    gid, new = guest(request)
    if new:
        raise ApiError(404, "Request not found")
    await enc(request).party.withdraw_request(gid, int_param(request, "id"))
    return web.json_response({"ok": True})


@routes.get("/api/requests/mine")
async def my_requests(request: web.Request) -> web.Response:
    gid, new = guest(request)
    reqs = [] if new else await enc(request).party.my_requests(gid)
    resp = web.json_response({"requests": reqs})
    if new:
        set_guest(resp, request, gid)
    return resp


@routes.get("/api/me")
async def me(request: web.Request) -> web.Response:
    e = enc(request)
    user = await e.auth.current_user(request)
    return web.json_response(
        {
            "user": user.to_dict() if user else None,
            "oidc_enabled": e.config.oidc_enabled,
            "oidc_name": e.config.oidc_name,
        }
    )


@routes.get("/auth/login")
async def login(request: web.Request) -> web.Response:
    return await enc(request).auth.login_redirect(request)


@routes.get("/auth/callback")
async def callback(request: web.Request) -> web.Response:
    return await enc(request).auth.callback(request)


@routes.post("/auth/logout")
async def logout(request: web.Request) -> web.Response:
    csrf_check(request)
    e = enc(request)
    sid = request.cookies.get(SESSION_COOKIE)
    resp = await e.auth.logout(request)
    if sid:
        await e.close_session_sockets(sid)
    return resp


@routes.get("/sendspin")
@routes.get(r"/sendspin/{kbps:\d{1,3}}/sendspin")
async def sendspin_proxy(request: web.Request) -> web.StreamResponse:
    """Sendspin websocket. ``/sendspin/<kbps>/sendspin`` picks the Opus bitrate (the web
    player's quality setting); sendspin-js keeps that path across its own reconnects."""
    if not origin_allowed(request):
        raise web.HTTPForbidden(text="Cross-site WebSocket blocked")
    kbps = request.match_info.get("kbps")
    return await enc(request).output.ws_proxy(
        request, client_ip=ip_of(request), opus_kbps=int(kbps) if kbps else None
    )


# --------------------------------------------------------------------------- approvers
@routes.get("/api/admin/requests")
async def admin_requests(request: web.Request) -> web.Response:
    await enc(request).auth.require(request, "approve_requests")
    status = request.query.get("status")
    if status not in (None, "pending", "approved", "rejected", "all"):
        raise ApiError(400, "Invalid status")
    return web.json_response({"requests": await enc(request).party.list_requests(status)})


@routes.post("/api/admin/requests/approve-all")
async def approve_all(request: web.Request) -> web.Response:
    csrf_check(request)
    user = await enc(request).auth.require(request, "approve_requests")
    data = await body(request) if request.can_read_body else {}
    position = "next" if data.get("position") == "next" else "end"
    n = await enc(request).party.approve_all(user, position)
    return web.json_response({"ok": True, "approved": n})


@routes.post("/api/admin/requests/require-approval")
async def require_approval(request: web.Request) -> web.Response:
    """Approvers can switch approval off (auto-approve) and back on."""
    csrf_check(request)
    user = await enc(request).auth.require(request, "approve_requests")
    data = await body(request)
    if not isinstance(data.get("enabled"), bool):
        raise ApiError(400, "enabled must be true or false")
    n = await enc(request).party.set_require_approval(data["enabled"], user)
    return web.json_response({"ok": True, "require_approval": data["enabled"], "approved": n})


@routes.post("/api/admin/requests/{id}/approve")
async def approve(request: web.Request) -> web.Response:
    csrf_check(request)
    user = await enc(request).auth.require(request, "approve_requests")
    data = await body(request) if request.can_read_body else {}
    position = "next" if data.get("position") == "next" else "end"
    req = await enc(request).party.decide_request(int_param(request, "id"), user, True, position)
    return web.json_response({"request": req})


@routes.post("/api/admin/requests/{id}/reject")
async def reject(request: web.Request) -> web.Response:
    csrf_check(request)
    user = await enc(request).auth.require(request, "approve_requests")
    req = await enc(request).party.decide_request(int_param(request, "id"), user, False)
    return web.json_response({"request": req})


@routes.post("/api/admin/requests/{id}/recheck")
async def recheck(request: web.Request) -> web.Response:
    """Run the automatic (Jev) review again on a waiting request."""
    csrf_check(request)
    user = await enc(request).auth.require(request, "approve_requests")
    await enc(request).party.recheck_request(int_param(request, "id"), user)
    return web.json_response({"ok": True})


# --------------------------------------------------------------------------- queue managers
@routes.get("/api/admin/search")
async def admin_search(request: web.Request) -> web.Response:
    await enc(request).auth.require(request, "queue_add")
    tracks = await enc(request).party.search(request.query.get("q", ""))
    return web.json_response({"tracks": [t.to_dict() for t in tracks]})


@routes.post("/api/admin/queue")
async def queue_add(request: web.Request) -> web.Response:
    csrf_check(request)
    user = await enc(request).auth.require(request, "queue_add")
    data = await body(request)
    track_id = track_id_param(data)
    position = "next" if data.get("position") == "next" else "end"
    await enc(request).party.add_to_queue(track_id, position, user)
    return web.json_response({"ok": True}, status=201)


@routes.delete("/api/admin/queue/{id}")
async def queue_remove(request: web.Request) -> web.Response:
    csrf_check(request)
    user = await enc(request).auth.require(request, "queue_remove")
    await enc(request).party.remove_from_queue(int_param(request, "id"), user)
    return web.json_response({"ok": True})


@routes.post("/api/admin/queue/{id}/move")
async def queue_move(request: web.Request) -> web.Response:
    csrf_check(request)
    user = await enc(request).auth.require(request, "queue_reorder")
    data = await body(request)
    index = data.get("index")
    if index is None:
        raise ApiError(400, "index is required")
    if isinstance(index, bool) or not isinstance(index, int):
        raise ApiError(400, "index must be an integer")
    await enc(request).party.move_in_queue(int_param(request, "id"), index, user)
    return web.json_response({"ok": True})


@routes.post("/api/admin/player/{action}")
async def player_action(request: web.Request) -> web.Response:
    csrf_check(request)
    e = enc(request)
    action = request.match_info["action"]
    perm = "playback_skip" if action in ("skip", "previous", "seek") else "playback_pause"
    user = await e.auth.require(request, perm)
    now_playing = e.engine.now.candidate if e.engine.now else None
    audit: dict[str, Any] = {}
    if action == "skip":
        # what plays instead: the track already being mixed in, else the party's next pick
        upcoming = e.engine.locked or e.party.peek_next()
        audit = {
            "at": round(e.engine.position(), 1) if now_playing else None,
            "next": {**track_ref(upcoming.track), "source": upcoming.source} if upcoming else None,
        }
        e.engine.skip()
    elif action == "previous":
        data = await body(request)
        at = data.get("at")
        if at is not None and (isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at)):
            raise ApiError(400, "at must be a number of seconds")
        track_id = data.get("track_id")
        if track_id is not None and not isinstance(track_id, str):
            raise ApiError(400, "track_id must be a string")
        result = await e.party.previous(None if at is None else float(at), track_id, user)
        return web.json_response({"ok": True, "result": result})
    elif action == "seek":
        data = await body(request)
        pos = data.get("position")
        if isinstance(pos, bool) or not isinstance(pos, (int, float)) or not math.isfinite(pos):
            raise ApiError(400, "position must be a number of seconds")
        before = e.engine.position() if e.engine.now else None
        if not e.engine.seek(float(pos)):
            raise ApiError(409, "Nothing is playing")
        audit = {"from": None if before is None else round(before, 1), "to": round(float(pos), 1)}
    elif action == "pause":
        await e.engine.pause()
    elif action == "resume":
        await e.engine.resume()
    else:
        raise ApiError(404, "Unknown action")
    e.party.changed()
    track = now_playing.track if now_playing else None
    await e.party.audit.record(
        f"player.{action}",
        user=user,
        target_type="track" if track else None,
        target_id=track.id if track else None,
        target_label=track_label(track) if track else None,
        details={
            **({"track": track_ref(track), "source": now_playing.source} if track and now_playing else {}),
            **audit,
        },
    )
    return web.json_response({"ok": True})


# --------------------------------------------------------------------------- speakers
# Device side: the speaker identifies itself by its Sendspin client_id (its secret), sent
# in the body so it never ends up in URLs or access logs. No sign-in.
@routes.post("/api/speaker/self")
async def speaker_self(request: web.Request) -> web.Response:
    csrf_check(request)
    data = await body(request)
    return web.json_response(await enc(request).speakers.device_view(data.get("client_id")))


@routes.post("/api/speaker/self/name")
async def speaker_self_name(request: web.Request) -> web.Response:
    csrf_check(request)
    data = await body(request)
    e = enc(request)
    before = await e.speakers.device_view(data.get("client_id"))
    view = await e.speakers.device_rename(data.get("client_id"), data.get("name"))
    await e.party.audit.record(
        "speaker.rename",
        actor_kind="device",
        actor_name=f"Speaker {before.get('name') or ''}".strip(),
        ip=ip_of(request),
        target_type="speaker",
        target_id=view.get("id"),
        target_label=view.get("name"),
        details={"from": before.get("name"), "to": view.get("name"), "by": "speaker page"},
    )
    return web.json_response(view)


async def speaker_owner(request: web.Request) -> Any:
    return await enc(request).auth.require(request, "claim_speakers")


@routes.get("/api/speakers")
async def speakers_list(request: web.Request) -> web.Response:
    user = await speaker_owner(request)
    return web.json_response({"speakers": await enc(request).speakers.list_for(user)})


@routes.get("/api/speakers/claim/{token}")
async def speaker_claim_preview(request: web.Request) -> web.Response:
    user = await speaker_owner(request)
    return web.json_response(
        await enc(request).speakers.claim_preview(user, request.match_info["token"])
    )


@routes.post("/api/speakers/claim")
async def speaker_claim(request: web.Request) -> web.Response:
    csrf_check(request)
    user = await speaker_owner(request)
    data = await body(request)
    speaker = await enc(request).speakers.claim(user, data.get("token"))
    await audit(request, "speaker.claim", user, **speaker_targets([speaker]))
    return web.json_response({"speaker": speaker})


@routes.post("/api/speakers/control")
async def speakers_control(request: web.Request) -> web.Response:
    csrf_check(request)
    user = await speaker_owner(request)
    data = await body(request)
    speakers = await enc(request).speakers.control(user, data)
    changes = {k: data[k] for k in ("volume", "volume_delta", "muted", "locked") if data.get(k) is not None}
    await audit(request, "speaker.control", user, **speaker_targets(speakers), details=changes)
    return web.json_response({"speakers": speakers})


@routes.post("/api/speakers/share")
async def speakers_share(request: web.Request) -> web.Response:
    csrf_check(request)
    user = await speaker_owner(request)
    data = await body(request)
    e = enc(request)
    share = await e.speakers.create_share(user, data)
    views = [v for v in await e.speakers.list_for(user) if v["id"] in set(data.get("ids") or [])]
    await audit(
        request, "speaker.share", user, **speaker_targets(views),
        details={"mode": share["mode"], "expires_at": share["expires_at"]},
    )
    return web.json_response(share)


@routes.get("/api/speakers/share/{token}")
async def speakers_share_preview(request: web.Request) -> web.Response:
    await speaker_owner(request)
    return web.json_response(
        await enc(request).speakers.share_preview(request.match_info["token"])
    )


@routes.post("/api/speakers/share/{token}/accept")
async def speakers_share_accept(request: web.Request) -> web.Response:
    csrf_check(request)
    user = await speaker_owner(request)
    e = enc(request)
    share = e.speakers.peek_share(request.match_info["token"])
    speakers = await e.speakers.accept_share(user, request.match_info["token"])
    await audit(
        request, "speaker.share_accept", user, **speaker_targets(speakers),
        details={"mode": share.mode, "from": share.by_name, "from_user_id": share.by_user_id}
        if share else None,
    )
    return web.json_response({"speakers": speakers})


@routes.post("/api/speakers/assign")
async def speakers_assign(request: web.Request) -> web.Response:
    csrf_check(request)
    user = await speaker_owner(request)
    data = await body(request)
    e = enc(request)
    speakers = await e.speakers.assign(user, data)
    to = await e.db.fetchone("SELECT id, name, email FROM users WHERE id=?", data.get("user_id"))
    await audit(
        request, "speaker.assign", user, **speaker_targets(speakers),
        details={"user_id": data.get("user_id"), "user": (to or {}).get("name") or (to or {}).get("email")},
    )
    return web.json_response({"speakers": speakers})


@routes.patch("/api/speakers/{id}")
async def speaker_update(request: web.Request) -> web.Response:
    csrf_check(request)
    user = await speaker_owner(request)
    data = await body(request)
    e = enc(request)
    old = await e.db.fetchone("SELECT name, locked FROM speakers WHERE id=?", request.match_info["id"])
    speaker = await e.speakers.update(user, request.match_info["id"], data)
    changes: dict[str, Any] = {}
    if old and old["name"] != speaker["name"]:
        changes["name"] = {"from": old["name"], "to": speaker["name"]}
    if old and bool(old["locked"]) != speaker["locked"]:
        changes["locked"] = {"from": bool(old["locked"]), "to": speaker["locked"]}
    if changes:
        await audit(request, "speaker.update", user, **speaker_targets([speaker]), details=changes)
    return web.json_response({"speaker": speaker})


@routes.delete("/api/speakers/{id}/owners/{user_id}")
async def speaker_remove_owner(request: web.Request) -> web.Response:
    csrf_check(request)
    user = await speaker_owner(request)
    e = enc(request)
    sid, uid = request.match_info["id"], int_param(request, "user_id")
    row = await e.db.fetchone("SELECT name FROM speakers WHERE id=?", sid)
    owner = await e.db.fetchone("SELECT name, email FROM users WHERE id=?", uid)
    speaker = await e.speakers.remove_owner(user, sid, uid)
    await audit(
        request, "speaker.remove_owner", user,
        target_type="speaker", target_id=sid, target_label=(row or {}).get("name"),
        details={"user_id": uid, "user": (owner or {}).get("name") or (owner or {}).get("email")},
    )
    return web.json_response({"speaker": speaker})


@routes.delete("/api/speakers/{id}")
async def speaker_forget(request: web.Request) -> web.Response:
    csrf_check(request)
    user = await speaker_owner(request)
    e = enc(request)
    sid = request.match_info["id"]
    row = await e.db.fetchone("SELECT name FROM speakers WHERE id=?", sid)
    await e.speakers.forget(user, sid)
    await audit(
        request, "speaker.forget", user,
        target_type="speaker", target_id=sid, target_label=(row or {}).get("name"),
    )
    return web.json_response({"ok": True})


# --------------------------------------------------------------------------- admins
@routes.get("/api/admin/permissions")
async def permission_catalog(request: web.Request) -> web.Response:
    await enc(request).auth.require(request)
    from .auth import PERMISSIONS

    return web.json_response(
        {"permissions": [{"id": k, **v} for k, v in PERMISSIONS.items()]}
    )


@routes.get("/api/admin/users")
async def users(request: web.Request) -> web.Response:
    e = enc(request)
    await e.auth.require(request, "admin")
    from .auth import User

    rows = await e.db.fetchall("SELECT * FROM users ORDER BY id")
    out = []
    for r in rows:
        u = User.from_row(r).to_dict()
        u["created_at"] = r["created_at"]
        u["last_login"] = r["last_login"]
        out.append(u)
    return web.json_response({"users": out})


async def _apply_user_changes(
    e: AmParty, uids: list[int], is_admin: bool | None, set_: list[str] | None,
    add: list[str], remove: list[str],
) -> list[dict[str, Any]]:
    """Apply the changes; returns what actually changed per user (for the audit log)."""
    from .auth import User, clean_permissions

    changed: list[dict[str, Any]] = []
    # one transaction so the last-admin check and the writes can't interleave
    # with another request's changes
    async with e.db.transaction():
        rows = [await e.db.fetchone("SELECT * FROM users WHERE id=?", uid) for uid in uids]
        if any(r is None for r in rows):
            raise ApiError(404, "User not found")
        if is_admin is False:
            admins = await e.db.fetchone("SELECT COUNT(*) AS n FROM users WHERE is_admin=1")
            demoting = sum(1 for r in rows if r and r["is_admin"])
            if admins and demoting and admins["n"] - demoting < 1:
                raise ApiError(409, "Can't remove the last admin")
        for r in rows:
            assert r
            user = User.from_row(r)
            perms = set(clean_permissions(set_)) if set_ is not None else set(user.granted)
            perms |= set(clean_permissions(add))
            perms -= set(clean_permissions(remove))
            admin_val = int(is_admin) if is_admin is not None else r["is_admin"]
            granted, revoked = sorted(perms - set(user.granted)), sorted(set(user.granted) - perms)
            if granted or revoked or bool(admin_val) != bool(r["is_admin"]):
                change: dict[str, Any] = {"id": r["id"], "name": r["name"] or r["email"]}
                if granted:
                    change["granted"] = granted
                if revoked:
                    change["revoked"] = revoked
                if bool(admin_val) != bool(r["is_admin"]):
                    change["admin"] = bool(admin_val)
                changed.append(change)
            await e.db.execute(
                "UPDATE users SET permissions=?, is_admin=? WHERE id=?",
                json.dumps(sorted(perms)),
                admin_val,
                r["id"],
            )
    await e.refresh_ws_users()
    await e.hub.send_all({"type": "users_changed"}, "signed_in")
    return changed


async def _audit_user_changes(request: web.Request, user: Any, changed: list[dict[str, Any]]) -> None:
    for change in changed:
        details = {k: v for k, v in change.items() if k not in ("id", "name")}
        await audit(
            request, "user.update", user,
            target_type="user", target_id=change["id"], target_label=change["name"], details=details,
        )


def _perm_list(data: dict[str, Any], key: str) -> list[str] | None:
    val = data.get(key)
    if val is None:
        return None
    if not isinstance(val, list):
        raise ApiError(400, f"{key} must be a list")
    return val


@routes.patch("/api/admin/users/{id}")
async def update_user(request: web.Request) -> web.Response:
    csrf_check(request)
    e = enc(request)
    admin = await e.auth.require(request, "admin")
    data = await body(request)
    is_admin = data.get("is_admin")
    changed = await _apply_user_changes(
        e,
        [int_param(request, "id")],
        bool(is_admin) if is_admin is not None else None,
        _perm_list(data, "permissions"),
        [],
        [],
    )
    await _audit_user_changes(request, admin, changed)
    return web.json_response({"ok": True})


@routes.post("/api/admin/users/bulk")
async def bulk_update_users(request: web.Request) -> web.Response:
    """Apply permission changes to several users at once."""
    csrf_check(request)
    e = enc(request)
    admin = await e.auth.require(request, "admin")
    data = await body(request)
    ids = data.get("user_ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, int) for i in ids):
        raise ApiError(400, "user_ids must be a non-empty list of ids")
    is_admin = data.get("is_admin")
    changed = await _apply_user_changes(
        e,
        ids,
        bool(is_admin) if is_admin is not None else None,
        _perm_list(data, "set"),
        _perm_list(data, "add") or [],
        _perm_list(data, "remove") or [],
    )
    await _audit_user_changes(request, admin, changed)
    return web.json_response({"ok": True, "updated": len(ids)})


@routes.delete("/api/admin/users/{id}")
async def delete_user(request: web.Request) -> web.Response:
    csrf_check(request)
    e = enc(request)
    me_user = await e.auth.require(request, "admin")
    uid = int_param(request, "id")
    row = await e.db.fetchone("SELECT * FROM users WHERE id=?", uid)
    if not row:
        raise ApiError(404, "User not found")
    if row["is_admin"]:
        admins = await e.db.fetchone("SELECT COUNT(*) AS n FROM users WHERE is_admin=1")
        if admins and admins["n"] <= 1:
            raise ApiError(409, "Can't delete the last admin")
    if uid == me_user.id:
        raise ApiError(409, "You can't delete yourself")
    # One transaction, so a claim/assign can't slip in between and be cascaded away unnoticed.
    async with e.db.transaction():
        owned = await e.db.fetchall("SELECT speaker_id FROM speaker_owners WHERE user_id=?", uid)
        await e.db.execute("DELETE FROM users WHERE id=?", uid)
    e.speakers.owners_changed([r["speaker_id"] for r in owned])
    await audit(
        request, "user.delete", me_user,
        target_type="user", target_id=uid, target_label=row["name"] or row["email"],
        details={
            "email": row["email"],
            "was_admin": bool(row["is_admin"]),
            "permissions": json.loads(row["permissions"] or "[]"),
            "speakers": [r["speaker_id"] for r in owned],
        },
    )
    await e.refresh_ws_users()
    await e.hub.send_all({"type": "users_changed"}, "signed_in")
    return web.json_response({"ok": True})


@routes.get("/api/admin/settings")
async def get_settings(request: web.Request) -> web.Response:
    await enc(request).auth.require(request, "admin")
    return web.json_response(enc(request).party.settings())


@routes.patch("/api/admin/settings")
async def patch_settings(request: web.Request) -> web.Response:
    csrf_check(request)
    user = await enc(request).auth.require(request, "admin")
    data = await body(request)
    return web.json_response(await enc(request).party.update_settings(data, user))


@routes.get("/api/admin/playlists")
async def playlists(request: web.Request) -> web.Response:
    e = enc(request)
    await e.auth.require(request, "admin")
    e.party._require_apple()  # noqa: SLF001
    pls = await e.apple.get_library_playlists()
    return web.json_response({"playlists": [p.to_dict() for p in pls]})


@routes.post("/api/admin/playlists/resolve")
async def resolve_playlist(request: web.Request) -> web.Response:
    csrf_check(request)
    e = enc(request)
    await e.auth.require(request, "admin")
    e.party._require_apple()  # noqa: SLF001
    data = await body(request)
    pid = e.apple.parse_playlist_ref(str(data.get("url_or_id", "")))
    if not pid:
        raise ApiError(400, "That doesn't look like an Apple Music playlist link")
    try:
        pl = await e.apple.get_playlist(pid)
    except Exception as err:  # noqa: BLE001
        raise ApiError(404, "Playlist not found") from err
    return web.json_response({"id": pl.id, "name": pl.name})


@routes.get("/api/admin/status")
async def status(request: web.Request) -> web.Response:
    e = enc(request)
    await e.auth.require(request, "admin")
    return web.json_response(e.status())


@routes.get("/api/admin/audit")
async def audit_log(request: web.Request) -> web.Response:
    """The audit log, newest first. Paged with ?before=<id>; filters: action (exact, or
    a prefix ending in "."), actor (user id), actor_kind, q (free text)."""
    e = enc(request)
    user = await e.auth.require(request, "view_audit_log")
    query = request.query

    def int_query(name: str) -> int | None:
        raw = query.get(name)
        if raw in (None, ""):
            return None
        try:
            return int(raw)
        except ValueError as err:
            raise ApiError(400, f"Invalid {name}") from err

    kind = query.get("actor_kind") or None
    if kind is not None and kind not in ACTOR_KINDS:
        raise ApiError(400, "Invalid actor_kind")
    action = query.get("action") or None
    if action is not None and (len(action) > 64 or not re.fullmatch(r"[a-z_.]+", action)):
        raise ApiError(400, "Invalid action")
    out = await e.party.audit.list(
        before=int_query("before"),
        limit=int_query("limit") or 100,
        action=action,
        actor_id=int_query("actor"),
        actor_kind=kind,
        q=query.get("q") or None,
        include_ip=user.is_admin,  # guest IPs are for admins only
    )
    return web.json_response(out)


@routes.get("/api/admin/apple-music")
async def apple_status(request: web.Request) -> web.Response:
    e = enc(request)
    await e.auth.require(request, "admin")
    return web.json_response(await e.apple_status())


@routes.put("/api/admin/apple-music")
async def apple_put(request: web.Request) -> web.Response:
    csrf_check(request)
    e = enc(request)
    user = await e.auth.require(request, "admin")
    data = await body(request)
    for key in ("developer_token", "user_token"):
        value = data.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > 8192):
            raise ApiError(400, f"{key} must be a string")
    await e.update_apple_credentials(
        developer_token=data.get("developer_token"), user_token=data.get("user_token")
    )
    # never the tokens themselves, only which were set or cleared
    changes = {
        key: "set" if data[key] else "cleared"
        for key in ("developer_token", "user_token")
        if data.get(key) is not None
    }
    if changes:
        await audit(
            request, "credentials.apple_music", user,
            target_type="credential", target_label="Apple Music", details=changes,
        )
    return web.json_response(await e.apple_status())


@routes.get("/api/admin/auto-approval")
async def auto_approval_status(request: web.Request) -> web.Response:
    e = enc(request)
    await e.auth.require(request, "admin")
    return web.json_response(await e.auto_approval_status())


@routes.put("/api/admin/auto-approval")
async def auto_approval_put(request: web.Request) -> web.Response:
    """Set ("" clears) a Jev provider's API key. Keys are never returned by any endpoint."""
    from .autoapprove import PROVIDERS

    csrf_check(request)
    e = enc(request)
    user = await e.auth.require(request, "admin")
    data = await body(request)
    provider = data.get("provider", "typesafe")
    if provider not in PROVIDERS:
        raise ApiError(400, "provider must be one of " + ", ".join(PROVIDERS))
    key = data.get("api_key")
    if not isinstance(key, str) or len(key) > 1024:
        raise ApiError(400, "api_key must be a string")
    await e.update_jev_api_key(provider, key)
    await audit(
        request, "credentials.jev_api_key", user,
        target_type="credential", target_id=provider, target_label=f"Jev API key ({provider})",
        details={"provider": provider, "change": "set" if key.strip() else "cleared"},
    )
    return web.json_response(await e.auto_approval_status())


@routes.post("/api/admin/apple-music/user-token")
async def apple_user_token(request: web.Request) -> web.Response:
    csrf_check(request)
    e = enc(request)
    user = await e.auth.require(request, "admin")
    data = await body(request)
    token = data.get("music_user_token") or data.get("music-user-token")
    if not token or not isinstance(token, str) or len(token) > 8192:
        raise ApiError(400, "music_user_token is required")
    await e.update_apple_credentials(user_token=token)
    await audit(
        request, "credentials.apple_music", user,
        target_type="credential", target_label="Apple Music",
        details={"user_token": "set", "via": "Apple sign-in"},
    )
    return web.json_response(await e.apple_status())


@routes.get("/admin/apple-music/connect")
async def apple_connect_page(request: web.Request) -> web.Response:
    e = enc(request)
    user = await e.auth.current_user(request)
    if not user:
        raise web.HTTPFound("/auth/login?next=/admin/apple-music/connect")
    if not user.is_admin:
        raise web.HTTPForbidden(text="Admins only")
    dev = await e.developer_token()
    if not dev:
        return web.Response(
            text="No Apple Music developer token configured. Add one in Settings first.",
            status=400,
        )
    from .providers.apple_music import render_musickit_auth_page

    html = render_musickit_auth_page(
        developer_token=dev,
        callback_url="/api/admin/apple-music/user-token",
        app_name=e.party.settings().get("party_name") or "Party",
    )
    html, csp = musickit_page_csp(html)
    return web.Response(
        text=html,
        content_type="text/html",
        headers={"Cache-Control": "no-store", "Content-Security-Policy": csp},
    )


_SCRIPT_TAG_RE = re.compile(r"<script(?=[\s>])", re.IGNORECASE)
_HANDLER_RE = re.compile(r'\son[a-z]+="([^"]*)"', re.IGNORECASE)


def musickit_page_csp(html: str) -> tuple[str, str]:
    """Nonce the page's <script> tags and hash its inline event handlers.

    The page loads MusicKit JS from Apple's CDN, which then talks to Apple's APIs and
    opens Apple's sign-in popup.
    """
    nonce = secrets.token_urlsafe(18)
    html = _SCRIPT_TAG_RE.sub(f'<script nonce="{nonce}"', html)
    handler_hashes = sorted(
        {
            "'sha256-" + base64.b64encode(hashlib.sha256(h.encode()).digest()).decode() + "'"
            for h in _HANDLER_RE.findall(html)
        }
    )
    script_src = [f"'nonce-{nonce}'", "https://js-cdn.music.apple.com"]
    if handler_hashes:
        script_src += ["'unsafe-hashes'", *handler_hashes]
    csp = "; ".join(
        [
            "default-src 'self'",
            "script-src " + " ".join(script_src),
            "style-src 'self' 'unsafe-inline' https://*.apple.com",
            "img-src 'self' data: https:",
            "font-src 'self' data: https://*.apple.com",
            "connect-src 'self' https://*.apple.com",
            "frame-src https://*.apple.com",
            "worker-src 'self' blob: https://js-cdn.music.apple.com",
            "media-src 'self' blob: https:",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ]
    )
    return html, csp

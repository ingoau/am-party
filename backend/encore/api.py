"""HTTP + WebSocket routes."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

from .auth import guest_id, set_guest_cookie
from .party import ApiError

if TYPE_CHECKING:
    from .app import Encore

LOGGER = logging.getLogger(__name__)

routes = web.RouteTableDef()


def enc(request: web.Request) -> Encore:
    return request.app["encore"]


async def body(request: web.Request) -> dict[str, Any]:
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


@web.middleware
async def security_headers(request: web.Request, handler):  # type: ignore[no-untyped-def]
    resp = await handler(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


def csrf_check(request: web.Request) -> None:
    """Reject cross-site state-changing requests (cookies are SameSite=Lax, belt and braces)."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/") != enc(request).config.public_url:
        host = request.headers.get("Host", "")
        if not origin.split("://", 1)[-1].startswith(host):
            raise ApiError(403, "Cross-site request blocked")


# --------------------------------------------------------------------------- public
@routes.get("/api/state")
async def get_state(request: web.Request) -> web.Response:
    return web.json_response(enc(request).party.state())


@routes.get("/api/ws")
async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    e = enc(request)
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    user = await e.auth.current_user(request)
    e.hub.clients[ws] = user
    try:
        await ws.send_str(json.dumps({"type": "state", "data": e.party.state()}))
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "ping":
                    await ws.send_str(json.dumps({"type": "pong", "server_time": time.time()}))
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        e.hub.clients.pop(ws, None)
    return ws


@routes.get("/api/search")
async def search(request: web.Request) -> web.Response:
    gid, new = guest_id(request)
    tracks = await enc(request).party.search(request.query.get("q", ""), guest=gid)
    resp = web.json_response({"tracks": [t.to_dict() for t in tracks]})
    if new:
        set_guest_cookie(resp, gid, enc(request).config.cookie_secure)
    return resp


@routes.post("/api/requests")
async def create_request(request: web.Request) -> web.Response:
    csrf_check(request)
    data = await body(request)
    gid, new = guest_id(request)
    if not data.get("track_id"):
        raise ApiError(400, "track_id is required")
    req = await enc(request).party.create_request(gid, str(data["track_id"]))
    resp = web.json_response({"request": req}, status=201)
    if new:
        set_guest_cookie(resp, gid, enc(request).config.cookie_secure)
    return resp


@routes.get("/api/requests/mine")
async def my_requests(request: web.Request) -> web.Response:
    gid, new = guest_id(request)
    reqs = [] if new else await enc(request).party.my_requests(gid)
    resp = web.json_response({"requests": reqs})
    if new:
        set_guest_cookie(resp, gid, enc(request).config.cookie_secure)
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
    return await enc(request).auth.logout(request)


@routes.get("/sendspin")
async def sendspin_proxy(request: web.Request) -> web.StreamResponse:
    return await enc(request).output.ws_proxy(request)


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
    if not data.get("track_id"):
        raise ApiError(400, "track_id is required")
    position = "next" if data.get("position") == "next" else "end"
    await enc(request).party.add_to_queue(str(data["track_id"]), position, user)
    return web.json_response({"ok": True}, status=201)


@routes.delete("/api/admin/queue/{id}")
async def queue_remove(request: web.Request) -> web.Response:
    csrf_check(request)
    await enc(request).auth.require(request, "queue_remove")
    await enc(request).party.remove_from_queue(int_param(request, "id"))
    return web.json_response({"ok": True})


@routes.post("/api/admin/queue/{id}/move")
async def queue_move(request: web.Request) -> web.Response:
    csrf_check(request)
    await enc(request).auth.require(request, "queue_reorder")
    data = await body(request)
    try:
        index = int(data.get("index"))  # type: ignore[arg-type]
    except (TypeError, ValueError) as err:
        raise ApiError(400, "index is required") from err
    await enc(request).party.move_in_queue(int_param(request, "id"), index)
    return web.json_response({"ok": True})


@routes.post("/api/admin/player/{action}")
async def player_action(request: web.Request) -> web.Response:
    csrf_check(request)
    e = enc(request)
    action = request.match_info["action"]
    await e.auth.require(request, "playback_skip" if action == "skip" else "playback_pause")
    if action == "skip":
        e.engine.skip()
    elif action == "pause":
        await e.engine.pause()
    elif action == "resume":
        await e.engine.resume()
    else:
        raise ApiError(404, "Unknown action")
    e.party.changed()
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
    e: Encore, uids: list[int], is_admin: bool | None, set_: list[str] | None,
    add: list[str], remove: list[str],
) -> None:
    from .auth import User, clean_permissions

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
        await e.db.c.execute(
            "UPDATE users SET permissions=?, is_admin=? WHERE id=?",
            (json.dumps(sorted(perms)), admin_val, r["id"]),
        )
    await e.db.c.commit()
    await e.refresh_ws_users()
    await e.hub.send_all({"type": "users_changed"}, "signed_in")


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
    await e.auth.require(request, "admin")
    data = await body(request)
    is_admin = data.get("is_admin")
    await _apply_user_changes(
        e,
        [int_param(request, "id")],
        bool(is_admin) if is_admin is not None else None,
        _perm_list(data, "permissions"),
        [],
        [],
    )
    return web.json_response({"ok": True})


@routes.post("/api/admin/users/bulk")
async def bulk_update_users(request: web.Request) -> web.Response:
    """Apply permission changes to several users at once."""
    csrf_check(request)
    e = enc(request)
    await e.auth.require(request, "admin")
    data = await body(request)
    ids = data.get("user_ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, int) for i in ids):
        raise ApiError(400, "user_ids must be a non-empty list of ids")
    is_admin = data.get("is_admin")
    await _apply_user_changes(
        e,
        ids,
        bool(is_admin) if is_admin is not None else None,
        _perm_list(data, "set"),
        _perm_list(data, "add") or [],
        _perm_list(data, "remove") or [],
    )
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
    await e.db.execute("DELETE FROM users WHERE id=?", uid)
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
    await enc(request).auth.require(request, "admin")
    data = await body(request)
    return web.json_response(await enc(request).party.update_settings(data))


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


@routes.get("/api/admin/apple-music")
async def apple_status(request: web.Request) -> web.Response:
    e = enc(request)
    await e.auth.require(request, "admin")
    return web.json_response(await e.apple_status())


@routes.put("/api/admin/apple-music")
async def apple_put(request: web.Request) -> web.Response:
    csrf_check(request)
    e = enc(request)
    await e.auth.require(request, "admin")
    data = await body(request)
    await e.update_apple_credentials(
        developer_token=data.get("developer_token"), user_token=data.get("user_token")
    )
    return web.json_response(await e.apple_status())


@routes.post("/api/admin/apple-music/user-token")
async def apple_user_token(request: web.Request) -> web.Response:
    csrf_check(request)
    e = enc(request)
    await e.auth.require(request, "admin")
    data = await body(request)
    token = data.get("music_user_token") or data.get("music-user-token")
    if not token or not isinstance(token, str):
        raise ApiError(400, "music_user_token is required")
    await e.update_apple_credentials(user_token=token)
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
        app_name=e.party.settings().get("party_name") or "Encore",
    )
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})

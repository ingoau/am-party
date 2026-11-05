"""Generic OIDC login (authorization code + PKCE), sessions and permissions.

The first user to ever sign in becomes admin; everyone after that starts with no
privileges until an admin grants them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

from .config import Config
from .db import Database, now

LOGGER = logging.getLogger(__name__)

SESSION_COOKIE = "encore_session"
GUEST_COOKIE = "encore_guest"
SESSION_TTL = 30 * 24 * 3600
STATE_TTL = 600

PERMISSIONS = ("is_admin", "can_approve", "can_manage_queue")


@dataclass
class User:
    id: int
    sub: str
    email: str | None
    name: str | None
    picture: str | None
    is_admin: bool
    can_approve: bool
    can_manage_queue: bool

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> User:
        return cls(
            id=row["id"],
            sub=row["sub"],
            email=row.get("email"),
            name=row.get("name"),
            picture=row.get("picture"),
            is_admin=bool(row["is_admin"]),
            can_approve=bool(row["can_approve"]) or bool(row["is_admin"]),
            can_manage_queue=bool(row["can_manage_queue"]) or bool(row["is_admin"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name or self.email or self.sub,
            "picture": self.picture,
            "is_admin": self.is_admin,
            "can_approve": self.can_approve,
            "can_manage_queue": self.can_manage_queue,
        }


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    """Decode JWT payload without verifying.

    Only used for the id_token we receive directly from the token endpoint over TLS
    (OIDC core 3.1.3.7 allows TLS server validation in place of signature checks for
    the code flow). We still check iss/aud/nonce.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}


class AuthManager:
    def __init__(self, config: Config, db: Database, http: aiohttp.ClientSession) -> None:
        self.config = config
        self.db = db
        self.http = http
        self._discovery: dict[str, Any] | None = None
        self._discovery_at = 0.0
        self._pending: dict[str, dict[str, Any]] = {}

    # -- OIDC ------------------------------------------------------------------
    async def discovery(self) -> dict[str, Any]:
        if self._discovery and time.time() - self._discovery_at < 3600:
            return self._discovery
        url = f"{self.config.oidc_issuer}/.well-known/openid-configuration"
        async with self.http.get(url) as resp:
            resp.raise_for_status()
            self._discovery = await resp.json(content_type=None)
        self._discovery_at = time.time()
        return self._discovery

    @property
    def redirect_uri(self) -> str:
        return f"{self.config.public_url}/auth/callback"

    async def login_redirect(self, request: web.Request) -> web.Response:
        if not self.config.oidc_enabled:
            raise web.HTTPServiceUnavailable(text="OIDC is not configured")
        disc = await self.discovery()
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        next_url = request.query.get("next", "/admin")
        if not next_url.startswith("/") or next_url.startswith("//"):
            next_url = "/admin"
        self._gc_pending()
        self._pending[state] = {
            "nonce": nonce,
            "verifier": verifier,
            "next": next_url,
            "expires": time.time() + STATE_TTL,
        }
        params = {
            "response_type": "code",
            "client_id": self.config.oidc_client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.config.oidc_scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        raise web.HTTPFound(f"{disc['authorization_endpoint']}?{urlencode(params)}")

    def _gc_pending(self) -> None:
        t = time.time()
        for k in [k for k, v in self._pending.items() if v["expires"] < t]:
            self._pending.pop(k, None)

    async def callback(self, request: web.Request) -> web.Response:
        if "error" in request.query:
            raise web.HTTPBadRequest(text=f"Login failed: {request.query.get('error')}")
        state = request.query.get("state", "")
        code = request.query.get("code", "")
        pending = self._pending.pop(state, None)
        if not pending or pending["expires"] < time.time() or not code:
            raise web.HTTPBadRequest(text="Login expired or invalid, please try again.")
        disc = await self.discovery()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.config.oidc_client_id,
            "code_verifier": pending["verifier"],
        }
        if self.config.oidc_client_secret:
            data["client_secret"] = self.config.oidc_client_secret
        async with self.http.post(disc["token_endpoint"], data=data) as resp:
            body = await resp.json(content_type=None)
            if resp.status >= 400:
                LOGGER.warning("Token exchange failed: %s", body)
                raise web.HTTPBadRequest(text="Login failed (token exchange).")

        claims: dict[str, Any] = {}
        if id_token := body.get("id_token"):
            claims = _decode_jwt_claims(id_token)
            aud = claims.get("aud")
            auds = aud if isinstance(aud, list) else [aud]
            if (
                claims.get("iss", "").rstrip("/") != self.config.oidc_issuer
                or self.config.oidc_client_id not in auds
                or claims.get("nonce") != pending["nonce"]
            ):
                raise web.HTTPBadRequest(text="Login failed (id_token validation).")
        if access_token := body.get("access_token"):
            if userinfo_ep := disc.get("userinfo_endpoint"):
                async with self.http.get(
                    userinfo_ep, headers={"Authorization": f"Bearer {access_token}"}
                ) as resp:
                    if resp.status < 400:
                        userinfo = await resp.json(content_type=None)
                        if claims.get("sub") and userinfo.get("sub") != claims["sub"]:
                            raise web.HTTPBadRequest(text="Login failed (subject mismatch).")
                        claims.update(userinfo)
        if not claims.get("sub"):
            raise web.HTTPBadRequest(text="Login failed (no subject).")

        user = await self.upsert_user(claims)
        session_id = secrets.token_urlsafe(32)
        await self.db.execute(
            "INSERT INTO sessions(id, user_id, created_at, expires_at) VALUES(?,?,?,?)",
            session_id,
            user.id,
            now(),
            now() + SESSION_TTL,
        )
        resp = web.HTTPFound(pending["next"])
        resp.set_cookie(
            SESSION_COOKIE,
            session_id,
            max_age=SESSION_TTL,
            httponly=True,
            secure=self.config.cookie_secure,
            samesite="Lax",
            path="/",
        )
        raise resp

    async def upsert_user(self, claims: dict[str, Any]) -> User:
        sub = str(claims["sub"])
        email = claims.get("email")
        name = claims.get("name") or claims.get("preferred_username") or email
        picture = claims.get("picture")
        existing = await self.db.fetchone("SELECT * FROM users WHERE sub=?", sub)
        if existing:
            await self.db.execute(
                "UPDATE users SET email=?, name=?, picture=?, last_login=? WHERE id=?",
                email,
                name,
                picture,
                now(),
                existing["id"],
            )
        else:
            # BEGIN IMMEDIATE serialises concurrent first sign-ins so exactly one admin.
            await self.db.c.execute("BEGIN IMMEDIATE")
            try:
                count = await self.db.fetchone("SELECT COUNT(*) AS n FROM users")
                first = (count or {}).get("n", 0) == 0
                await self.db.c.execute(
                    "INSERT INTO users(sub, email, name, picture, is_admin, can_approve, "
                    "can_manage_queue, created_at, last_login) VALUES(?,?,?,?,?,?,?,?,?)",
                    (sub, email, name, picture, int(first), int(first), int(first), now(), now()),
                )
                await self.db.c.commit()
            except Exception:
                await self.db.c.rollback()
                raise
            if first:
                LOGGER.info("First user %s signed in and was made admin", email or sub)
        row = await self.db.fetchone("SELECT * FROM users WHERE sub=?", sub)
        assert row
        return User.from_row(row)

    # -- sessions --------------------------------------------------------------
    async def current_user(self, request: web.Request) -> User | None:
        if "user" in request:
            return request["user"]
        user = None
        if sid := request.cookies.get(SESSION_COOKIE):
            row = await self.db.fetchone(
                "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.id=? AND s.expires_at > ?",
                sid,
                now(),
            )
            if row:
                user = User.from_row(row)
        request["user"] = user
        return user

    async def logout(self, request: web.Request) -> web.Response:
        if sid := request.cookies.get(SESSION_COOKIE):
            await self.db.execute("DELETE FROM sessions WHERE id=?", sid)
        resp = web.json_response({"ok": True})
        resp.del_cookie(SESSION_COOKIE, path="/")
        return resp

    async def require(self, request: web.Request, permission: str | None = None) -> User:
        user = await self.current_user(request)
        if not user:
            raise web.HTTPUnauthorized(text="Sign in required")
        if permission and not getattr(user, permission):
            raise web.HTTPForbidden(text="You don't have permission for this")
        return user


def guest_id(request: web.Request) -> tuple[str, bool]:
    """Return the anonymous guest id for this browser, and whether it is new."""
    gid = request.cookies.get(GUEST_COOKIE)
    if gid and 16 <= len(gid) <= 64:
        return gid, False
    return secrets.token_urlsafe(18), True


def set_guest_cookie(resp: web.StreamResponse, gid: str, secure: bool) -> None:
    resp.set_cookie(
        GUEST_COOKIE,
        gid,
        max_age=365 * 24 * 3600,
        httponly=True,
        secure=secure,
        samesite="Lax",
        path="/",
    )

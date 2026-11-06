"""Generic OIDC login (authorization code + PKCE), sessions and permissions.

Admins: if ``AM_PARTY_ADMIN_EMAILS`` is set, exactly those (verified) emails become
admin when they sign in. Otherwise the first user to ever sign in becomes admin;
everyone after that starts with no privileges until an admin grants them.
``AM_PARTY_ALLOWED_EMAILS`` / ``AM_PARTY_ALLOWED_DOMAINS`` optionally restrict who may
sign in at all.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import aiohttp
import jwt
from aiohttp import web

from .config import Config
from .db import Database, now

LOGGER = logging.getLogger(__name__)

SESSION_COOKIE = "am_party_session"
GUEST_COOKIE = "am_party_guest"
SESSION_TTL = 30 * 24 * 3600  # absolute lifetime of a session
SESSION_IDLE_TTL = 7 * 24 * 3600  # ...and it expires after this long unused
SESSION_REFRESH_SLACK = 3600  # don't rewrite expires_at more than ~hourly
STATE_TTL = 600
MAX_PENDING_LOGINS = 1000
GUEST_COOKIE_TTL = 365 * 24 * 3600

# Individually grantable permissions. Admins implicitly have all of them, plus
# managing users and settings.
PERMISSIONS: dict[str, dict[str, str]] = {
    "approve_requests": {
        "label": "Approve requests",
        "description": "Approve or reject songs guests request",
        "group": "Requests",
    },
    "queue_add": {
        "label": "Add to queue",
        "description": "Add songs straight to the queue without approval",
        "group": "Queue",
    },
    "queue_reorder": {
        "label": "Reorder queue",
        "description": "Move songs around in the queue",
        "group": "Queue",
    },
    "queue_remove": {
        "label": "Remove from queue",
        "description": "Take songs out of the queue",
        "group": "Queue",
    },
    "playback_skip": {
        "label": "Skip songs",
        "description": "Skip the current song for every device",
        "group": "Playback",
    },
    "playback_pause": {
        "label": "Pause & resume",
        "description": "Pause and resume playback for every device",
        "group": "Playback",
    },
}
ADMIN = "admin"
# what the old can_* columns map to (migration)
LEGACY_APPROVE = ["approve_requests"]
LEGACY_MANAGE_QUEUE = ["queue_add", "queue_reorder", "queue_remove", "playback_skip", "playback_pause"]


def clean_permissions(perms: Any) -> list[str]:
    if not isinstance(perms, (list, tuple, set)):
        return []
    return sorted({p for p in perms if isinstance(p, str) and p in PERMISSIONS})


@dataclass
class User:
    id: int
    sub: str
    email: str | None
    name: str | None
    picture: str | None
    is_admin: bool
    granted: list[str]

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> User:
        try:
            granted = clean_permissions(json.loads(row.get("permissions") or "[]"))
        except ValueError:
            granted = []
        return cls(
            id=row["id"],
            sub=row["sub"],
            email=row.get("email"),
            name=row.get("name"),
            picture=row.get("picture"),
            is_admin=bool(row["is_admin"]),
            granted=granted,
        )

    @property
    def permissions(self) -> list[str]:
        """Effective permissions."""
        return list(PERMISSIONS) if self.is_admin else self.granted

    def has(self, permission: str) -> bool:
        if permission == ADMIN:
            return self.is_admin
        return self.is_admin or permission in self.granted

    def has_any(self) -> bool:
        return self.is_admin or bool(self.granted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name or self.email or self.sub,
            "picture": self.picture,
            "is_admin": self.is_admin,
            "permissions": self.permissions,
            "granted": self.granted,
        }


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


ID_TOKEN_LEEWAY = 120
# Asymmetric algorithms we accept for id_tokens, by JWK key type ("none"/HS* never).
KEY_TYPE_ALGORITHMS: dict[str, tuple[str, ...]] = {
    "RSA": ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512"),
    "EC": ("ES256", "ES384", "ES512"),
    "OKP": ("EdDSA",),
}
ALLOWED_ALGORITHMS = tuple(a for algs in KEY_TYPE_ALGORITHMS.values() for a in algs)


def id_token_algorithms(jwk: dict[str, Any], discovery_algs: list[str] | None) -> list[str]:
    """Algorithms an id_token signed by ``jwk`` may use.

    Asymmetric only, intersected with what discovery advertises, and pinned to the
    JWK's own ``alg`` when it has one (otherwise to the algorithms of its key type).
    """
    advertised = discovery_algs or ["RS256"]
    algs = [a for a in ALLOWED_ALGORITHMS if a in advertised]
    if jwk_alg := jwk.get("alg"):
        return [a for a in algs if a == jwk_alg]
    return [a for a in algs if a in KEY_TYPE_ALGORITHMS.get(str(jwk.get("kty")), ())]


def email_verified(claims: dict[str, Any]) -> bool:
    """email_verified must not be false (some IdPs omit it, some send strings)."""
    value = claims.get("email_verified", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no")
    return value is not False


def normalised_email(claims: dict[str, Any]) -> str | None:
    email = claims.get("email")
    if not isinstance(email, str) or "@" not in email:
        return None
    return email.strip().lower()


def sign_in_allowed(config: Config, claims: dict[str, Any]) -> bool:
    """Whether the allowlists (if any) let this identity sign in."""
    if not config.sign_in_restricted:
        return True
    email = normalised_email(claims)
    if not email or not email_verified(claims):
        return False
    domain = email.rsplit("@", 1)[1]
    return (
        email in config.allowed_emails
        or email in config.admin_emails
        or domain in config.allowed_domains
    )


def is_configured_admin(config: Config, claims: dict[str, Any]) -> bool:
    email = normalised_email(claims)
    return bool(email and email in config.admin_emails and email_verified(claims))


def safe_next(value: str | None, default: str = "/admin") -> str:
    """Only same-origin /admin paths are allowed as post-login redirect targets."""
    if not value or len(value) > 512 or not value.startswith("/") or value.startswith("//"):
        return default
    if "\\" in value or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return default
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return default
    if parts.path != "/admin" and not parts.path.startswith("/admin/"):
        return default
    return value


def access_denied_page(email: str | None) -> web.Response:
    who = html.escape(email) if email else "This account"
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Not on the list</title>
<style>body{{font-family:system-ui,sans-serif;background:#111;color:#eee;display:grid;place-items:center;min-height:100vh;margin:0}}
main{{max-width:28rem;padding:2rem;text-align:center}}a{{color:#9cf}}</style></head>
<body><main><h1>Not on the list</h1>
<p>{who} isn't allowed to sign in to this party's admin area.</p>
<p>Ask the host to add you, or <a href="/">go back to the party</a>.</p></main></body></html>"""
    return web.Response(
        text=body, status=403, content_type="text/html", headers={"Cache-Control": "no-store"}
    )


class AuthManager:
    def __init__(self, config: Config, db: Database, http: aiohttp.ClientSession) -> None:
        self.config = config
        self.db = db
        self.http = http
        self._discovery: dict[str, Any] | None = None
        self._discovery_at = 0.0
        self._pending: dict[str, dict[str, Any]] = {}
        self._jwks: jwt.PyJWKClient | None = None
        # one shared aiosqlite connection: BEGIN IMMEDIATE can't serialise coroutines
        self._user_lock = asyncio.Lock()

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

    async def verify_id_token(self, id_token: str, disc: dict[str, Any]) -> dict[str, Any]:
        """Verify the id_token signature (JWKS), audience, expiry and issuer."""
        if self._jwks is None or self._jwks.uri != disc["jwks_uri"]:
            self._jwks = jwt.PyJWKClient(disc["jwks_uri"], cache_keys=True, lifespan=3600)
        signing_key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, id_token)
        algorithms = id_token_algorithms(
            signing_key._jwk_data,  # noqa: SLF001
            disc.get("id_token_signing_alg_values_supported"),
        )
        if not algorithms:
            raise jwt.InvalidAlgorithmError("no acceptable id_token algorithm for this key")
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=algorithms,
            audience=self.config.oidc_client_id,
            leeway=ID_TOKEN_LEEWAY,
            options={"require": ["exp", "iat", "sub"], "verify_iss": False},
        )
        # issuers differ in trailing-slash handling (e.g. Authentik), compare normalised
        if str(claims.get("iss", "")).rstrip("/") != self.config.oidc_issuer:
            raise jwt.InvalidIssuerError("issuer mismatch")
        check_azp(claims, self.config.oidc_client_id or "")
        return claims

    async def login_redirect(self, request: web.Request) -> web.Response:
        if not self.config.oidc_enabled:
            raise web.HTTPServiceUnavailable(text="OIDC is not configured")
        disc = await self.discovery()
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        next_url = safe_next(request.query.get("next"))
        self._gc_pending()
        while len(self._pending) >= MAX_PENDING_LOGINS:
            self._pending.pop(next(iter(self._pending)))  # drop the oldest
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

        id_token = body.get("id_token") if isinstance(body, dict) else None
        if not id_token or not isinstance(id_token, str):
            # userinfo-only logins aren't bound to this client/nonce: refuse them
            raise web.HTTPBadRequest(text="Login failed (no id_token).")
        try:
            claims = await self.verify_id_token(id_token, disc)
        except jwt.PyJWTError as err:
            LOGGER.warning("id_token validation failed: %s", err)
            raise web.HTTPBadRequest(text="Login failed (id_token validation).") from err
        if claims.get("nonce") != pending["nonce"]:
            raise web.HTTPBadRequest(text="Login failed (nonce mismatch).")
        if access_token := body.get("access_token"):
            if userinfo_ep := disc.get("userinfo_endpoint"):
                async with self.http.get(
                    userinfo_ep, headers={"Authorization": f"Bearer {access_token}"}
                ) as resp:
                    if resp.status < 400:
                        userinfo = await resp.json(content_type=None)
                        if not isinstance(userinfo, dict) or userinfo.get("sub") != claims["sub"]:
                            raise web.HTTPBadRequest(text="Login failed (subject mismatch).")
                        # never let userinfo override the token's identity binding
                        for key in ("iss", "aud", "azp", "nonce", "exp", "iat"):
                            userinfo.pop(key, None)
                        claims.update(userinfo)
        if not claims.get("sub"):
            raise web.HTTPBadRequest(text="Login failed (no subject).")

        if not sign_in_allowed(self.config, claims):
            LOGGER.warning(
                "Sign-in refused for %s (not in AM_PARTY_ALLOWED_EMAILS/DOMAINS)",
                claims.get("email") or claims.get("sub"),
            )
            email = claims.get("email")
            return access_denied_page(email if isinstance(email, str) else None)

        user = await self.upsert_user(claims)
        session_id = secrets.token_urlsafe(32)
        await self.db.execute(
            "INSERT INTO sessions(id, user_id, created_at, expires_at) VALUES(?,?,?,?)",
            session_id,
            user.id,
            now(),
            now() + min(SESSION_IDLE_TTL, SESSION_TTL),
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
        email = claims.get("email") if isinstance(claims.get("email"), str) else None
        name = claims.get("name") or claims.get("preferred_username") or email
        name = name if isinstance(name, str) else None
        picture = claims.get("picture") if isinstance(claims.get("picture"), str) else None
        configured_admin = is_configured_admin(self.config, claims)
        # The lock serialises concurrent sign-ins (first-user rule => exactly one admin).
        async with self._user_lock:
            existing = await self.db.fetchone("SELECT * FROM users WHERE sub=?", sub)
            if existing:
                await self.db.execute(
                    "UPDATE users SET email=?, name=?, picture=?, last_login=?, is_admin=? "
                    "WHERE id=?",
                    email,
                    name,
                    picture,
                    now(),
                    int(bool(existing["is_admin"]) or configured_admin),
                    existing["id"],
                )
                if configured_admin and not existing["is_admin"]:
                    LOGGER.info("%s is in AM_PARTY_ADMIN_EMAILS and was made admin", email)
            else:
                if self.config.admin_emails:
                    make_admin = configured_admin  # first-user rule disabled
                else:
                    count = await self.db.fetchone("SELECT COUNT(*) AS n FROM users")
                    make_admin = (count or {}).get("n", 0) == 0
                    if make_admin:
                        LOGGER.warning(
                            "First user %s signed in and was made admin "
                            "(set AM_PARTY_ADMIN_EMAILS to choose admins explicitly)",
                            email or sub,
                        )
                await self.db.execute(
                    "INSERT INTO users(sub, email, name, picture, is_admin, permissions, "
                    "created_at, last_login) VALUES(?,?,?,?,?,?,?,?)",
                    sub, email, name, picture, int(make_admin), "[]", now(), now(),
                )
            row = await self.db.fetchone("SELECT * FROM users WHERE sub=?", sub)
        assert row
        return User.from_row(row)

    async def warn_if_unclaimed(self) -> None:
        """Loudly warn while the first-user-becomes-admin rule is armed."""
        if self.config.admin_emails:
            return
        count = await self.db.fetchone("SELECT COUNT(*) AS n FROM users")
        if (count or {}).get("n", 0) == 0:
            LOGGER.warning(
                "!!! No users yet and AM_PARTY_ADMIN_EMAILS is not set: the FIRST person to sign "
                "in via OIDC becomes ADMIN. Set AM_PARTY_ADMIN_EMAILS (and restrict who can log "
                "in at your identity provider) before exposing am-party publicly. !!!"
            )

    # -- sessions --------------------------------------------------------------
    async def current_user(self, request: web.Request) -> User | None:
        if "user" in request:
            return request["user"]
        user = None
        if sid := request.cookies.get(SESSION_COOKIE):
            t = now()
            row = await self.db.fetchone(
                "SELECT u.*, s.created_at AS s_created_at, s.expires_at AS s_expires_at "
                "FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.id=? AND s.expires_at > ? AND s.created_at > ?",
                sid,
                t,
                t - SESSION_TTL,
            )
            if row:
                user = User.from_row(row)
                # sliding idle expiry, capped by the absolute lifetime
                target = min(row["s_created_at"] + SESSION_TTL, t + SESSION_IDLE_TTL)
                if abs(target - row["s_expires_at"]) > SESSION_REFRESH_SLACK:
                    await self.db.execute(
                        "UPDATE sessions SET expires_at=? WHERE id=?", target, sid
                    )
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
        if permission and not user.has(permission):
            raise web.HTTPForbidden(text="You don't have permission for this")
        return user


def check_azp(claims: dict[str, Any], client_id: str) -> None:
    """OIDC core 3.1.3.7: with several audiences, azp must be us; if present, it must be us."""
    aud = claims.get("aud")
    azp = claims.get("azp")
    if isinstance(aud, list) and len(aud) > 1 and azp is None:
        raise jwt.InvalidAudienceError("multiple audiences but no azp")
    if azp is not None and azp != client_id:
        raise jwt.InvalidAudienceError("azp mismatch")


# -- anonymous guests ------------------------------------------------------------------
# The cookie is "<id>.<hmac>": ids are server-issued, so a client can't pick (or rotate
# through) arbitrary ids without us noticing; forged/unsigned values get a fresh id.
_GUEST_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")


def _guest_sig(secret: str, gid: str) -> str:
    mac = hmac.new(secret.encode(), b"am_party-guest\x00" + gid.encode(), hashlib.sha256)
    return _b64url(mac.digest()[:18])


def guest_id(request: web.Request, secret: str) -> tuple[str, bool]:
    """Return the anonymous guest id for this browser, and whether it is new."""
    raw = request.cookies.get(GUEST_COOKIE) or ""
    gid, sep, sig = raw.rpartition(".")
    if sep and _GUEST_ID_RE.fullmatch(gid) and hmac.compare_digest(sig, _guest_sig(secret, gid)):
        return gid, False
    return secrets.token_urlsafe(18), True


def guest_cookie_value(gid: str, secret: str) -> str:
    return f"{gid}.{_guest_sig(secret, gid)}"


def set_guest_cookie(resp: web.StreamResponse, gid: str, secure: bool, secret: str) -> None:
    resp.set_cookie(
        GUEST_COOKIE,
        guest_cookie_value(gid, secret),
        max_age=GUEST_COOKIE_TTL,
        httponly=True,
        secure=secure,
        samesite="Lax",
        path="/",
    )


# -- client IP ---------------------------------------------------------------------------
_proxy_hint_logged = False


def _normalise_ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        # one subscriber usually owns a whole /64: rate limit by prefix
        return str(ipaddress.IPv6Network(f"{ip}/64", strict=False))
    return str(ip)


def client_ip(request: web.Request, trusted_proxy: str | None) -> str:
    """The client address used for rate limiting.

    Forwarding headers are only believed when AM_PARTY_TRUSTED_PROXY says a proxy sets
    them (otherwise any client could spoof them): ``cloudflare`` -> CF-Connecting-IP,
    ``x-forwarded-for`` -> the right-most X-Forwarded-For entry (the one added by our
    own proxy).
    """
    forwarded: str | None = None
    if trusted_proxy == "cloudflare":
        forwarded = request.headers.get("CF-Connecting-IP")
    elif trusted_proxy == "x-forwarded-for":
        entries = [
            part.strip()
            for header in request.headers.getall("X-Forwarded-For", [])
            for part in header.split(",")
            if part.strip()
        ]
        forwarded = entries[-1] if entries else None
    else:
        global _proxy_hint_logged  # noqa: PLW0603
        if not _proxy_hint_logged and (
            "CF-Connecting-IP" in request.headers or "X-Forwarded-For" in request.headers
        ):
            _proxy_hint_logged = True
            LOGGER.warning(
                "Requests arrive through a proxy but AM_PARTY_TRUSTED_PROXY is unset: every "
                "guest shares the proxy's address for rate limiting. Set "
                "AM_PARTY_TRUSTED_PROXY=cloudflare or x-forwarded-for."
            )
    return _normalise_ip(forwarded) or _normalise_ip(request.remote) or "unknown"

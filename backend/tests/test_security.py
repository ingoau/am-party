"""Security hardening: guest ids, rate limits, CSRF, redirects, admin rules, input checks."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from encore import auth as auth_mod
from encore.api import (
    CSP,
    body,
    csrf_check,
    musickit_page_csp,
    origin_allowed,
    track_id_param,
)
from encore.auth import (
    GUEST_COOKIE,
    AuthManager,
    check_azp,
    client_ip,
    guest_cookie_value,
    guest_id,
    id_token_algorithms,
    is_configured_admin,
    safe_next,
    sign_in_allowed,
)
from encore.config import Config, _email_set, _trusted_proxy
from encore.db import Database
from encore.models import Track
from encore.party import ApiError, Hub, Party
from encore.security import ConnectionCounter, SlidingWindow, TTLCache, valid_track_id

SECRET = "test-secret"


def make_config(**kw: Any) -> Config:
    base = Config(
        data_dir=Path("/tmp"),
        host="0.0.0.0",
        port=8930,
        public_url="https://party.example.com",
        session_secret=SECRET,
        oidc_issuer="https://idp.example.com",
        oidc_client_id="encore",
        oidc_client_secret=None,
        oidc_scopes="openid",
        oidc_name="SSO",
        apple_music_user_token=None,
        apple_music_storefront=None,
        apple_music_developer_token=None,
        widevine_private_key_path=None,
        widevine_client_id_path=None,
    )
    return replace(base, **kw)


def mk_request(
    method: str = "GET",
    path: str = "/api/state",
    headers: dict[str, str] | None = None,
    config: Config | None = None,
    remote: str = "203.0.113.9",
) -> web.Request:
    app = web.Application()
    app["encore"] = SimpleNamespace(config=config or make_config())
    req = make_mocked_request(method, path, headers=headers or {}, app=app)
    return req.clone(remote=remote)


# --------------------------------------------------------------------------- guest ids
def test_guest_id_round_trip() -> None:
    gid, new = guest_id(mk_request(), SECRET)
    assert new
    cookie = guest_cookie_value(gid, SECRET)
    again, new2 = guest_id(mk_request(headers={"Cookie": f"{GUEST_COOKIE}={cookie}"}), SECRET)
    assert (again, new2) == (gid, False)


@pytest.mark.parametrize(
    "cookie",
    [
        "aaaaaaaaaaaaaaaaaaaaaaaa",  # legacy unsigned id
        "aaaaaaaaaaaaaaaaaaaaaaaa.forgedsignature000000",
        "short.sig",
        "",
    ],
)
def test_guest_id_forged_rejected(cookie: str) -> None:
    gid, new = guest_id(mk_request(headers={"Cookie": f"{GUEST_COOKIE}={cookie}"}), SECRET)
    assert new
    assert gid != cookie.split(".")[0]


def test_guest_id_other_secret_rejected() -> None:
    cookie = guest_cookie_value("bbbbbbbbbbbbbbbbbbbbbbbb", "another-secret")
    _, new = guest_id(mk_request(headers={"Cookie": f"{GUEST_COOKIE}={cookie}"}), SECRET)
    assert new


# --------------------------------------------------------------------------- client ip
def test_client_ip_ignores_headers_without_trusted_proxy() -> None:
    req = mk_request(headers={"CF-Connecting-IP": "1.1.1.1", "X-Forwarded-For": "2.2.2.2"})
    assert client_ip(req, None) == "203.0.113.9"


def test_client_ip_cloudflare() -> None:
    req = mk_request(headers={"CF-Connecting-IP": "1.1.1.1", "X-Forwarded-For": "6.6.6.6"})
    assert client_ip(req, "cloudflare") == "1.1.1.1"
    # garbage header falls back to the socket address
    assert client_ip(mk_request(headers={"CF-Connecting-IP": "nope"}), "cloudflare") == "203.0.113.9"


def test_client_ip_xff_uses_rightmost() -> None:
    req = mk_request(headers={"X-Forwarded-For": "6.6.6.6, 7.7.7.7, 8.8.4.4"})
    assert client_ip(req, "x-forwarded-for") == "8.8.4.4"


def test_client_ip_ipv6_grouped_by_64() -> None:
    a = client_ip(mk_request(remote="2001:db8:1:2::1"), None)
    b = client_ip(mk_request(remote="2001:db8:1:2:ffff::9"), None)
    assert a == b == "2001:db8:1:2::/64"


def test_trusted_proxy_config() -> None:
    assert _trusted_proxy(None) is None
    assert _trusted_proxy("Cloudflare") == "cloudflare"
    with pytest.raises(ValueError):
        _trusted_proxy("everything")


# --------------------------------------------------------------------------- rate limits
class FakeApple:
    configured = True

    async def search_tracks(self, q: str, limit: int = 20) -> list[Track]:
        return [Track(id="1", title=q, artist="A")]


@pytest_asyncio.fixture
async def party(tmp_path: Path):  # type: ignore[no-untyped-def]
    db = Database(tmp_path / "p.db")
    await db.open()
    p = Party(db, apple=FakeApple(), hub=Hub())
    await p.load()
    await p.update_settings({"request_cooldown_seconds": 0, "max_pending_per_guest": 100})
    for i in range(40):
        p._cache_track(Track(id=str(i), title=f"Song {i}", artist="A"))  # noqa: SLF001
    yield p
    await db.close()


@pytest.mark.asyncio
async def test_search_limited_per_ip_across_guest_ids(party: Party) -> None:
    p = party
    p.set_rate_limits(ip_search_per_min=5, ip_max_pending=20)
    for i in range(5):
        await p.search("x", guest=f"guest-{i}", ip="1.1.1.1")  # fresh guest id every time
    with pytest.raises(ApiError) as err:
        await p.search("x", guest="guest-new", ip="1.1.1.1")
    assert err.value.status == 429
    # another IP is unaffected
    await p.search("x", guest="guest-other", ip="2.2.2.2")


@pytest.mark.asyncio
async def test_search_per_ip_via_trusted_proxy(party: Party) -> None:
    p = party
    p.set_rate_limits(ip_search_per_min=3, ip_max_pending=20)
    cfg = make_config(trusted_proxy="cloudflare")
    ips = [
        client_ip(mk_request(headers={"CF-Connecting-IP": "9.9.9.9"}, config=cfg), "cloudflare")
        for _ in range(4)
    ]
    for i, ip in enumerate(ips[:3]):
        await p.search("x", guest=f"g{i}", ip=ip)
    with pytest.raises(ApiError):
        await p.search("x", guest="g4", ip=ips[3])


@pytest.mark.asyncio
async def test_search_global_ceiling(party: Party) -> None:
    p = party
    p._all_searches = SlidingWindow(4, 60, 1)  # noqa: SLF001
    for i in range(4):
        await p.search("x", guest=f"g{i}", ip=f"10.0.0.{i}")
    with pytest.raises(ApiError) as err:
        await p.search("x", guest="g9", ip="10.0.0.9")
    assert err.value.status == 429
    # admin searches (no guest/ip) are not limited
    await p.search("x")


@pytest.mark.asyncio
async def test_requests_pending_limited_per_ip(party: Party) -> None:
    p = party
    p.set_rate_limits(ip_search_per_min=100, ip_max_pending=3)
    p._ip_last_request = TTLCache(10, -1.0)  # noqa: SLF001  (no per-IP cooldown here)
    for i in range(3):
        await p.create_request(f"guest-{i}", str(i), ip="1.1.1.1")
    with pytest.raises(ApiError) as err:
        await p.create_request("guest-new", "10", ip="1.1.1.1")
    assert err.value.status == 429
    await p.create_request("guest-x", "11", ip="2.2.2.2")
    # once one is decided, the IP may request again
    await p.db.execute("UPDATE requests SET status='rejected' WHERE guest_id='guest-0'")
    await p.create_request("guest-new", "12", ip="1.1.1.1")


@pytest.mark.asyncio
async def test_requests_ip_cooldown(party: Party) -> None:
    p = party
    await p.create_request("guest-a", "1", ip="1.1.1.1")
    with pytest.raises(ApiError) as err:
        await p.create_request("guest-b", "2", ip="1.1.1.1")
    assert err.value.status == 429


@pytest.mark.asyncio
async def test_requests_global_pending_ceiling(party: Party, monkeypatch: pytest.MonkeyPatch) -> None:
    import encore.party as party_mod

    monkeypatch.setattr(party_mod, "MAX_PENDING_TOTAL", 2)
    p = party
    await p.create_request("g1", "1")
    await p.create_request("g2", "2")
    with pytest.raises(ApiError) as err:
        await p.create_request("g3", "3")
    assert err.value.status == 429


def test_ttl_cache_is_bounded_and_expires() -> None:
    t = [0.0]
    c: TTLCache[str, int] = TTLCache(3, 10, clock=lambda: t[0])
    for i in range(10):
        c.set(f"k{i}", i)
    assert len(c) == 3 and c.get("k9") == 9 and c.get("k0") is None
    t[0] = 11
    assert c.get("k9") is None and len(c) == 0


def test_connection_counter() -> None:
    cc = ConnectionCounter(per_key=2, total=3)
    assert cc.acquire("a") and cc.acquire("a") and not cc.acquire("a")
    assert cc.acquire("b") and not cc.acquire("c")
    cc.release("a")
    assert cc.acquire("c") and cc.active == 3


# --------------------------------------------------------------------------- csrf
def post(headers: dict[str, str], config: Config | None = None, host: str = "party.example.com") -> web.Request:
    return mk_request("POST", "/api/requests", headers={"Host": host, **headers}, config=config)


def test_csrf_exact_public_origin() -> None:
    csrf_check(post({"Origin": "https://party.example.com"}))
    csrf_check(post({"Origin": "https://PARTY.example.com:443"}))


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.com",
        "https://party.example.com.evil.com",
        "http://party.example.com",  # scheme must match
        "https://party.example.com:8443",
        "null",
    ],
)
def test_csrf_rejects_other_origins(origin: str) -> None:
    with pytest.raises(ApiError) as err:
        csrf_check(post({"Origin": origin}))
    assert err.value.status == 403


def test_csrf_old_prefix_bypass_closed() -> None:
    # previously: origin host startswith Host header -> allowed
    with pytest.raises(ApiError):
        csrf_check(post({"Origin": "http://192.168.1.50:8930.evil.com"}, host="192.168.1.50:8930"))


def test_csrf_lan_request_own_origin() -> None:
    # LAN access by IP: Origin equals the request's own scheme+host
    csrf_check(post({"Origin": "http://192.168.1.50:8930"}, host="192.168.1.50:8930"))


def test_csrf_missing_origin_uses_fetch_metadata() -> None:
    csrf_check(post({"Sec-Fetch-Site": "same-origin"}))
    for bad in ({"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}, {}):
        with pytest.raises(ApiError):
            csrf_check(post(bad))


def test_csrf_get_is_exempt() -> None:
    csrf_check(mk_request("GET", headers={"Origin": "https://evil.com"}))


def test_ws_origin_check() -> None:
    assert origin_allowed(mk_request(headers={"Host": "party.example.com"}))
    assert origin_allowed(
        mk_request(headers={"Host": "party.example.com", "Origin": "https://party.example.com"})
    )
    assert not origin_allowed(
        mk_request(headers={"Host": "party.example.com", "Origin": "https://evil.com"})
    )


@pytest.mark.asyncio
async def test_body_requires_json_content_type() -> None:
    req = make_mocked_request(
        "POST", "/api/requests", headers={"Content-Type": "text/plain"}, payload=None
    )
    with pytest.raises(ApiError) as err:
        await body(req)
    assert err.value.status == 415


# --------------------------------------------------------------------------- redirects
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/admin", "/admin"),
        ("/admin/settings?tab=users", "/admin/settings?tab=users"),
        ("/admin/apple-music/connect", "/admin/apple-music/connect"),
        ("//evil.com", "/admin"),
        ("/\\evil.com", "/admin"),
        ("\\\\evil.com", "/admin"),
        ("https://evil.com/admin", "/admin"),
        ("/admin\r\nSet-Cookie: x", "/admin"),
        ("/\tevil.com", "/admin"),
        ("/", "/admin"),
        ("/administrator", "/admin"),
        ("", "/admin"),
        (None, "/admin"),
    ],
)
def test_safe_next(value: str | None, expected: str) -> None:
    assert safe_next(value) == expected


# --------------------------------------------------------------------------- admin / allowlist
def test_email_set_parsing() -> None:
    assert _email_set(" A@x.com, b@Y.com ;c@z.com") == {"a@x.com", "b@y.com", "c@z.com"}


def test_allowlist() -> None:
    open_cfg = make_config()
    assert sign_in_allowed(open_cfg, {"sub": "1"})
    cfg = make_config(
        allowed_emails=frozenset({"friend@gmail.com"}),
        allowed_domains=frozenset({"example.org"}),
        admin_emails=frozenset({"host@example.net"}),
    )
    assert sign_in_allowed(cfg, {"email": "Friend@Gmail.com", "email_verified": True})
    assert sign_in_allowed(cfg, {"email": "anyone@example.org"})
    assert sign_in_allowed(cfg, {"email": "host@example.net"})
    assert not sign_in_allowed(cfg, {"email": "anyone@example.org", "email_verified": False})
    assert not sign_in_allowed(cfg, {"email": "anyone@example.org", "email_verified": "false"})
    assert not sign_in_allowed(cfg, {"email": "x@evil.example.org"})
    assert not sign_in_allowed(cfg, {"sub": "no-email"})


def test_configured_admin_requires_verified_email() -> None:
    cfg = make_config(admin_emails=frozenset({"host@example.net"}))
    assert is_configured_admin(cfg, {"email": "HOST@example.net"})
    assert not is_configured_admin(cfg, {"email": "host@example.net", "email_verified": False})
    assert not is_configured_admin(cfg, {"email": "other@example.net"})


async def make_auth(tmp_path: Path, **cfg: Any) -> AuthManager:
    db = Database(tmp_path / "a.db")
    await db.open()
    return AuthManager(make_config(**cfg), db, http=None)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def auth(tmp_path: Path):  # type: ignore[no-untyped-def]
    am = await make_auth(tmp_path)
    yield am
    await am.db.close()


@pytest.mark.asyncio
async def test_first_user_rule(auth: AuthManager) -> None:
    am = auth
    import asyncio

    users = await asyncio.gather(
        *(am.upsert_user({"sub": f"s{i}", "email": f"u{i}@x.com"}) for i in range(5))
    )
    assert sum(u.is_admin for u in users) == 1


@pytest.mark.asyncio
async def test_admin_emails_disable_first_user_rule(auth: AuthManager) -> None:
    am = auth
    am.config = replace(am.config, admin_emails=frozenset({"host@x.com"}))
    first = await am.upsert_user({"sub": "a", "email": "attacker@x.com"})
    assert not first.is_admin
    host = await am.upsert_user({"sub": "b", "email": "host@x.com"})
    assert host.is_admin
    unverified = await am.upsert_user({"sub": "c", "email": "host@x.com", "email_verified": False})
    assert not unverified.is_admin
    # an existing user is promoted when their (verified) email is listed
    am.config = replace(am.config, admin_emails=frozenset({"host@x.com", "attacker@x.com"}))
    assert (await am.upsert_user({"sub": "a", "email": "attacker@x.com"})).is_admin


@pytest.mark.asyncio
async def test_unclaimed_warning(auth: AuthManager, caplog: pytest.LogCaptureFixture) -> None:
    am = auth
    with caplog.at_level("WARNING"):
        await am.warn_if_unclaimed()
    assert "ENCORE_ADMIN_EMAILS" in caplog.text


# --------------------------------------------------------------------------- oidc
def test_id_token_algorithms() -> None:
    disc = ["RS256", "ES256", "HS256", "none"]
    assert id_token_algorithms({"kty": "RSA", "alg": "RS256"}, disc) == ["RS256"]
    assert id_token_algorithms({"kty": "RSA"}, disc) == ["RS256"]
    assert id_token_algorithms({"kty": "EC"}, disc) == ["ES256"]
    assert id_token_algorithms({"kty": "oct"}, disc) == []
    assert id_token_algorithms({"kty": "RSA", "alg": "HS256"}, disc) == []
    assert id_token_algorithms({"kty": "RSA"}, None) == ["RS256"]


def test_check_azp() -> None:
    check_azp({"aud": "encore"}, "encore")
    check_azp({"aud": ["encore", "other"], "azp": "encore"}, "encore")
    with pytest.raises(jwt.InvalidAudienceError):
        check_azp({"aud": ["encore", "other"]}, "encore")
    with pytest.raises(jwt.InvalidAudienceError):
        check_azp({"aud": "encore", "azp": "other"}, "encore")


@pytest.mark.asyncio
async def test_pending_logins_bounded(auth: AuthManager, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_mod, "MAX_PENDING_LOGINS", 5)
    am = auth

    async def disc() -> dict[str, Any]:
        return {"authorization_endpoint": "https://idp.example.com/authorize"}

    am.discovery = disc  # type: ignore[method-assign]
    for _ in range(20):
        with pytest.raises(web.HTTPFound):
            await am.login_redirect(mk_request(path="/auth/login?next=//evil.com"))
    assert len(am._pending) == 5  # noqa: SLF001
    assert {p["next"] for p in am._pending.values()} == {"/admin"}  # noqa: SLF001


# --------------------------------------------------------------------------- track ids
@pytest.mark.parametrize("tid", ["1440818839", "i.AbC123", "l.x9", "a.1", "p.Zz"])
def test_valid_track_ids(tid: str) -> None:
    assert valid_track_id(tid)
    assert track_id_param({"track_id": tid}) == tid


@pytest.mark.parametrize(
    "tid",
    ["../me/library", "1/../../x", "pl.abc", "i.", "1" * 21, "١٢٣", "1\n", "i.a/b", "x.abc", ""],
)
def test_invalid_track_ids(tid: str) -> None:
    assert not valid_track_id(tid)
    with pytest.raises(ApiError) as err:
        track_id_param({"track_id": tid})
    assert err.value.status == 400


def test_numeric_track_id_accepted() -> None:
    assert track_id_param({"track_id": 1440818839}) == "1440818839"


@pytest.mark.asyncio
async def test_party_get_track_validates(party: Party) -> None:
    p = party
    with pytest.raises(ApiError) as err:
        await p.get_track("../../me/library/songs")
    assert err.value.status == 400


@pytest.mark.asyncio
async def test_apple_client_rejects_bad_ids() -> None:
    from encore.models import ProviderError
    from encore.providers.apple_music.client import _require_track_id

    _require_track_id("123")
    with pytest.raises(ProviderError):
        _require_track_id("123/../../me")


# --------------------------------------------------------------------------- headers
def test_spa_csp() -> None:
    assert "script-src 'self'" in CSP and "frame-ancestors 'none'" in CSP
    assert "unsafe-eval" not in CSP


def test_musickit_page_csp_nonce_and_handler_hashes() -> None:
    page = (
        '<script>const A=1;</script><script src="https://js-cdn.music.apple.com/x.js" async>'
        '</script><button onclick="go();">x</button>'
    )
    out, csp = musickit_page_csp(page)
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert out.count(f'<script nonce="{nonce}"') == 2
    digest = base64.b64encode(hashlib.sha256(b"go();").digest()).decode()
    assert f"'sha256-{digest}'" in csp and "'unsafe-hashes'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]


def test_sendspin_reserved_client_id() -> None:
    import asyncio

    from aiosendspin.server.compliance import ClientComplianceError

    from encore.player.sendspin_out import ANCHOR_CLIENT_ID, _TrustAllPairingStore

    store = _TrustAllPairingStore()
    with pytest.raises(ClientComplianceError):
        asyncio.run(store.trusted_unpaired(ANCHOR_CLIENT_ID))
    assert asyncio.run(store.trusted_unpaired("browser-123")) is not None

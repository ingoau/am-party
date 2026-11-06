"""End-to-end id_token verification against a JWKS endpoint (regression: Cloudflare
blocked PyJWKClient's urllib User-Agent, so every login failed)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import aiohttp
import jwt
import pytest
import pytest_asyncio
from aiohttp import web
from cryptography.hazmat.primitives.asymmetric import rsa

from am_party.auth import AuthManager

CLIENT_ID = "client-123"


def make_key(kid: str) -> tuple[rsa.RSAPrivateKey, dict]:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(priv.public_key(), as_dict=True)
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return priv, jwk


def sign(priv: rsa.RSAPrivateKey, kid: str, issuer: str, **extra) -> str:
    now = int(time.time())
    claims = {"iss": issuer, "aud": CLIENT_ID, "sub": "user-1", "iat": now, "exp": now + 300}
    claims.update(extra)
    return jwt.encode(claims, priv, algorithm="RS256", headers={"kid": kid})


@pytest_asyncio.fixture
async def idp():  # noqa: ANN201
    state = {"keys": [], "fetches": 0}

    async def jwks(request: web.Request) -> web.Response:
        # behave like a WAF that blocks Python's default urllib client
        if request.headers.get("User-Agent", "").startswith("Python-urllib"):
            return web.Response(status=403)
        state["fetches"] += 1
        return web.json_response({"keys": state["keys"]})

    app = web.Application()
    app.router.add_get("/jwks", jwks)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    state["uri"] = f"http://127.0.0.1:{port}/jwks"
    state["issuer"] = f"http://127.0.0.1:{port}/application/o/am-party/"
    yield state
    await runner.cleanup()


def manager(http: aiohttp.ClientSession, issuer: str) -> AuthManager:
    cfg = SimpleNamespace(oidc_client_id=CLIENT_ID, oidc_issuer=issuer.rstrip("/"))
    return AuthManager(cfg, db=None, http=http)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_verifies_rs256_token_with_trailing_slash_issuer(idp) -> None:  # noqa: ANN001
    priv, jwk = make_key("k1")
    idp["keys"] = [jwk]
    disc = {"jwks_uri": idp["uri"], "id_token_signing_alg_values_supported": ["RS256"]}
    async with aiohttp.ClientSession() as http:
        auth = manager(http, idp["issuer"])
        claims = await auth.verify_id_token(sign(priv, "k1", idp["issuer"]), disc)
        assert claims["sub"] == "user-1"
        # cached: second verification doesn't refetch
        await auth.verify_id_token(sign(priv, "k1", idp["issuer"]), disc)
        assert idp["fetches"] == 1


@pytest.mark.asyncio
async def test_key_rotation_refetches_once(idp) -> None:  # noqa: ANN001
    old, old_jwk = make_key("old")
    new, new_jwk = make_key("new")
    idp["keys"] = [old_jwk]
    disc = {"jwks_uri": idp["uri"], "id_token_signing_alg_values_supported": ["RS256"]}
    async with aiohttp.ClientSession() as http:
        auth = manager(http, idp["issuer"])
        await auth.verify_id_token(sign(old, "old", idp["issuer"]), disc)
        idp["keys"] = [old_jwk, new_jwk]
        auth._jwks_at -= 60  # past the refetch guard  # noqa: SLF001
        claims = await auth.verify_id_token(sign(new, "new", idp["issuer"]), disc)
        assert claims["sub"] == "user-1" and idp["fetches"] == 2


@pytest.mark.asyncio
async def test_rejects_wrong_key_audience_and_issuer(idp) -> None:  # noqa: ANN001
    priv, jwk = make_key("k1")
    attacker, _ = make_key("k1")
    idp["keys"] = [jwk]
    disc = {"jwks_uri": idp["uri"], "id_token_signing_alg_values_supported": ["RS256"]}
    async with aiohttp.ClientSession() as http:
        auth = manager(http, idp["issuer"])
        with pytest.raises(jwt.InvalidSignatureError):
            await auth.verify_id_token(sign(attacker, "k1", idp["issuer"]), disc)
        with pytest.raises(jwt.InvalidAudienceError):
            await auth.verify_id_token(sign(priv, "k1", idp["issuer"], aud="other"), disc)
        with pytest.raises(jwt.InvalidIssuerError):
            await auth.verify_id_token(sign(priv, "k1", "https://evil.example/"), disc)
        with pytest.raises(jwt.PyJWKClientError):
            await auth.verify_id_token(sign(priv, "missing", idp["issuer"]), disc)

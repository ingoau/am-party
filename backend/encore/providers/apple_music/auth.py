"""Apple Music sign-in helpers: MusicKit JS auth page and token checks.

Adapted from Music Assistant (Apache-2.0), https://github.com/music-assistant/server
(music_assistant/providers/apple_music/setup_flow.py)
Modifications: MA's SetupSession/external-step flow and dynamic routes are replaced by
render_musickit_auth_page(), which returns one self-contained HTML page (config and CSS
inlined) for Encore's admin UI to serve; _app_token_accepted() became
validate_developer_token(); added developer_token_expiry(). is_plausible_user_token()
keeps MA's _validate_user_token() rule.
"""

from __future__ import annotations

import base64
import html
import json
import re
from functools import cache
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import ClientError, ClientTimeout

from .errors import ResourceTemporarilyUnavailable

_ASSET_DIR = Path(__file__).parent / "musickit_auth"
_TEST_URL = "https://api.music.apple.com/v1/test"


@cache
def _assets() -> tuple[str, str]:
    page = (_ASSET_DIR / "musickit_wrapper.html").read_text(encoding="utf-8")
    css = (_ASSET_DIR / "musickit_wrapper.css").read_text(encoding="utf-8")
    return page, css


def _js_literal(value: Any) -> str:
    """JSON-encode for embedding in an inline <script> (neutralises </script>, <!--, U+2028)."""
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def render_musickit_auth_page(
    developer_token: str, callback_url: str, app_name: str = "Encore", app_build: str = "1.0.0"
) -> str:
    """
    Return the full HTML of the MusicKit sign-in page.

    The page loads MusicKit JS v3 from js-cdn.music.apple.com, configures it with the
    developer token, and after ``music.authorize()`` POSTs
    ``{"music_user_token": str, "music_user_token_timestamp": int}`` (JSON, same-origin
    credentials) to ``callback_url``. On a 2xx it navigates to /admin/settings; otherwise
    it shows the response's ``detail``/``error``/``message`` (or the HTTP status).

    MusicKit's authorize() popup needs the page to be served from a real http(s) origin.
    The developer token is necessarily visible to whoever can open this page.
    """
    page, css = _assets()
    config = {
        "developer_token": developer_token,
        "callback_url": callback_url,
        "app_name": app_name,
        "app_build": app_build,
    }
    config_js = f"const ENCORE_CONFIG = Object.freeze({_js_literal(config)});"
    page = page.replace("<title>Connect Apple Music</title>", f"<title>Connect Apple Music – {html.escape(app_name)}</title>")
    page = page.replace("/*__ENCORE_CSS__*/", css.replace("</", "<\\/"))
    return page.replace("/*__ENCORE_CONFIG__*/", config_js)


async def validate_developer_token(http: aiohttp.ClientSession, token: str) -> bool:
    """
    Check a developer token against Apple's ``GET /v1/test``.

    Returns True when accepted, False when rejected (401/403) or empty. Raises
    ResourceTemporarilyUnavailable (a ProviderError) when the answer is inconclusive
    (throttled, 5xx, network error) -- a throttled check says nothing about validity.
    """
    token = (token or "").strip()
    if not token:
        return False
    try:
        async with http.get(
            _TEST_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=ClientTimeout(total=10),
        ) as response:
            if response.status == 200:
                return True
            if response.status in (401, 403):
                return False
            raise ResourceTemporarilyUnavailable(
                f"Apple Music token check inconclusive (HTTP {response.status})"
            )
    except (ClientError, TimeoutError) as err:
        raise ResourceTemporarilyUnavailable(f"Apple Music token check failed: {err}") from err


def developer_token_expiry(token: str) -> float | None:
    """Return the ``exp`` claim (unix seconds) of a developer token JWT, without verifying it."""
    try:
        parts = (token or "").strip().split(".")
        if len(parts) != 3:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = claims.get("exp") if isinstance(claims, dict) else None
        if isinstance(exp, bool) or not isinstance(exp, (int, float)):
            return None
        return float(exp)
    except ValueError, TypeError:
        return None


_USER_TOKEN_RE = re.compile(r"[a-zA-Z0-9=/+]{32,}==$")


def is_plausible_user_token(token: Any) -> bool:
    """Return whether a value looks like a (base64) Music-User-Token (MA's heuristic)."""
    if not isinstance(token, str):
        return False
    return bool(_USER_TOKEN_RE.search(token.strip()))

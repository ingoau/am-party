"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _read_secret(name: str) -> str | None:
    """Read a secret from ENV or from a file pointed to by ENV_FILE (docker secrets style)."""
    file_path = _env(f"{name}_FILE")
    if file_path and Path(file_path).is_file():
        return Path(file_path).read_text().strip()
    return _env(name)


@dataclass
class Config:
    data_dir: Path
    host: str
    port: int
    public_url: str
    session_secret: str
    oidc_issuer: str | None
    oidc_client_id: str | None
    oidc_client_secret: str | None
    oidc_scopes: str
    oidc_name: str
    # Apple Music
    apple_music_user_token: str | None
    apple_music_storefront: str | None
    apple_music_developer_token: str | None
    widevine_private_key_path: Path | None
    widevine_client_id_path: Path | None
    # misc
    dev_mode: bool = False
    static_dir: Path = field(default_factory=lambda: Path(__file__).parent / "static")

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id)

    @property
    def cookie_secure(self) -> bool:
        return self.public_url.startswith("https://")


def load_config() -> Config:
    data_dir = Path(_env("ENCORE_DATA_DIR", "/data"))
    data_dir.mkdir(parents=True, exist_ok=True)

    # Session secret: env, else persisted random in data dir.
    session_secret = _read_secret("ENCORE_SESSION_SECRET")
    if not session_secret:
        secret_file = data_dir / ".session_secret"
        if secret_file.exists():
            session_secret = secret_file.read_text().strip()
        else:
            session_secret = secrets.token_urlsafe(48)
            secret_file.write_text(session_secret)
            secret_file.chmod(0o600)

    secrets_dir = Path(_env("ENCORE_SECRETS_DIR", "/run/secrets"))

    def _secret_path(env_name: str, default_name: str) -> Path | None:
        p = _env(env_name)
        if p:
            return Path(p)
        candidate = secrets_dir / default_name
        return candidate if candidate.exists() else None

    return Config(
        data_dir=data_dir,
        host=_env("ENCORE_HOST", "0.0.0.0"),
        port=int(_env("ENCORE_PORT", "8930")),
        public_url=(_env("ENCORE_PUBLIC_URL", "http://localhost:8930") or "").rstrip("/"),
        session_secret=session_secret,
        oidc_issuer=(_env("OIDC_ISSUER") or "").rstrip("/") or None,
        oidc_client_id=_read_secret("OIDC_CLIENT_ID"),
        oidc_client_secret=_read_secret("OIDC_CLIENT_SECRET"),
        oidc_scopes=_env("OIDC_SCOPES", "openid profile email"),
        oidc_name=_env("OIDC_PROVIDER_NAME", "SSO"),
        apple_music_user_token=_read_secret("APPLE_MUSIC_USER_TOKEN")
        or _file_if_exists(secrets_dir / "apple_music_user_token"),
        apple_music_storefront=_env("APPLE_MUSIC_STOREFRONT"),
        apple_music_developer_token=_read_secret("APPLE_MUSIC_DEVELOPER_TOKEN"),
        widevine_private_key_path=_secret_path("WIDEVINE_PRIVATE_KEY", "private_key.pem"),
        widevine_client_id_path=_secret_path("WIDEVINE_CLIENT_ID", "client_id.bin"),
        dev_mode=_env("ENCORE_DEV", "0") == "1",
        static_dir=Path(_env("ENCORE_STATIC_DIR", str(Path(__file__).parent / "static"))),
    )


def _file_if_exists(path: Path) -> str | None:
    if path.is_file():
        return path.read_text().strip() or None
    return None

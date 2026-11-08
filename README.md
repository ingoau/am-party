# am-party

[![Container](https://github.com/ingoau/am-party/actions/workflows/container.yml/badge.svg)](https://github.com/ingoau/am-party/actions/workflows/container.yml)

A self-hosted party jukebox. Any device opens the page and plays the party music **in tight sync** (Sendspin protocol). Guests scan a QR code to request songs from **Apple Music**, every request goes through an **OIDC-authenticated approval panel**, and tracks are mixed with Music Assistant's **smart crossfade**.

- `/` — guest page (no login): search Apple Music, request songs, see request status
- `/tv` — TV / listening screen: now playing, up next, QR code, listen toggle, mute and volume per device
- `/stream` — listen-only page: connect a device (phone, laptop, speaker box) to the audio stream without the TV screen
- `/admin` — sign in with your OIDC provider. The **first user to sign in becomes admin**; everyone after that has no privileges until an admin grants them (approve requests, manage queue, admin)
- `/admin/speakers` — speakers: people with the **Claim speakers** permission scan the QR code on a listening TV (or "Claim this speaker" on `/stream`) to claim it, then mute it, set its volume, lock its local controls, rename it, and share or hand it over to others; admins see and control every speaker

## How it works

```
Apple Music ──(webPlayback + Widevine key)──► ffmpeg decrypt/decode ──► PCM cache (+ EBU R128 loudness)
                                                                   └─► smart-fade analysis worker (beats, key, energy, vocals)
queue/requests/fallback playlist ─► engine ─► smart/standard crossfade render ─► Sendspin server ─► browsers (synced)
```

- Upcoming tracks are decoded and analysed **before** they play, so smart crossfades work on first play.
- When the approved queue is empty, a **fallback playlist** (Apple Music library or catalog playlist) plays, shuffled.
- Everything runs on one origin/port: the web app, `/api`, and the Sendspin WebSocket at `/sendspin`.

## Run it

```sh
cp .env.example .env      # set AM_PARTY_PUBLIC_URL and the OIDC_* values
mkdir -p data secrets     # create these yourself so they aren't created root-owned
docker compose up -d --build
docker compose logs -f    # WARN lines tell you if OIDC / Apple Music still need setup
```

To use the prebuilt image instead of building locally, set `image: ghcr.io/ingoau/am-party:latest`
in `compose.yaml`, remove the `build:` block, and run `docker compose up -d`. Images are built
by GitHub Actions (linux/amd64) after the test suite passes: `latest` tracks `main`, `vX.Y.Z`
tags publish `X.Y.Z` and `X.Y`, and every build is also tagged `sha-<commit>`.

The container starts as root only long enough to hand a root-owned `./data` to uid 1000,
then drops privileges (`setpriv`). If you run it with `user:` set and `./data` isn't
writable, it exits with the `chown` command to fix it. Logs are capped (json-file, 3 x
10 MB); `compose.yaml` has commented-out `mem_limit`/`cpus` caps and a LAN Sendspin port.

Then:

1. Register an OIDC client with your provider (Authentik, Pocket ID, Keycloak, Authelia, Google…):
   - redirect URI: `${AM_PARTY_PUBLIC_URL}/auth/callback`
   - scopes: `openid profile email`; confidential client (secret) or public client with PKCE both work
   - **restrict who can log in at the provider** (bind the app to a group / user list). am-party trusts every identity the provider vouches for.
2. Set `AM_PARTY_ADMIN_EMAILS=you@example.com` (recommended), then open `${AM_PARTY_PUBLIC_URL}/admin` and sign in. Without `AM_PARTY_ADMIN_EMAILS` the *first* person to sign in becomes admin (the log shows a warning until someone has).
3. **Settings → Apple Music**: click **Connect Apple Music** and sign in with your Apple ID (MusicKit, same flow as Music Assistant). Or provide tokens as files, see below.
4. **Settings → Fallback playlist**: pick one of your playlists or paste an Apple Music playlist link.
5. Open `/tv` on the TVs/speakers, tap **Listen**. Guests scan the QR code.

Put it behind HTTPS (e.g. Cloudflare Tunnel or Tailscale Funnel) so guests' phones can reach it: browsers need a secure context for Opus decoding, and the MusicKit sign-in needs a real https origin.

### Apple Music credentials

Streaming needs three things, all of which can be dropped into `./secrets/` (mounted read-only at `/run/secrets`):

| File | What |
|---|---|
| `apple_music_developer_token` | MusicKit developer token (ES256 JWT from an Apple Developer account). Can also be pasted in Settings. |
| `apple_music_user_token` | Music-User-Token. Easiest via **Connect Apple Music** in Settings (UI-set tokens take precedence over files). |
| `client_id.bin`, `private_key.pem` | Widevine L3 device files used to obtain content keys (same files Music Assistant uses). |

Apple allows one concurrent stream per account — don't play the same account elsewhere during the party.

### Configuration (env)

| Variable | Default | |
|---|---|---|
| `AM_PARTY_PUBLIC_URL` | `http://localhost:8930` | public base URL (QR code, OIDC redirect, cookies `Secure` when https) |
| `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | — | generic OIDC; `*_FILE` variants supported |
| `OIDC_PROVIDER_NAME` | `SSO` | label on the sign-in button |
| `OIDC_SCOPES` | `openid profile email` | |
| `AM_PARTY_ADMIN_EMAILS` | — | comma-separated emails that become admin when they sign in (email must not be `email_verified: false`). When set, the "first user becomes admin" rule is off. |
| `AM_PARTY_ALLOWED_EMAILS`, `AM_PARTY_ALLOWED_DOMAINS` | — | optional sign-in allowlists (comma-separated; e.g. `example.org`). Anyone else gets a 403 page. Admin emails are always allowed. |
| `AM_PARTY_TRUSTED_PROXY` | unset | `cloudflare` (use `CF-Connecting-IP`) or `x-forwarded-for` (right-most entry) — only when am-party is reachable **solely** through that proxy; otherwise clients could spoof their IP. Unset = the socket address. |
| `AM_PARTY_IP_SEARCHES_PER_MINUTE` | `120` | guest searches per client IP per minute |
| `AM_PARTY_IP_MAX_PENDING` | `20` | pending guest requests per client IP |
| `AM_PARTY_MAX_LISTENERS` | `100` | max concurrent `/sendspin` connections |
| `AM_PARTY_SENDSPIN_PORT` | unset (off) | also run the native Sendspin listener on this port (plain `ws://`, for hardware players on the LAN; publish it in compose) |
| `AM_PARTY_HOST_PORT` | `8930` | host port in compose |
| `AM_PARTY_LOG_LEVEL` | `INFO` | |

### Security notes

- **Who can sign in** is decided by your OIDC provider: restrict the am-party application to the people who should manage the party. Use `AM_PARTY_ADMIN_EMAILS` so nobody can claim admin by signing in first, and optionally `AM_PARTY_ALLOWED_EMAILS`/`AM_PARTY_ALLOWED_DOMAINS` as a second gate.
- **Guests** get a server-signed anonymous cookie (HMAC with the session secret). Guest searches and requests are rate limited per guest, per client IP and globally (600 searches/min, 200 pending requests in total). Everyone on the party wifi usually shares one public IP, so per-IP limits are deliberately loose; raise them for big parties. Behind Cloudflare Tunnel or a reverse proxy set `AM_PARTY_TRUSTED_PROXY`, or every guest shares the proxy's address.
- Sessions last at most 30 days and expire after 7 days unused. Signing out closes that session's live connections.
- Live-update websockets are capped per IP (100) and in total (500); `/sendspin` per IP (50) and in total (`AM_PARTY_MAX_LISTENERS`).
- Responses carry a strict Content-Security-Policy (`script-src 'self'`), `X-Frame-Options: DENY` and HSTS when `AM_PARTY_PUBLIC_URL` is https.

## Development

- Backend: Python 3.14, aiohttp (`backend/am_party`). Tests: `backend/tests` (engine, Apple Music parsers, Sendspin, smart fades).
  ```sh
  docker build --target test -t am-party:test .   # runtime image + pytest, tests baked in
  docker run --rm am-party:test                     # runs the whole suite
  # against your working tree without rebuilding:
  docker run --rm -v $PWD/backend:/app -w /app am-party:test python -m pytest -q -n auto --dist worksteal -p no:cacheprovider tests
  ```
  Python dependencies: `backend/requirements.txt` (runtime, the only source of truth) and
  `backend/requirements-dev.txt` (tests).
- Frontend: Vite + React (`web/`). `npm run dev` proxies to `localhost:8930`; `web/mock/server.mjs` is a dependency-free mock backend.
- API contract: [docs/API.md](docs/API.md).

## License

am-party is licensed under the [GNU General Public License v3.0](LICENSE). It includes Apache-2.0 code from Music Assistant (compatible with GPL-3.0) and its container image bundles GPL-3.0 pywidevine.

## Credits

am-party vendors code from [Music Assistant](https://github.com/music-assistant/server) (Apache-2.0): the Apple Music provider and the smart fades planner/renderer/analysis. See [NOTICE.md](NOTICE.md) for details and third-party model attributions (Beat This!, S-KEY, FireRedVAD).

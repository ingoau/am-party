# Encore

A self-hosted party jukebox. Any device opens the page and plays the party music **in tight sync** (Sendspin protocol). Guests scan a QR code to request songs from **Apple Music**, every request goes through an **OIDC-authenticated approval panel**, and tracks are mixed with Music Assistant's **smart crossfade**.

- `/` — TV / listening screen: now playing, up next, QR code, listen toggle, mute and volume per device
- `/request` — guest page (no login): search Apple Music, request songs, see request status
- `/admin` — sign in with your OIDC provider. The **first user to sign in becomes admin**; everyone after that has no privileges until an admin grants them (approve requests, manage queue, admin)

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
cp .env.example .env      # set ENCORE_PUBLIC_URL and the OIDC_* values
docker compose up -d --build
```

Then:

1. Register an OIDC client with your provider (Authentik, Pocket ID, Keycloak, Authelia, Google…):
   - redirect URI: `${ENCORE_PUBLIC_URL}/auth/callback`
   - scopes: `openid profile email`; confidential client (secret) or public client with PKCE both work
2. Open `${ENCORE_PUBLIC_URL}/admin` and sign in — you're the admin.
3. **Settings → Apple Music**: click **Connect Apple Music** and sign in with your Apple ID (MusicKit, same flow as Music Assistant). Or provide tokens as files, see below.
4. **Settings → Fallback playlist**: pick one of your playlists or paste an Apple Music playlist link.
5. Open `/` on the TVs/speakers, tap **Listen**. Guests scan the QR code.

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
| `ENCORE_PUBLIC_URL` | `http://localhost:8930` | public base URL (QR code, OIDC redirect, cookies `Secure` when https) |
| `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | — | generic OIDC; `*_FILE` variants supported |
| `OIDC_PROVIDER_NAME` | `SSO` | label on the sign-in button |
| `OIDC_SCOPES` | `openid profile email` | |
| `ENCORE_HOST_PORT` | `8930` | host port in compose |
| `ENCORE_LOG_LEVEL` | `INFO` | |

Sendspin hardware players on the LAN can also connect directly to port 8927 (publish it in compose if you want that).

## Development

- Backend: Python 3.14, aiohttp (`backend/encore`). Tests: `backend/tests` (engine, Apple Music parsers, Sendspin, smart fades).
  ```sh
  docker build -t encore:local .
  docker run --rm -v $PWD/backend:/app -w /app --entrypoint python encore:local -m pytest tests/engine tests/smartfades
  ```
- Frontend: Vite + React (`web/`). `npm run dev` proxies to `localhost:8930`; `web/mock/server.mjs` is a dependency-free mock backend.
- API contract: [docs/API.md](docs/API.md).

## Credits

Encore vendors code from [Music Assistant](https://github.com/music-assistant/server) (Apache-2.0): the Apple Music provider and the smart fades planner/renderer/analysis. See [NOTICE.md](NOTICE.md) for details and third-party model attributions (Beat This!, S-KEY, FireRedVAD).

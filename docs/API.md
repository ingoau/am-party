# am-party HTTP API

All JSON. Times are unix seconds (float) in **server time**; every state payload includes
`server_time` so clients can compute their clock offset.

## Types

```ts
type Track = {
  id: string;            // Apple Music catalog id
  title: string;
  artist: string;
  album: string | null;
  duration: number;      // seconds
  artwork_url: string | null; // ready-to-use URL, ~600x600
  explicit: boolean;
};

type QueueItem = {
  id: number;
  track: Track;
  source: "request" | "admin" | "fallback";
  request_id: number | null;   // set for approved guest requests (match against /api/requests/mine)
};

type NowPlaying = Track & {
  queue_item_id: number | null;
  source: "request" | "admin" | "fallback";
  started_at: number;    // server time the track became audible (position 0)
};

type State = {
  party_name: string;
  server_time: number;
  now_playing: NowPlaying | null;
  upcoming: QueueItem[];        // approved queue, in order (does NOT include fallback picks)
  up_next_fallback: Track | null; // what plays next if the queue is empty
  paused: boolean;
  requests_open: boolean;
  require_approval: boolean;    // false = guest requests go straight into the queue
  listeners: number;            // connected sendspin players
  public_url: string;           // configured public base URL (QR code target)
  speaker_naming_notes: string; // admin notes shown when naming a new speaker ("" = none)
};

type Request = {
  id: number;
  track: Track;
  status: "pending" | "approved" | "rejected" | "withdrawn";
  queue_position?: number;      // /api/requests/mine only: 1-based position in upcoming while queued
  eta_seconds?: number;         // /api/requests/mine only: estimated seconds until it starts
  can_withdraw?: boolean;       // /api/requests/mine only
  created_at: number;
  decided_at: number | null;
  decided_by: string | null;    // user display name (admin views only)
  guest_label: string;          // short anonymised label e.g. "Guest 7F3A" (admin views only)
  speaker?: string;             // name of the speaker whose QR the guest scanned; absent if none (admin views only)
};

type Permission = "approve_requests" | "queue_add" | "queue_reorder" | "queue_remove"
                | "playback_skip" | "playback_pause" | "claim_speakers";

type User = {
  id: number; email: string | null; name: string; picture: string | null;
  is_admin: boolean;            // admins have every permission + users/settings
  permissions: Permission[];    // effective (all of them for admins)
  granted: Permission[];        // explicitly granted (what the admin UI toggles)
  created_at?: number; last_login?: number | null; // in /api/admin/users
};
```

## Public

| Method | Path | Notes |
|---|---|---|
| GET | `/api/state` | `State` |
| GET | `/api/ws` | WebSocket. Server sends `{"type":"state","data":State}` on connect and on every change. Signed-in users with `approve_requests` also get `{"type":"requests_changed"}`; all signed-in users get `{"type":"users_changed"}` (refetch `/api/me`, admins also the user list). Users with `claim_speakers` get `{"type":"speakers_changed"}` (refetch `/api/speakers`); everyone gets `{"type":"speaker_changed","id"}` when that speaker's name, lock, claim or claim code changes (the speaker refetches `/api/speaker/self`). Client may send `{"type":"ping"}`. |
| GET | `/api/search?q=...` | `{tracks: Track[]}`. Rate limited per guest. 503 if Apple Music not configured. |
| POST | `/api/requests` | body `{track_id, speaker?}`: `track_id` must come from a recent search; `speaker` is the claim token from `/?speaker=` when the guest arrived via a speaker's QR (unknown tokens are ignored, never an error). `201 {request: Request}`. 429 with `{error}` when rate limited or too many pending; 409 if already queued/pending; 403 if requests closed. Sets anonymous guest cookie. |
| DELETE | `/api/requests/{id}` | guest withdraws their own request (pending, or approved and still queued but not yet being mixed in); 409 otherwise |
| GET | `/api/requests/mine` | `{requests: Request[]}` for this browser (no admin fields) |
| GET | `/api/me` | `{user: User \| null, oidc_enabled: boolean, oidc_name: string}` |
| GET | `/auth/login?next=/admin` | redirects to the OIDC provider |
| GET | `/auth/callback` | OIDC redirect target |
| POST | `/auth/logout` | |
| GET | `/sendspin` | WebSocket proxied to the Sendspin server (use as sendspin-js `baseUrl: location.origin`) |
| POST | `/api/speaker/self` | the listening device itself: body `{client_id}` (its sendspin-js `clientId`, which works as its secret; never put it in a URL) → `SpeakerSelf`. 404 until the speaker has connected once |
| POST | `/api/speaker/self/name` | body `{client_id, name}` (1-40 chars) → `SpeakerSelf`; 423 while the owner has locked local controls |

Errors: non-2xx with `{error: string}`.

### Security rules (all endpoints)

These are additive: a well-behaved same-origin client (the SPA) is unaffected.

- **JSON bodies** must be sent with `Content-Type: application/json`, otherwise `415 {error}`.
- **CSRF**: every POST/PUT/PATCH/DELETE must carry an `Origin` that exactly equals the
  `AM_PARTY_PUBLIC_URL` origin, or the request's own `scheme://host` when that host differs
  (LAN access by IP). Without an `Origin` header, `Sec-Fetch-Site` must be `same-origin`
  or `none`. Otherwise `403 {error}`. (Browsers send these automatically for `fetch`.)
- **`track_id`** (POST `/api/requests`, POST `/api/admin/queue`) must be an Apple Music id:
  digits (`^[0-9]{1,20}$`) or a library id (`^[ailp]\.[A-Za-z0-9]{1,64}$`), else `400`.
  Numbers are accepted and converted to strings.
- **Guest cookie** (`am_party_guest`) is now `"<id>.<signature>"`, issued by the server.
  Unsigned/forged values are replaced by a new id (so a pre-upgrade guest starts fresh).
- **Rate limits** (all `429 {error}`): search per guest (30/min), per client IP
  (`AM_PARTY_IP_SEARCHES_PER_MINUTE`, default 120/min) and in total (600/min); requests per
  guest (`max_pending_per_guest`, `request_cooldown_seconds`), per IP
  (`AM_PARTY_IP_MAX_PENDING` pending, default 20; 2 s cooldown) and in total (200 pending).
- **`/api/ws`**: messages over 4 KiB close the socket; a cross-site `Origin` gets `403`; more
  than 100 sockets per IP or 500 in total get `429` before the upgrade. `ping` is answered at
  most twice per second (extra pings are ignored, no `pong`). On `POST /auth/logout` the
  session's sockets are closed with code `4001` (reconnect to continue anonymously).
- **`/sendspin`**: cross-site `Origin` → `403`; more than 50 connections per IP or
  `AM_PARTY_MAX_LISTENERS` (default 100) in total → `503`. Clients claiming a reserved
  client id (the internal party anchor) are disconnected during the handshake.
- **`/auth/login?next=`** only accepts `/admin` and `/admin/...` paths; anything else
  falls back to `/admin`. `/auth/callback` returns an HTML `403` page when the account is
  not allowed by `AM_PARTY_ALLOWED_EMAILS`/`AM_PARTY_ALLOWED_DOMAINS`, and `400` if the
  provider returned no `id_token`.
- Sessions expire after 7 days without use (30 days at most).

## Permission-gated endpoints

Each endpoint needs the permission shown (admins have all). 401 if signed out, 403 if lacking it.

### `approve_requests`

| Method | Path | Notes |
|---|---|---|
| GET | `/api/admin/requests?status=pending` | `{requests: Request[]}` (status optional: pending/approved/rejected/all, newest first, max 200) |
| POST | `/api/admin/requests/{id}/approve` | body `{position?: "next" \| "end"}` (default end) |
| POST | `/api/admin/requests/{id}/reject` | |
| POST | `/api/admin/requests/approve-all` | body `{position?: "next" \| "end"}` → `{ok, approved: n}`; oldest first |
| POST | `/api/admin/requests/require-approval` | `{enabled: boolean}` → `{ok, require_approval, approved}`; disabling auto-approves everything pending and all future requests (`decided_by` null = auto-approved) |

### Queue and playback

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/api/admin/search?q=` | `queue_add` | same as public search, no guest rate limit |
| POST | `/api/admin/queue` | `queue_add` | `{track_id, position?: "next" \| "end"}` → adds directly (no approval) |
| DELETE | `/api/admin/queue/{id}` | `queue_remove` | 409 if the item is already being mixed in |
| POST | `/api/admin/queue/{id}/move` | `queue_reorder` | `{index: number}` (0-based in `upcoming`); 409 if being mixed in |
| POST | `/api/admin/player/skip` | `playback_skip` | skips for **every device** |
| POST | `/api/admin/player/previous` | `playback_skip` | restarts the song, or (within 3 s of its start) goes back to the previous one; the current song is queued right after it. Optional body `{at, track_id}`: `at` is the position (seconds) to decide by instead of the live one, e.g. when the button was pressed before a confirmation; with `track_id`, 409 if a different song is playing by now. `{ok, result: "restart" \| "previous"}` |
| POST | `/api/admin/player/seek` | `playback_skip` | body `{position}` (seconds); jumps within the current song for every device (also while paused) |
| POST | `/api/admin/player/pause` / `/resume` | `playback_pause` | for every device |
| GET | `/api/admin/permissions` | any signed-in | `{permissions: {id, label, description, group}[]}` catalog for the UI |

### `claim_speakers`

Speakers are browsers listening on `/tv` or `/stream`, identified by their Sendspin identity (stable
across reconnects while the browser keeps its storage). Owners are equal co-owners; admins see and
control every speaker. Volume, mute and the lock are stored and reapplied whenever a speaker
reconnects; local changes on the device win unless it is locked. Unclaimed speakers offline for 30
days are forgotten (and only the 300 most recently seen unclaimed ones are kept).

```ts
type Speaker = {
  id: string; name: string;
  connected: boolean; last_seen: number | null;
  volume: number | null;        // 0-100, null until the speaker first reported one
  muted: boolean;
  locked: boolean;              // "Lock local controls"
  owners: {id: number; name: string}[];
  mine: boolean;
};
type SpeakerSelf = {
  id: string; name: string;
  named: boolean;               // a name was chosen deliberately (device or owner)
  locked: boolean; claimed: boolean; connected: boolean;
  claim_token: string | null;   // QR code: `${public_url}/?speaker=${claim_token}`; rotates on every connect
};
```

| Method | Path | Notes |
|---|---|---|
| GET | `/api/speakers` | `{speakers: Speaker[]}`: your speakers (admins: all), connected first |
| GET | `/api/speakers/claim/{token}` | `{speaker: {id, name, connected}, already_mine}`; 404 if the code is stale |
| POST | `/api/speakers/claim` | `{token}` → `{speaker}`; joins as a co-owner |
| PATCH | `/api/speakers/{id}` | `{name?, locked?}` → `{speaker}` |
| POST | `/api/speakers/control` | `{ids, volume? \| volume_delta?, muted?, locked?}` → `{speakers}`; `volume` sets every one to that level, `volume_delta` (-100..100) moves each by the same step; max 200 ids |
| DELETE | `/api/speakers/{id}/owners/{user_id}` | any co-owner (or admin) removes any co-owner, yourself = leave → `{speaker \| null}` (null when you lost access) |
| DELETE | `/api/speakers/{id}` | admin only: forget an offline speaker (409 while connected) |
| POST | `/api/speakers/share` | `{ids, mode: "share" \| "handover"}` → `{token, expires_at, mode}`; single use, 10 minutes. Link: `${public_url}/admin/speakers?share=${token}` |
| GET | `/api/speakers/share/{token}` | `{mode, from, expires_at, speakers: {id, name, connected}[]}`; only the speakers the sharer still controls (410 if none) |
| POST | `/api/speakers/share/{token}/accept` | → `{speakers}`; adds you as a co-owner, and for `handover` removes the sharer |

### `admin`

| Method | Path | Notes |
|---|---|---|
| GET | `/api/admin/users` | `{users: User[]}` |
| PATCH | `/api/admin/users/{id}` | `{is_admin?: boolean, permissions?: Permission[]}` (permissions replaces the granted set); can't remove the last admin |
| POST | `/api/admin/users/bulk` | `{user_ids: number[], add?: Permission[], remove?: Permission[], set?: Permission[], is_admin?: boolean}` → `{ok, updated}` |
| DELETE | `/api/admin/users/{id}` | |
| GET | `/api/admin/settings` | `Settings` |
| PATCH | `/api/admin/settings` | partial `Settings` |
| GET | `/api/admin/playlists` | `{playlists: {id, name, artwork_url}[]}` (user's Apple Music library playlists) |
| POST | `/api/admin/playlists/resolve` | `{url_or_id}` → `{id, name}` (catalog playlist URL like music.apple.com/.../pl.xxxx) |
| GET | `/api/admin/status` | `{apple_music: {configured, ok, error}, smart_fades: {available, error}, listeners, engine: {...}}` |

```ts
type Settings = {
  party_name: string;
  requests_open: boolean;
  require_approval: boolean;
  fallback_playlist_id: string | null;
  fallback_playlist_name: string | null;
  fallback_shuffle: boolean;
  crossfade_mode: "smart" | "standard" | "off";
  crossfade_duration: number;        // seconds, standard mode (1-15)
  max_pending_per_guest: number;
  request_cooldown_seconds: number;
  speaker_naming_notes: string;      // up to 1000 chars, line breaks kept
};
```

### Apple Music connection (admin)

Tokens can come from env/secret files or be set from the UI (stored in the DB; UI-set values take precedence).

| Method | Path | Notes |
|---|---|---|
| GET | `/api/admin/apple-music` | `{configured, ok, error, storefront, developer_token_source: "env"\|"db"\|null, developer_token_expires_at: number\|null, user_token_source: "env"\|"db"\|null, user_token_set_at: number\|null, widevine: boolean}` |
| PUT | `/api/admin/apple-music` | `{developer_token?: string, user_token?: string}` (manual entry; empty string clears the DB value) → same as GET. `400` if a value is not a string (or longer than 8192 chars). |
| GET | `/admin/apple-music/connect` | **Full page** (not SPA): Apple MusicKit sign-in. On success it POSTs to the callback below and returns to `/admin/settings`. The settings UI should show a "Connect Apple Music" button linking here (disabled with hint if no developer token). |
| POST | `/api/admin/apple-music/user-token` | `{music_user_token, music_user_token_timestamp?}` — used by the connect page |

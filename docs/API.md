# Encore HTTP API

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
  listeners: number;            // connected sendspin players
  public_url: string;           // configured public base URL (QR code target)
};

type Request = {
  id: number;
  track: Track;
  status: "pending" | "approved" | "rejected";
  created_at: number;
  decided_at: number | null;
  decided_by: string | null;    // user display name (admin views only)
  guest_label: string;          // short anonymised label e.g. "Guest 7F3A" (admin views only)
};

type Permission = "approve_requests" | "queue_add" | "queue_reorder" | "queue_remove"
                | "playback_skip" | "playback_pause";

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
| GET | `/api/ws` | WebSocket. Server sends `{"type":"state","data":State}` on connect and on every change. Signed-in users with `approve_requests` also get `{"type":"requests_changed"}`; all signed-in users get `{"type":"users_changed"}` (refetch `/api/me`, admins also the user list). Client may send `{"type":"ping"}`. |
| GET | `/api/search?q=...` | `{tracks: Track[]}`. Rate limited per guest. 503 if Apple Music not configured. |
| POST | `/api/requests` | body `{track_id}` (must come from a recent search). `201 {request: Request}`. 429 with `{error}` when rate limited or too many pending; 409 if already queued/pending; 403 if requests closed. Sets anonymous guest cookie. |
| GET | `/api/requests/mine` | `{requests: Request[]}` for this browser (no admin fields) |
| GET | `/api/me` | `{user: User \| null, oidc_enabled: boolean, oidc_name: string}` |
| GET | `/auth/login?next=/admin` | redirects to the OIDC provider |
| GET | `/auth/callback` | OIDC redirect target |
| POST | `/auth/logout` | |
| GET | `/sendspin` | WebSocket proxied to the Sendspin server (use as sendspin-js `baseUrl: location.origin`) |

Errors: non-2xx with `{error: string}`.

## Permission-gated endpoints

Each endpoint needs the permission shown (admins have all). 401 if signed out, 403 if lacking it.

### `approve_requests`

| Method | Path | Notes |
|---|---|---|
| GET | `/api/admin/requests?status=pending` | `{requests: Request[]}` (status optional: pending/approved/rejected/all, newest first, max 200) |
| POST | `/api/admin/requests/{id}/approve` | body `{position?: "next" \| "end"}` (default end) |
| POST | `/api/admin/requests/{id}/reject` | |

### Queue and playback

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/api/admin/search?q=` | `queue_add` | same as public search, no guest rate limit |
| POST | `/api/admin/queue` | `queue_add` | `{track_id, position?: "next" \| "end"}` → adds directly (no approval) |
| DELETE | `/api/admin/queue/{id}` | `queue_remove` | 409 if the item is already being mixed in |
| POST | `/api/admin/queue/{id}/move` | `queue_reorder` | `{index: number}` (0-based in `upcoming`); 409 if being mixed in |
| POST | `/api/admin/player/skip` | `playback_skip` | skips for **every device** |
| POST | `/api/admin/player/pause` / `/resume` | `playback_pause` | for every device |
| GET | `/api/admin/permissions` | any signed-in | `{permissions: {id, label, description, group}[]}` catalog for the UI |

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
  fallback_playlist_id: string | null;
  fallback_playlist_name: string | null;
  fallback_shuffle: boolean;
  crossfade_mode: "smart" | "standard" | "off";
  crossfade_duration: number;        // seconds, standard mode (1-15)
  max_pending_per_guest: number;
  request_cooldown_seconds: number;
};
```

### Apple Music connection (admin)

Tokens can come from env/secret files or be set from the UI (stored in the DB; UI-set values take precedence).

| Method | Path | Notes |
|---|---|---|
| GET | `/api/admin/apple-music` | `{configured, ok, error, storefront, developer_token_source: "env"\|"db"\|null, developer_token_expires_at: number\|null, user_token_source: "env"\|"db"\|null, user_token_set_at: number\|null, widevine: boolean}` |
| PUT | `/api/admin/apple-music` | `{developer_token?: string, user_token?: string}` (manual entry; empty string clears the DB value) → same as GET |
| GET | `/admin/apple-music/connect` | **Full page** (not SPA): Apple MusicKit sign-in. On success it POSTs to the callback below and returns to `/admin/settings`. The settings UI should show a "Connect Apple Music" button linking here (disabled with hint if no developer token). |
| POST | `/api/admin/apple-music/user-token` | `{music_user_token, music_user_token_timestamp?}` — used by the connect page |

// Mirrors docs/API.md — keep in sync with the backend contract.

export type Source = "request" | "admin" | "fallback";

export type Track = {
  id: string;
  title: string;
  artist: string;
  album: string | null;
  duration: number;
  artwork_url: string | null;
  explicit: boolean;
};

export type QueueItem = {
  id: number;
  track: Track;
  source: Source;
};

export type NowPlaying = Track & {
  queue_item_id: number | null;
  source: Source;
  started_at: number;
};

export type State = {
  party_name: string;
  server_time: number;
  now_playing: NowPlaying | null;
  upcoming: QueueItem[];
  up_next_fallback: Track | null;
  paused: boolean;
  requests_open: boolean;
  listeners: number;
};

export type RequestStatus = "pending" | "approved" | "rejected";

export type SongRequest = {
  id: number;
  track: Track;
  status: RequestStatus;
  created_at: number;
  decided_at: number | null;
  decided_by: string | null;
  guest_label: string;
};

export type User = {
  id: number;
  email: string | null;
  name: string;
  picture: string | null;
  is_admin: boolean;
  can_approve: boolean;
  can_manage_queue: boolean;
};

export type Me = {
  user: User | null;
  oidc_enabled: boolean;
  oidc_name: string;
};

export type CrossfadeMode = "smart" | "standard" | "off";

export type Settings = {
  party_name: string;
  requests_open: boolean;
  fallback_playlist_id: string | null;
  fallback_playlist_name: string | null;
  fallback_shuffle: boolean;
  crossfade_mode: CrossfadeMode;
  crossfade_duration: number;
  max_pending_per_guest: number;
  request_cooldown_seconds: number;
};

export type Playlist = { id: string; name: string; artwork_url: string | null };

export type AdminStatus = {
  apple_music: { configured: boolean; ok: boolean; error: string | null };
  smart_fades: { available: boolean; error: string | null };
  listeners: number;
  engine: Record<string, unknown>;
};

export type TokenSource = "env" | "db" | null;

export type AppleMusicStatus = {
  configured: boolean;
  ok: boolean;
  error: string | null;
  storefront: string | null;
  developer_token_source: TokenSource;
  developer_token_expires_at: number | null;
  user_token_source: TokenSource;
  user_token_set_at: number | null;
  widevine: boolean;
};

export type WsMessage =
  | { type: "state"; data: State }
  | { type: "requests_changed" }
  | { type: "users_changed" }
  | { type: "pong" };

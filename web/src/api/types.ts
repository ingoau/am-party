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
  /** Set for approved guest requests (match against /api/requests/mine). */
  request_id?: number | null;
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
  /** false = guest requests skip approval and go straight into the queue. */
  require_approval: boolean;
  listeners: number;
  /** Configured public base URL, used for the QR code ("" when unset). */
  public_url: string;
  /** Custom TV screen copy; "" = built-in default. */
  tv_cta_text: string;
  tv_paused_text: string;
  tv_empty_text: string;
  /** Shown when someone names a new speaker ("" = none). */
  speaker_naming_notes: string;
};

export type RequestStatus = "pending" | "approved" | "rejected" | "withdrawn";

export type SongRequest = {
  id: number;
  track: Track;
  status: RequestStatus;
  /** /api/requests/mine only: 1-based position in upcoming while queued. */
  queue_position?: number;
  /** /api/requests/mine only: estimated seconds until it starts. */
  eta_seconds?: number;
  /** /api/requests/mine only. */
  can_withdraw?: boolean;
  created_at: number;
  decided_at: number | null;
  /** Admin views only (null = auto-approved). */
  decided_by?: string | null;
  /** Admin views only: anonymised label such as "Guest 7F3A". */
  guest_label?: string;
};

export type Permission =
  | "approve_requests"
  | "queue_add"
  | "queue_reorder"
  | "queue_remove"
  | "playback_skip"
  | "playback_pause"
  | "claim_speakers";

export const ALL_PERMISSIONS: readonly Permission[] = [
  "approve_requests",
  "queue_add",
  "queue_reorder",
  "queue_remove",
  "playback_skip",
  "playback_pause",
  "claim_speakers",
];

/** Anything that unlocks the Queue tab. */
export const QUEUE_PERMISSIONS: readonly Permission[] = [
  "queue_add",
  "queue_reorder",
  "queue_remove",
  "playback_skip",
  "playback_pause",
];

export type PermissionInfo = {
  id: Permission;
  label: string;
  description: string;
  group: string;
};

export type User = {
  id: number;
  email: string | null;
  name: string;
  picture: string | null;
  /** Admins have every permission plus users/settings. */
  is_admin: boolean;
  /** Effective permissions (all of them for admins). */
  permissions: Permission[];
  /** Explicitly granted permissions (what the admin UI toggles). */
  granted: Permission[];
  created_at?: number;
  last_login?: number | null;
};

export type BulkUserUpdate = {
  user_ids: number[];
  add?: Permission[];
  remove?: Permission[];
  set?: Permission[];
  is_admin?: boolean;
};

export type Me = {
  user: User | null;
  oidc_enabled: boolean;
  oidc_name: string;
};

export type CrossfadeMode = "smart" | "standard" | "off";

export type Settings = {
  party_name: string;
  tv_cta_text: string;
  tv_paused_text: string;
  tv_empty_text: string;
  requests_open: boolean;
  require_approval: boolean;
  fallback_playlist_id: string | null;
  fallback_playlist_name: string | null;
  fallback_shuffle: boolean;
  crossfade_mode: CrossfadeMode;
  crossfade_duration: number;
  max_pending_per_guest: number;
  request_cooldown_seconds: number;
  speaker_naming_notes: string;
};

export type Playlist = { id: string; name: string; artwork_url: string | null };

export type AdminStatus = {
  apple_music: { configured: boolean; ok: boolean; error: string | null };
  smart_fades: { available: boolean; error: string | null };
  fallback: { tracks: number; error: string | null };
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

export type SpeakerOwner = { id: number; name: string };

/** A listening device (/tv or /stream), as its owners and admins see it. */
export type Speaker = {
  id: string;
  name: string;
  connected: boolean;
  /** Unix seconds: last connect / disconnect / activity. */
  last_seen: number | null;
  /** 0-100; null = never reported. */
  volume: number | null;
  muted: boolean;
  /** "Lock local controls": the device itself can't change volume, mute or name. */
  locked: boolean;
  owners: SpeakerOwner[];
  /** The current user is a co-owner. */
  mine: boolean;
};

/** What a device learns about itself (POST /api/speaker/self). */
export type SpeakerSelf = {
  id: string;
  name: string;
  /** false until a name was explicitly chosen (device prompt or owner rename). */
  named: boolean;
  locked: boolean;
  claimed: boolean;
  connected: boolean;
  /** For the claim QR; null while not connected. */
  claim_token: string | null;
};

export type SpeakerBrief = { id: string; name: string; connected: boolean };

export type SpeakerControl = {
  ids: string[];
  volume?: number;
  volume_delta?: number;
  muted?: boolean;
  locked?: boolean;
};

export type ShareMode = "share" | "handover";

export type SpeakerShare = { token: string; expires_at: number; mode: ShareMode };

export type SpeakerSharePreview = { mode: ShareMode; from: string; expires_at: number; speakers: SpeakerBrief[] };

export type WsMessage =
  | { type: "state"; data: State }
  | { type: "requests_changed" }
  | { type: "users_changed" }
  | { type: "speakers_changed" }
  | { type: "speaker_changed"; id: string }
  | { type: "pong"; server_time: number };

import type {
  AdminStatus,
  AppleMusicStatus,
  BulkUserUpdate,
  Me,
  Permission,
  PermissionInfo,
  Playlist,
  Settings,
  ShareMode,
  SongRequest,
  Speaker,
  SpeakerBrief,
  SpeakerControl,
  SpeakerSelf,
  SpeakerShare,
  SpeakerSharePreview,
  State,
  Track,
  User,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function send(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<{ res: Response; text: string; data: unknown }> {
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      credentials: "same-origin",
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal,
    });
  } catch (e) {
    if ((e as Error).name === "AbortError") throw e;
    throw new ApiError(0, "Can't reach the party server. Check your connection.");
  }
  const text = await res.text();
  let data: unknown = undefined;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = undefined;
    }
  }
  if (!res.ok) {
    const msg =
      data && typeof data === "object" && "error" in data && typeof (data as { error: unknown }).error === "string"
        ? (data as { error: string }).error
        : `${res.status} ${res.statusText || "Error"}`;
    throw new ApiError(res.status, msg);
  }
  if (text && data === undefined) {
    // A 2xx that isn't JSON (e.g. a proxy's HTML page) is not a success we can use.
    throw new ApiError(res.status, "Unexpected response from the party server.");
  }
  return { res, text, data };
}

/** JSON request whose response body the caller uses: an empty or non-JSON 2xx body is an error. */
async function request<T>(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const { text, data } = await send(method, path, body, signal);
  if (!text) throw new ApiError(502, "Empty response from the party server.");
  return data as T;
}

/** Request where only success matters; an empty body (e.g. 204) is fine, non-JSON is still an error. */
async function command(method: string, path: string, body?: unknown): Promise<void> {
  await send(method, path, body);
}

const enc = encodeURIComponent;

export const api = {
  // Public
  state: () => request<State>("GET", "/api/state"),
  search: (q: string, signal?: AbortSignal) =>
    request<{ tracks: Track[] }>("GET", `/api/search?q=${enc(q)}`, undefined, signal),
  createRequest: (track_id: string, speaker?: string | null) =>
    request<{ request: SongRequest }>("POST", "/api/requests", speaker ? { track_id, speaker } : { track_id }),
  withdrawRequest: (id: number) => command("DELETE", `/api/requests/${id}`),
  myRequests: () => request<{ requests: SongRequest[] }>("GET", "/api/requests/mine"),
  me: () => request<Me>("GET", "/api/me"),
  logout: () => command("POST", "/auth/logout"),

  // This device as a speaker (client_id is the device's secret: body only, never the URL)
  speakerSelf: (client_id: string) => request<SpeakerSelf>("POST", "/api/speaker/self", { client_id }),
  renameSpeakerSelf: (client_id: string, name: string) =>
    request<SpeakerSelf>("POST", "/api/speaker/self/name", { client_id, name }),

  // Speaker owners (claim_speakers) and admins
  speakers: () => request<{ speakers: Speaker[] }>("GET", "/api/speakers"),
  claimPreview: (token: string) =>
    request<{ speaker: SpeakerBrief; already_mine: boolean }>("GET", `/api/speakers/claim/${enc(token)}`),
  claimSpeaker: (token: string) => request<{ speaker: Speaker }>("POST", "/api/speakers/claim", { token }),
  patchSpeaker: (id: string, patch: { name?: string; locked?: boolean }) =>
    request<{ speaker: Speaker }>("PATCH", `/api/speakers/${enc(id)}`, patch),
  controlSpeakers: (body: SpeakerControl) => request<{ speakers: Speaker[] }>("POST", "/api/speakers/control", body),
  removeSpeakerOwner: (id: string, userId: number) =>
    request<{ speaker: Speaker | null }>("DELETE", `/api/speakers/${enc(id)}/owners/${userId}`),
  forgetSpeaker: (id: string) => command("DELETE", `/api/speakers/${enc(id)}`),
  shareSpeakers: (ids: string[], mode: ShareMode) => request<SpeakerShare>("POST", "/api/speakers/share", { ids, mode }),
  sharePreview: (token: string) => request<SpeakerSharePreview>("GET", `/api/speakers/share/${enc(token)}`),
  acceptShare: (token: string) => request<{ speakers: Speaker[] }>("POST", `/api/speakers/share/${enc(token)}/accept`),

  // Approvers
  adminRequests: (status: "pending" | "approved" | "rejected" | "all" = "pending", signal?: AbortSignal) =>
    request<{ requests: SongRequest[] }>("GET", `/api/admin/requests?status=${status}`, undefined, signal),
  approve: (id: number, position: "next" | "end" = "end") =>
    request<{ request: SongRequest }>("POST", `/api/admin/requests/${id}/approve`, { position }),
  reject: (id: number) => request<{ request: SongRequest }>("POST", `/api/admin/requests/${id}/reject`, {}),
  approveAll: (position: "next" | "end" = "end") =>
    request<{ ok: boolean; approved: number }>("POST", "/api/admin/requests/approve-all", { position }),
  setRequireApproval: (enabled: boolean) =>
    request<{ ok: boolean; require_approval: boolean; approved: number }>("POST", "/api/admin/requests/require-approval", { enabled }),

  // Queue managers
  adminSearch: (q: string, signal?: AbortSignal) =>
    request<{ tracks: Track[] }>("GET", `/api/admin/search?q=${enc(q)}`, undefined, signal),
  queueAdd: (track_id: string, position: "next" | "end" = "end") =>
    request<{ ok: boolean }>("POST", "/api/admin/queue", { track_id, position }),
  queueRemove: (id: number) => command("DELETE", `/api/admin/queue/${id}`),
  queueMove: (id: number, index: number) => command("POST", `/api/admin/queue/${id}/move`, { index }),
  skip: () => command("POST", "/api/admin/player/skip", {}),
  /** `at`: the song position when the button was pressed, which decides restart vs go back. */
  previous: (at?: number, trackId?: string) =>
    request<{ ok: boolean; result: "restart" | "previous" }>("POST", "/api/admin/player/previous", {
      at,
      track_id: trackId,
    }),
  seek: (position: number) => command("POST", "/api/admin/player/seek", { position }),
  pause: () => command("POST", "/api/admin/player/pause", {}),
  resume: () => command("POST", "/api/admin/player/resume", {}),

  // Admin
  users: () => request<{ users: User[] }>("GET", "/api/admin/users"),
  permissions: () => request<{ permissions: PermissionInfo[] }>("GET", "/api/admin/permissions"),
  patchUser: (id: number, patch: { is_admin?: boolean; permissions?: Permission[] }) =>
    command("PATCH", `/api/admin/users/${id}`, patch),
  bulkUsers: (body: BulkUserUpdate) => request<{ ok: boolean; updated: number }>("POST", "/api/admin/users/bulk", body),
  deleteUser: (id: number) => command("DELETE", `/api/admin/users/${id}`),
  settings: () => request<Settings>("GET", "/api/admin/settings"),
  patchSettings: (patch: Partial<Settings>) => request<Settings>("PATCH", "/api/admin/settings", patch),
  playlists: () => request<{ playlists: Playlist[] }>("GET", "/api/admin/playlists"),
  resolvePlaylist: (url_or_id: string) =>
    request<{ id: string; name: string }>("POST", "/api/admin/playlists/resolve", { url_or_id }),
  status: () => request<AdminStatus>("GET", "/api/admin/status"),
  appleMusic: () => request<AppleMusicStatus>("GET", "/api/admin/apple-music"),
  putAppleMusic: (body: { developer_token?: string; user_token?: string }) =>
    request<AppleMusicStatus>("PUT", "/api/admin/apple-music", body),
};

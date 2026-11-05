import type {
  AdminStatus,
  AppleMusicStatus,
  BulkUserUpdate,
  Me,
  Permission,
  PermissionInfo,
  Playlist,
  QueueItem,
  Settings,
  SongRequest,
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

async function request<T>(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
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
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!res.ok) {
    const msg =
      data && typeof data === "object" && "error" in data && typeof (data as { error: unknown }).error === "string"
        ? (data as { error: string }).error
        : `${res.status} ${res.statusText || "Error"}`;
    throw new ApiError(res.status, msg);
  }
  return data as T;
}

const enc = encodeURIComponent;

export const api = {
  // Public
  state: () => request<State>("GET", "/api/state"),
  search: (q: string, signal?: AbortSignal) =>
    request<{ tracks: Track[] }>("GET", `/api/search?q=${enc(q)}`, undefined, signal),
  createRequest: (track_id: string) => request<{ request: SongRequest }>("POST", "/api/requests", { track_id }),
  myRequests: () => request<{ requests: SongRequest[] }>("GET", "/api/requests/mine"),
  me: () => request<Me>("GET", "/api/me"),
  logout: () => request<unknown>("POST", "/auth/logout"),

  // Approvers
  adminRequests: (status: "pending" | "approved" | "rejected" | "all" = "pending") =>
    request<{ requests: SongRequest[] }>("GET", `/api/admin/requests?status=${status}`),
  approve: (id: number, position: "next" | "end" = "end") =>
    request<unknown>("POST", `/api/admin/requests/${id}/approve`, { position }),
  reject: (id: number) => request<unknown>("POST", `/api/admin/requests/${id}/reject`, {}),

  // Queue managers
  adminSearch: (q: string, signal?: AbortSignal) =>
    request<{ tracks: Track[] }>("GET", `/api/admin/search?q=${enc(q)}`, undefined, signal),
  queueAdd: (track_id: string, position: "next" | "end" = "end") =>
    request<{ item?: QueueItem }>("POST", "/api/admin/queue", { track_id, position }),
  queueRemove: (id: number) => request<unknown>("DELETE", `/api/admin/queue/${id}`),
  queueMove: (id: number, index: number) => request<unknown>("POST", `/api/admin/queue/${id}/move`, { index }),
  skip: () => request<unknown>("POST", "/api/admin/player/skip", {}),
  pause: () => request<unknown>("POST", "/api/admin/player/pause", {}),
  resume: () => request<unknown>("POST", "/api/admin/player/resume", {}),

  // Admin
  users: () => request<{ users: User[] }>("GET", "/api/admin/users"),
  permissions: () => request<{ permissions: PermissionInfo[] }>("GET", "/api/admin/permissions"),
  patchUser: (id: number, patch: { is_admin?: boolean; permissions?: Permission[] }) =>
    request<unknown>("PATCH", `/api/admin/users/${id}`, patch),
  bulkUsers: (body: BulkUserUpdate) => request<{ ok: boolean; updated: number }>("POST", "/api/admin/users/bulk", body),
  deleteUser: (id: number) => request<unknown>("DELETE", `/api/admin/users/${id}`),
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

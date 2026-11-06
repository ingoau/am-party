import { useEffect, useState } from "react";
import { api } from "../../api/client";
import { QUEUE_PERMISSIONS, type Permission, type PermissionInfo, type User } from "../../api/types";

/** Effective permission check (the server already expands admins to every permission). */
export const can = (u: User | null | undefined, p: Permission) => !!u && (u.is_admin || u.permissions.includes(p));
export const canAny = (u: User | null | undefined, ps: readonly Permission[]) => ps.some((p) => can(u, p));
export const canQueueTab = (u: User | null | undefined) => canAny(u, QUEUE_PERMISSIONS);

/** Used until /api/admin/permissions answers (or if it fails). Mirrors backend/am_party/auth.py. */
export const FALLBACK_CATALOG: PermissionInfo[] = [
  { id: "approve_requests", label: "Approve requests", description: "Approve or reject songs guests request", group: "Requests" },
  { id: "queue_add", label: "Add to queue", description: "Add songs straight to the queue without approval", group: "Queue" },
  { id: "queue_reorder", label: "Reorder queue", description: "Move songs around in the queue", group: "Queue" },
  { id: "queue_remove", label: "Remove from queue", description: "Take songs out of the queue", group: "Queue" },
  { id: "playback_skip", label: "Skip songs", description: "Skip, go back or seek within the current song for every device", group: "Playback" },
  { id: "playback_pause", label: "Pause & resume", description: "Pause and resume playback for every device", group: "Playback" },
];

export type Preset = { id: string; label: string; description: string; permissions: Permission[] };

export const PRESETS: Preset[] = [
  {
    id: "dj",
    label: "DJ",
    description: "Add, reorder and remove songs; skip and pause",
    permissions: ["queue_add", "queue_reorder", "queue_remove", "playback_skip", "playback_pause"],
  },
  { id: "approver", label: "Approver", description: "Approve or reject guest requests", permissions: ["approve_requests"] },
];

export type PermissionGroup = { name: string; items: PermissionInfo[] };

/** Group the catalog by `group`, keeping the server's order. */
export function groupCatalog(catalog: PermissionInfo[]): PermissionGroup[] {
  const groups: PermissionGroup[] = [];
  for (const p of catalog) {
    let g = groups.find((x) => x.name === p.group);
    if (!g) groups.push((g = { name: p.group, items: [] }));
    g.items.push(p);
  }
  return groups;
}

let cached: PermissionInfo[] | null = null;

/** The permission catalog (labels, descriptions, groups), fetched once per page load. */
export function usePermissionCatalog(): PermissionInfo[] {
  const [catalog, setCatalog] = useState<PermissionInfo[]>(cached ?? FALLBACK_CATALOG);
  useEffect(() => {
    if (cached) return;
    let alive = true;
    api
      .permissions()
      .then((r) => {
        if (!r.permissions?.length) return;
        cached = r.permissions;
        if (alive) setCatalog(r.permissions);
      })
      .catch(() => {
        /* keep the fallback */
      });
    return () => {
      alive = false;
    };
  }, []);
  return catalog;
}

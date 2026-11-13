#!/usr/bin/env node
// am-party mock backend — a dependency-free dev tool that implements docs/API.md with fake,
// in-memory data and serves web/dist with SPA fallback. No audio (/sendspin is not mocked).
//
//   npm run build && PORT=8930 node mock/server.mjs
//   open http://localhost:8930/            (guest)   /tv (TV)   /admin (host)
//
// /auth/login signs you in as a mock admin (cookie), /auth/logout signs out.
// MOCK_USER=none|noperm|admin|dj|approver|skipper sets what /auth/login gives you (default admin).
// /auth/login?as=<user id> signs in as any mock user (handy for testing permission gating).
// MOCK_NOW_PLAYING=<catalog index> picks the starting track (16 = very long title/artist, 17 = "Dai Dai").
// /mock/guest?id=x makes this browser guest "x" (who has seeded requests) and opens /.
import http from "node:http";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const PORT = Number(process.env.PORT || 8930);
const DIST = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../dist");
const LOGIN_AS = process.env.MOCK_USER || "admin";
const now = () => Date.now() / 1000;

// ---------- Fake catalog ----------
const PALETTE = [
  ["#ff5f6d", "#ffc371"], ["#2193b0", "#6dd5ed"], ["#8e2de2", "#4a00e0"], ["#f7971e", "#ffd200"],
  ["#11998e", "#38ef7d"], ["#fc466b", "#3f5efb"], ["#c94b4b", "#4b134f"], ["#00b4db", "#0083b0"],
  ["#ee0979", "#ff6a00"], ["#56ab2f", "#a8e063"], ["#614385", "#516395"], ["#e65c00", "#f9d423"],
];
const SONGS = [
  ["Midnight Static", "Neon Harbor", "City Lights EP", 212],
  ["Golden Hour", "The Paper Kites", "Sunsets", 198],
  ["Dance Floor Theory", "Kilo Mira", "Afterparty", 241],
  ["Electric Feel", "MGMT", "Oracular Spectacular", 229],
  ["Ocean Drive", "Duke Dumont", "Blasé Boys Club", 206],
  ["Lose Yourself to Dance", "Daft Punk", "Random Access Memories", 353],
  ["Bad Habit", "Steve Lacy", "Gemini Rights", 232],
  ["Levitating", "Dua Lipa", "Future Nostalgia", 203],
  ["Kids", "MGMT", "Oracular Spectacular", 302],
  ["Get Lucky", "Daft Punk", "Random Access Memories", 369],
  ["Blinding Lights", "The Weeknd", "After Hours", 200],
  ["Feel It Still", "Portugal. The Man", "Woodstock", 163],
  ["Dreams", "Fleetwood Mac", "Rumours", 257],
  ["Mr. Brightside", "The Killers", "Hot Fuss", 222],
  ["A Really Long Song Title That Goes On and On (Extended Club Remix)", "Somebody & The Featured Artists Collective", "An Album With A Very Long Name Indeed", 431],
  ["September", "Earth, Wind & Fire", "The Best of Earth, Wind & Fire", 215],
  ["Titanium (feat. Sia) [David Guetta & MORTEN Future Rave Remix]", "Daft Punk, Pharrell Williams & Nile Rodgers", "Titanium (feat. Sia) [David Guetta & MORTEN Future Rave Remix] - Single", 246],
  ["Dai Dai", "Shakira & Burna Boy", "Dai Dai - Single", 198],
];
const TRACKS = SONGS.map(([title, artist, album, duration], i) => ({
  id: String(1400000000 + i),
  title,
  artist,
  album,
  duration,
  artwork_url: `/mock-art/${i}/600x600bb.svg`,
  explicit: i % 5 === 3,
}));
const byId = new Map(TRACKS.map((t) => [t.id, t]));

function artSvg(i) {
  const [a, b] = PALETTE[i % PALETTE.length];
  const letter = (SONGS[i]?.[0] ?? "?")[0];
  return `<svg xmlns="http://www.w3.org/2000/svg" width="600" height="600" viewBox="0 0 600 600">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${a}"/><stop offset="1" stop-color="${b}"/></linearGradient></defs>
<rect width="600" height="600" fill="url(#g)"/><circle cx="430" cy="170" r="120" fill="#fff" opacity=".15"/>
<text x="60" y="520" font-family="Helvetica, Arial" font-weight="800" font-size="260" fill="#fff" opacity=".85">${letter}</text></svg>`;
}

// ---------- State ----------
let nextId = 1;
const settings = {
  party_name: "Sam's Birthday Bash",
  requests_open: true,
  require_approval: true,
  fallback_playlist_id: "p.mock1",
  fallback_playlist_name: "Party Classics",
  fallback_shuffle: true,
  crossfade_mode: "smart",
  crossfade_duration: 6,
  max_pending_per_guest: 3,
  request_cooldown_seconds: 30,
  speaker_naming_notes: "",
  auto_approval: true,
  auto_approval_threshold: 90,
  auto_approval_prompt: "",
  auto_approval_model: "jev-latest",
  auto_approval_provider: "typesafe",
};
const autoApproval = {
  api_key_sources: { typesafe: "db", openrouter: null },
  default_prompt:
    "Should this song be played at the party? Approve songs that suit a party crowd. Reject songs whose lyrics are sexually explicit, hateful, contain slurs, or glorify violence or self-harm. A song that only swears a little is fine.",
  default_model: "jev-latest",
};
const autoActive = () =>
  settings.require_approval && settings.auto_approval && !!autoApproval.api_key_sources[settings.auto_approval_provider];
let nowPlaying = { track: TRACKS[Number(process.env.MOCK_NOW_PLAYING ?? 3) % TRACKS.length], queue_item_id: null, source: "fallback", started_at: now() - 61 };
let paused = false;
let pausedAt = 0;
let upcoming = [
  { id: nextId++, track: TRACKS[5], source: "request", request_id: null },
  { id: nextId++, track: TRACKS[10], source: "admin", request_id: null },
  { id: nextId++, track: TRACKS[14], source: "request", request_id: null },
];
let fallbackIdx = 7;
const requests = [
  { id: nextId++, track: TRACKS[6], status: "pending", created_at: now() - 120, decided_at: null, decided_by: null, guest_label: "Guest 7F3A", guest: "x" },
  { id: nextId++, track: TRACKS[12], status: "pending", created_at: now() - 40, decided_at: null, decided_by: null, guest_label: "Guest 21C9", guest: "y", auto: { status: "flagged", decision: "approve", probability: 0.72, note: "No lyrics found" } },
  { id: nextId++, track: TRACKS[0], status: "pending", created_at: now() - 95, decided_at: null, decided_by: null, guest_label: "Guest 5D10", guest: "w" },
  { id: nextId++, track: TRACKS[7], status: "pending", created_at: now() - 75, decided_at: null, decided_by: null, guest_label: "Guest 9AC4", guest: "v" },
  { id: nextId++, track: TRACKS[16], status: "pending", created_at: now() - 60, decided_at: null, decided_by: null, guest_label: "Guest 3E77", guest: "u", auto: { status: "checking", decision: null, probability: null, note: null } },
  { id: nextId++, track: TRACKS[11], status: "pending", created_at: now() - 20, decided_at: null, decided_by: null, guest_label: "Guest 7F3A", guest: "x" },
  { id: nextId++, track: TRACKS[15], status: "approved", created_at: now() - 1500, decided_at: now() - 1500, decided_by: null, guest_label: "Guest 44B0", guest: "t", auto: { status: "approved", decision: "approve", probability: 0.98, note: null } },
  { id: nextId++, track: TRACKS[5], status: "approved", created_at: now() - 900, decided_at: now() - 860, decided_by: "Sam Host", guest_label: "Guest 7F3A", guest: "x" },
  { id: nextId++, track: TRACKS[1], status: "rejected", created_at: now() - 1200, decided_at: now() - 1100, decided_by: "Sam Host", guest_label: "Guest 0B12", guest: "z" },
  { id: nextId++, track: TRACKS[14], status: "approved", created_at: now() - 700, decided_at: now() - 650, decided_by: "Sam Host", guest_label: "Guest 7F3A", guest: "x" },
  { id: nextId++, track: TRACKS[2], status: "rejected", created_at: now() - 2000, decided_at: now() - 1900, decided_by: "Sam Host", guest_label: "Guest 7F3A", guest: "x" },
];
// Link the seeded approved requests to their queue items.
for (const r of requests) {
  if (r.status !== "approved") continue;
  const item = upcoming.find((u) => u.track.id === r.track.id && u.source === "request" && u.request_id == null);
  if (item) { item.request_id = r.id; r.queue_item_id = item.id; }
}
// ---------- Permissions (mirrors backend/am_party/auth.py) ----------
const PERMISSIONS = [
  { id: "approve_requests", label: "Approve requests", description: "Approve or reject songs guests request", group: "Requests" },
  { id: "queue_add", label: "Add to queue", description: "Add songs straight to the queue without approval", group: "Queue" },
  { id: "queue_reorder", label: "Reorder queue", description: "Move songs around in the queue", group: "Queue" },
  { id: "queue_remove", label: "Remove from queue", description: "Take songs out of the queue", group: "Queue" },
  { id: "playback_skip", label: "Skip songs", description: "Skip, go back or seek within the current song for every device", group: "Playback" },
  { id: "playback_pause", label: "Pause & resume", description: "Pause and resume playback for every device", group: "Playback" },
  { id: "claim_speakers", label: "Claim speakers", description: "Claim speakers by scanning their QR code and control the ones you own", group: "Speakers" },
];
const PERM_IDS = PERMISSIONS.map((p) => p.id);
const cleanPerms = (v) => (Array.isArray(v) ? [...new Set(v.filter((p) => PERM_IDS.includes(p)))].sort() : []);
const DJ = ["queue_add", "queue_reorder", "queue_remove", "playback_skip", "playback_pause"];

const mkUser = (id, name, email, is_admin, granted, daysAgo, seenMinsAgo) => ({
  id, name, email, picture: null, is_admin, granted: cleanPerms(granted),
  created_at: now() - daysAgo * 86400, last_login: seenMinsAgo == null ? null : now() - seenMinsAgo * 60,
});
const users = [
  mkUser(1, "Sam Host", "sam@example.com", true, [], 30, 2),
  mkUser(2, "Alex Rivera", "alex@example.com", false, ["approve_requests"], 12, 15),
  mkUser(3, "Jo Newcomer", "jo@example.com", false, [], 0, 1),
  mkUser(4, "Priya Shah", "priya@example.com", false, DJ, 9, 40),
  mkUser(5, "Marcus Chen", "marcus.chen@example.com", false, ["approve_requests", ...DJ], 20, 300),
  mkUser(6, "Lena Fischer", "lena@example.com", false, ["queue_add", "playback_skip"], 3, 90),
  mkUser(7, "Tomás Ortega", "tomas.ortega@example.com", false, [], 0, null),
  mkUser(8, "Dana Okafor", "dana@example.com", true, [], 25, 1440),
];
const effective = (u) => (u.is_admin ? [...PERM_IDS] : u.granted);
const publicUser = (u) => u && { ...u, permissions: effective(u) };
const hasPerm = (u, perm) => !!u && (u.is_admin || (perm !== "admin" && u.granted.includes(perm)));
const amState = {
  configured: true, ok: true, error: null, storefront: "us",
  developer_token_source: "db", developer_token_expires_at: now() + 86400 * 120,
  user_token_source: "db", user_token_set_at: now() - 86400 * 2, widevine: true,
};

function state() {
  const np = nowPlaying
    ? { ...nowPlaying.track, queue_item_id: nowPlaying.queue_item_id, source: nowPlaying.source, started_at: nowPlaying.started_at }
    : null;
  return {
    party_name: settings.party_name,
    server_time: now(),
    now_playing: np,
    upcoming,
    up_next_fallback: TRACKS[fallbackIdx % TRACKS.length],
    paused,
    requests_open: settings.requests_open,
    require_approval: settings.require_approval,
    auto_approval: autoActive(),
    speaker_naming_notes: settings.speaker_naming_notes,
    listeners: 4,
  };
}

const played = []; // back stack for "previous"
function advance() {
  if (nowPlaying) played.push(nowPlaying);
  const next = upcoming.shift();
  if (next) nowPlaying = { track: next.track, queue_item_id: next.id, source: next.source, started_at: now() };
  else {
    nowPlaying = { track: TRACKS[fallbackIdx % TRACKS.length], queue_item_id: null, source: "fallback", started_at: now() };
    fallbackIdx += 3;
  }
  broadcastState();
}
setInterval(() => {
  if (!paused && nowPlaying && now() - nowPlaying.started_at >= nowPlaying.track.duration) advance();
}, 500);

// ---------- Minimal WebSocket server (text frames only) ----------
const sockets = new Set();
function wsSend(sock, obj) {
  const payload = Buffer.from(JSON.stringify(obj));
  let header;
  if (payload.length < 126) header = Buffer.from([0x81, payload.length]);
  else if (payload.length < 65536) {
    header = Buffer.alloc(4);
    header[0] = 0x81; header[1] = 126; header.writeUInt16BE(payload.length, 2);
  } else {
    header = Buffer.alloc(10);
    header[0] = 0x81; header[1] = 127; header.writeBigUInt64BE(BigInt(payload.length), 2);
  }
  try { sock.write(Buffer.concat([header, payload])); } catch { /* ignore */ }
}
function broadcast(obj, filter = () => true) {
  for (const s of sockets) if (filter(s)) wsSend(s, obj);
}
const broadcastState = () => broadcast({ type: "state", data: state() });
const sockUser = (s) => users.find((u) => u.id === s.uid) || null;
const requestsChanged = () => broadcast({ type: "requests_changed" }, (s) => hasPerm(sockUser(s), "approve_requests"));
/** Approve a pending request into the queue. `by` null = auto-approved. */
function approveRequest(r, by, position = "end") {
  r.status = "approved";
  r.decided_at = now();
  r.decided_by = by;
  const item = { id: nextId++, track: r.track, source: "request", request_id: r.id };
  r.queue_item_id = item.id;
  position === "next" ? upcoming.unshift(item) : upcoming.push(item);
}
/** The head of the queue can't change once it's being mixed in (last 15s of the current track). */
const mixingIn = () => !!nowPlaying && !paused && nowPlaying.started_at + nowPlaying.track.duration - now() < 15;
/** /api/requests/mine extras: queue position, ETA and whether it can still be withdrawn. */
function mineView(r) {
  const out = strip(r, false);
  if (r.status === "pending") return { ...out, can_withdraw: true };
  if (r.status !== "approved") return { ...out, can_withdraw: false };
  const i = upcoming.findIndex((u) => u.id === r.queue_item_id);
  if (i < 0) return { ...out, can_withdraw: false };
  const remaining = nowPlaying ? Math.max(0, nowPlaying.started_at + nowPlaying.track.duration - (paused ? pausedAt : now())) : 0;
  const eta = remaining + upcoming.slice(0, i).reduce((s, u) => s + u.track.duration, 0);
  return { ...out, queue_position: i + 1, eta_seconds: Math.round(eta), can_withdraw: !(i === 0 && mixingIn()) };
}
/** Approve every pending request, oldest first (so "next" keeps their relative order). */
function approveAllPending(by, position = "end") {
  const pending = requests.filter((r) => r.status === "pending").sort((a, b) => a.created_at - b.created_at);
  (position === "next" ? [...pending].reverse() : pending).forEach((r) => approveRequest(r, by, position));
  return pending.length;
}
function setRequireApproval(enabled) {
  settings.require_approval = !!enabled;
  return settings.require_approval ? 0 : approveAllPending(null);
}
const usersChanged = () => broadcast({ type: "users_changed" }, (s) => s.uid != null); // every signed-in user

/** Mirrors the backend's _apply_user_changes. Returns [status, error] on failure. */
function applyUserChanges(ids, isAdmin, set, add, remove) {
  const targets = ids.map((id) => users.find((u) => u.id === id));
  if (targets.some((u) => !u)) return [404, "User not found"];
  if (isAdmin === false) {
    const admins = users.filter((u) => u.is_admin).length;
    const demoting = targets.filter((u) => u.is_admin).length;
    if (demoting && admins - demoting < 1) return [409, "Can't remove the last admin"];
  }
  for (const u of targets) {
    const perms = new Set(set != null ? cleanPerms(set) : u.granted);
    for (const p of cleanPerms(add)) perms.add(p);
    for (const p of cleanPerms(remove)) perms.delete(p);
    u.granted = [...perms].sort();
    if (isAdmin != null) u.is_admin = !!isAdmin;
  }
  usersChanged();
  return null;
}

// ---------- HTTP helpers ----------
function cookies(req) {
  return Object.fromEntries((req.headers.cookie || "").split(/;\s*/).filter(Boolean).map((c) => c.split("=").map(decodeURIComponent)));
}
function currentUser(req) {
  const id = Number(cookies(req).mock_uid);
  return users.find((u) => u.id === id) || null;
}
function json(res, status, body, headers = {}) {
  res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store", ...headers });
  res.end(body === undefined ? "" : JSON.stringify(body));
}
const err = (res, status, error) => json(res, status, { error });
async function readBody(req) {
  let data = "";
  for await (const chunk of req) data += chunk;
  try { return data ? JSON.parse(data) : {}; } catch { return {}; }
}
const strip = (r, admin) => {
  const { guest, queue_item_id, ...rest } = r;
  if (admin) return rest;
  const { decided_by, guest_label, ...pub } = rest; // admin views only, like the backend
  return pub;
};
function search(q) {
  q = q.toLowerCase();
  return TRACKS.filter((t) => `${t.title} ${t.artist} ${t.album}`.toLowerCase().includes(q)).slice(0, 25);
}
const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json", ".wasm": "application/wasm" };

// ---------- Routes ----------
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://x");
  const p = url.pathname;
  const user = currentUser(req);
  const guest = cookies(req).guest || crypto.randomBytes(4).toString("hex");
  const guestCookie = { "Set-Cookie": `guest=${guest}; Path=/; HttpOnly; SameSite=Lax` };
  const need = (perm) => {
    if (!user) return err(res, 401, "Not signed in"), false;
    if (!hasPerm(user, perm)) return err(res, 403, "Forbidden"), false;
    return true;
  };
  let m;

  if ((m = p.match(/^\/mock-art\/(\d+)\//))) {
    res.writeHead(200, { "Content-Type": "image/svg+xml", "Access-Control-Allow-Origin": "*", "Cache-Control": "max-age=3600" });
    return res.end(artSvg(Number(m[1])));
  }

  // Auth
  if (p === "/auth/login") {
    const named = { none: null, admin: 1, noperm: 3, approver: 2, dj: 4, skipper: 6 };
    const as = Number(url.searchParams.get("as"));
    const target = users.some((u) => u.id === as) ? as : LOGIN_AS in named ? named[LOGIN_AS] : 1;
    res.writeHead(302, { Location: url.searchParams.get("next") || "/admin", ...(target ? { "Set-Cookie": `mock_uid=${target}; Path=/; SameSite=Lax` } : {}) });
    return res.end();
  }
  if (p === "/auth/logout" && req.method === "POST") return json(res, 200, {}, { "Set-Cookie": "mock_uid=; Path=/; Max-Age=0" });
  if (p === "/admin/apple-music/connect") {
    res.writeHead(200, { "Content-Type": "text/html" });
    return res.end(`<body style="background:#111;color:#eee;font-family:sans-serif;padding:40px"><h1>Mock MusicKit connect page</h1><p>The real backend renders Apple's sign-in here.</p><a style="color:#f6a" href="/admin/settings">Back to settings</a></body>`);
  }

  // Public API
  if (p === "/api/state") return json(res, 200, state());
  if (p === "/api/me") return json(res, 200, { user: publicUser(user), oidc_enabled: true, oidc_name: "Authentik" });
  if (p === "/api/search" || p === "/api/admin/search") {
    if (p.startsWith("/api/admin") && !need("queue_add")) return;
    const q = url.searchParams.get("q") || "";
    await new Promise((r) => setTimeout(r, 250));
    return json(res, 200, { tracks: search(q) }, guestCookie);
  }
  if (p === "/api/requests" && req.method === "POST") {
    const { track_id } = await readBody(req);
    if (!settings.requests_open) return err(res, 403, "Requests are closed");
    const t = byId.get(track_id);
    if (!t) return err(res, 400, "Unknown track");
    if (upcoming.some((u) => u.track.id === track_id) || requests.some((r) => r.track.id === track_id && r.status === "pending"))
      return err(res, 409, "Already queued or pending");
    if (requests.filter((r) => r.guest === guest && r.status === "pending").length >= settings.max_pending_per_guest)
      return err(res, 429, `You already have ${settings.max_pending_per_guest} songs waiting for approval.`);
    const r = { id: nextId++, track: t, status: "pending", created_at: now(), decided_at: null, decided_by: null, guest_label: `Guest ${guest.slice(0, 4).toUpperCase()}`, guest };
    requests.unshift(r);
    if (!settings.require_approval) {
      approveRequest(r, null);
      broadcastState();
    }
    requestsChanged();
    return json(res, 201, { request: strip(r, false) }, guestCookie);
  }
  if (p === "/api/requests/mine") return json(res, 200, { requests: requests.filter((r) => r.guest === guest).map(mineView) }, guestCookie);
  if ((m = p.match(/^\/api\/requests\/(\d+)$/)) && req.method === "DELETE") {
    const r = requests.find((x) => x.id === Number(m[1]) && x.guest === guest);
    if (!r) return err(res, 404, "Not found");
    if (r.status === "approved") {
      const i = upcoming.findIndex((u) => u.id === r.queue_item_id);
      if (i < 0 || (i === 0 && mixingIn())) return err(res, 409, "Too late to withdraw that one");
      upcoming.splice(i, 1);
      broadcastState();
    } else if (r.status !== "pending") return err(res, 409, "Can't withdraw that request");
    r.status = "withdrawn";
    r.decided_at = now();
    requestsChanged();
    return json(res, 200, { ok: true }, guestCookie);
  }
  if (p === "/mock/guest") {
    const id = (url.searchParams.get("id") || "x").replace(/[^\w]/g, "");
    res.writeHead(302, { Location: "/", "Set-Cookie": `guest=${id}; Path=/; HttpOnly; SameSite=Lax` });
    return res.end();
  }

  // Approvers
  if (p === "/api/admin/requests") {
    if (!need("approve_requests")) return;
    const st = url.searchParams.get("status") || "all";
    const list = requests.filter((r) => st === "all" || r.status === st).sort((a, b) => b.created_at - a.created_at);
    return json(res, 200, { requests: list.map((r) => strip(r, true)) });
  }
  if (p === "/api/admin/requests/approve-all" && req.method === "POST") {
    if (!need("approve_requests")) return;
    const body = await readBody(req);
    const n = approveAllPending(user.name, body.position === "next" ? "next" : "end");
    if (n) {
      broadcastState();
      requestsChanged();
    }
    return json(res, 200, { ok: true, approved: n });
  }
  if (p === "/api/admin/requests/require-approval" && req.method === "POST") {
    if (!need("approve_requests")) return;
    const body = await readBody(req);
    const approved = setRequireApproval(body.enabled);
    broadcastState();
    requestsChanged();
    return json(res, 200, { ok: true, require_approval: settings.require_approval, approved });
  }
  if ((m = p.match(/^\/api\/admin\/requests\/(\d+)\/recheck$/)) && req.method === "POST") {
    if (!need("approve_requests")) return;
    const r = requests.find((x) => x.id === Number(m[1]));
    if (!r || r.status !== "pending") return err(res, 409, "Only waiting requests can be re-checked");
    if (!autoActive()) return err(res, 409, "Automatic approval is off");
    r.auto = { status: "checking", decision: null, probability: null, note: null };
    setTimeout(() => {
      if (r.status !== "pending") return;
      r.auto = { status: "flagged", decision: "reject", probability: 0.81, note: null };
      requestsChanged();
    }, 1500);
    requestsChanged();
    return json(res, 200, { ok: true });
  }
  if ((m = p.match(/^\/api\/admin\/requests\/(\d+)\/(approve|reject)$/)) && req.method === "POST") {
    if (!need("approve_requests")) return;
    const r = requests.find((x) => x.id === Number(m[1]));
    if (!r) return err(res, 404, "Not found");
    if (r.status !== "pending") return err(res, 409, "Already decided");
    const body = await readBody(req);
    if (m[2] === "approve") {
      approveRequest(r, user.name, body.position === "next" ? "next" : "end");
      broadcastState();
    } else {
      r.status = "rejected";
      r.decided_at = now();
      r.decided_by = user.name;
    }
    requestsChanged();
    return json(res, 200, { request: strip(r, true) });
  }

  // Queue managers
  if (p === "/api/admin/queue" && req.method === "POST") {
    if (!need("queue_add")) return;
    const { track_id, position } = await readBody(req);
    const t = byId.get(track_id);
    if (!t) return err(res, 400, "Unknown track");
    const item = { id: nextId++, track: t, source: "admin", request_id: null };
    position === "next" ? upcoming.unshift(item) : upcoming.push(item);
    broadcastState();
    return json(res, 201, { ok: true });
  }
  if ((m = p.match(/^\/api\/admin\/queue\/(\d+)$/)) && req.method === "DELETE") {
    if (!need("queue_remove")) return;
    upcoming = upcoming.filter((u) => u.id !== Number(m[1]));
    broadcastState();
    return json(res, 200, {});
  }
  if ((m = p.match(/^\/api\/admin\/queue\/(\d+)\/move$/)) && req.method === "POST") {
    if (!need("queue_reorder")) return;
    const { index } = await readBody(req);
    const i = upcoming.findIndex((u) => u.id === Number(m[1]));
    if (i < 0) return err(res, 404, "Not found");
    // Mirror the backend: the head of the queue can't move once it's being mixed in (last 15s of the current track).
    if (i === 0 && mixingIn())
      return err(res, 409, "That track is already being mixed in");
    const [it] = upcoming.splice(i, 1);
    upcoming.splice(Math.max(0, Math.min(upcoming.length, index)), 0, it);
    broadcastState();
    return json(res, 200, {});
  }
  if ((m = p.match(/^\/api\/admin\/player\/(skip|pause|resume|previous|seek)$/)) && req.method === "POST") {
    if (!need(["skip", "previous", "seek"].includes(m[1]) ? "playback_skip" : "playback_pause")) return;
    const pos = () => (paused ? pausedAt : now()) - nowPlaying.started_at;
    if (m[1] === "skip") { paused = false; advance(); }
    else if (m[1] === "previous") {
      const body = await readBody(req);
      if (!nowPlaying) return err(res, 409, "Nothing is playing");
      if (body.track_id != null && body.track_id !== nowPlaying.track.id) return err(res, 409, "The song changed. Try again.");
      if ((typeof body.at === "number" ? body.at : pos()) > 3 || !played.length) nowPlaying.started_at = (paused ? pausedAt : now());
      else {
        upcoming.unshift({ id: nextId++, track: nowPlaying.track, source: nowPlaying.source, request_id: null });
        nowPlaying = { ...played.pop(), started_at: paused ? pausedAt : now() };
      }
    } else if (m[1] === "seek") {
      const body = await readBody(req);
      if (!nowPlaying) return err(res, 409, "Nothing is playing");
      const to = Math.max(0, Math.min(Number(body.position) || 0, nowPlaying.track.duration - 1));
      nowPlaying.started_at = (paused ? pausedAt : now()) - to;
    }
    else if (m[1] === "pause" && !paused) { paused = true; pausedAt = now(); }
    else if (m[1] === "resume" && paused) { paused = false; nowPlaying.started_at += now() - pausedAt; }
    broadcastState();
    return json(res, 200, {});
  }

  // Admin
  if (p === "/api/admin/permissions") {
    if (!user) return err(res, 401, "Not signed in");
    return json(res, 200, { permissions: PERMISSIONS });
  }
  if (p === "/api/admin/users") {
    if (!need("admin")) return;
    return json(res, 200, { users: users.map(publicUser) });
  }
  if (p === "/api/admin/users/bulk" && req.method === "POST") {
    if (!need("admin")) return;
    const body = await readBody(req);
    const ids = body.user_ids;
    if (!Array.isArray(ids) || !ids.length || !ids.every(Number.isInteger)) return err(res, 400, "user_ids must be a non-empty list of ids");
    for (const k of ["add", "remove", "set"]) if (body[k] != null && !Array.isArray(body[k])) return err(res, 400, `${k} must be a list`);
    const r = applyUserChanges(ids, body.is_admin ?? null, body.set ?? null, body.add ?? [], body.remove ?? []);
    if (r) return err(res, r[0], r[1]);
    await new Promise((r) => setTimeout(r, 200));
    return json(res, 200, { ok: true, updated: ids.length });
  }
  if ((m = p.match(/^\/api\/admin\/users\/(\d+)$/))) {
    if (!need("admin")) return;
    const u = users.find((x) => x.id === Number(m[1]));
    if (!u) return err(res, 404, "User not found");
    if (req.method === "PATCH") {
      const body = await readBody(req);
      if (body.permissions != null && !Array.isArray(body.permissions)) return err(res, 400, "permissions must be a list");
      const r = applyUserChanges([u.id], body.is_admin ?? null, body.permissions ?? null, [], []);
      if (r) return err(res, r[0], r[1]);
    } else if (req.method === "DELETE") {
      if (u.is_admin && users.filter((x) => x.is_admin).length === 1) return err(res, 409, "Can't delete the last admin");
      if (u.id === user.id) return err(res, 409, "You can't delete yourself");
      users.splice(users.indexOf(u), 1);
      usersChanged();
    }
    return json(res, 200, { ok: true });
  }
  if (p === "/api/admin/settings") {
    if (!need("admin")) return;
    if (req.method === "PATCH") {
      const { require_approval, ...rest } = await readBody(req);
      Object.assign(settings, rest);
      if (typeof require_approval === "boolean" && require_approval !== settings.require_approval) {
        setRequireApproval(require_approval);
        requestsChanged();
      }
      broadcastState();
    }
    return json(res, 200, settings);
  }
  if (p === "/api/admin/playlists") {
    if (!need("admin")) return;
    return json(res, 200, {
      playlists: ["Party Classics", "Late Night Grooves", "Indie Summer", "90s Throwbacks", "Disco Fever", "Chill Sunday"].map((name, i) => ({
        id: `p.mock${i + 1}`, name, artwork_url: `/mock-art/${i + 2}/600x600bb.svg`,
      })),
    });
  }
  if (p === "/api/admin/playlists/resolve" && req.method === "POST") {
    if (!need("admin")) return;
    const { url_or_id } = await readBody(req);
    const pl = String(url_or_id || "").match(/pl\.[\w-]+/);
    if (!pl) return err(res, 400, "That doesn't look like an Apple Music playlist link");
    return json(res, 200, { id: pl[0], name: "Resolved Catalog Playlist" });
  }
  if (p === "/api/admin/status") {
    if (!need("admin")) return;
    return json(res, 200, {
      apple_music: { configured: true, ok: true, error: null },
      smart_fades: { available: false, error: "analysis model not installed" },
      fallback: { tracks: TRACKS.length, error: null },
      listeners: 4,
      // Same keys as backend/am_party/player/engine.py status().
      engine: {
        paused,
        now_playing: nowPlaying?.track.title ?? null,
        locked_next: upcoming[0]?.track.title ?? null,
        last_transition: "standard",
        last_error: null,
      },
    });
  }
  if (p === "/api/admin/auto-approval") {
    if (!need("admin")) return;
    if (req.method === "PUT") {
      const b = await readBody(req);
      autoApproval.api_key_sources[b.provider ?? "typesafe"] = b.api_key ? "db" : null;
      broadcastState();
    }
    const configured = !!autoApproval.api_key_sources[settings.auto_approval_provider];
    return json(res, 200, { ...autoApproval, configured, active: autoActive() });
  }
  if (p === "/api/admin/apple-music") {
    if (!need("admin")) return;
    if (req.method === "PUT") {
      const b = await readBody(req);
      if (typeof b.developer_token === "string") {
        amState.developer_token_source = b.developer_token ? "db" : null;
        amState.developer_token_expires_at = b.developer_token ? now() + 86400 * 180 : null;
      }
      if (typeof b.user_token === "string") {
        amState.user_token_source = b.user_token ? "db" : null;
        amState.user_token_set_at = b.user_token ? now() : null;
      }
      amState.configured = !!(amState.developer_token_source && amState.user_token_source);
      amState.ok = amState.configured;
      amState.error = amState.configured ? null : "Missing tokens";
    }
    return json(res, 200, amState);
  }
  if (p.startsWith("/api/")) return err(res, 404, "Not found");

  // Static + SPA fallback
  let file = path.join(DIST, path.normalize(p).replace(/^(\.\.[/\\])+/, ""));
  if (!file.startsWith(DIST) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) file = path.join(DIST, "index.html");
  if (!fs.existsSync(file)) {
    res.writeHead(503, { "Content-Type": "text/plain" });
    return res.end("web/dist not found — run `npm run build` first (or use `npm run dev` with this mock on :8930).");
  }
  res.writeHead(200, { "Content-Type": MIME[path.extname(file)] || "application/octet-stream" });
  fs.createReadStream(file).pipe(res);
});

server.on("upgrade", (req, sock) => {
  const p = new URL(req.url, "http://x").pathname;
  if (p !== "/api/ws") return sock.destroy(); // /sendspin is not mocked
  const key = req.headers["sec-websocket-key"];
  const accept = crypto.createHash("sha1").update(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest("base64");
  sock.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
  sock.uid = currentUser(req)?.id ?? null;
  sockets.add(sock);
  wsSend(sock, { type: "state", data: state() });
  sock.on("data", (buf) => {
    const op = buf[0] & 0x0f;
    if (op === 0x8) return sock.end(); // close
    // Text frame (client frames are masked). Only small single-frame messages are expected.
    if (op !== 0x1) return;
    let len = buf[1] & 0x7f;
    let off = 2;
    if (len === 126) { len = buf.readUInt16BE(2); off = 4; }
    else if (len === 127) return;
    const mask = buf.subarray(off, off + 4);
    const payload = Buffer.from(buf.subarray(off + 4, off + 4 + len).map((b, i) => b ^ mask[i % 4]));
    let msg;
    try { msg = JSON.parse(payload.toString("utf8")); } catch { return; }
    if (msg?.type === "ping") wsSend(sock, { type: "pong", server_time: Date.now() / 1000 });
  });
  sock.on("close", () => sockets.delete(sock));
  sock.on("error", () => sockets.delete(sock));
});

server.listen(PORT, () => console.log(`am-party mock backend on http://localhost:${PORT} (serving ${DIST})`));

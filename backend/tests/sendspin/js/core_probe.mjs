// Headless probe: two SendspinCore (sendspin-js 5) clients through the /sendspin route.
// usage: node core_probe.mjs http://am-party:8080 [seconds]
import { SendspinCore } from "@sendspin/sendspin-js";

const base = process.argv[2] ?? "http://am-party:8080";
const seconds = Number(process.argv[3] ?? 8);

function makeClient(name) {
  const st = { name, chunks: 0, firstTs: null, lastTs: null, starts: 0, ends: 0, open: false, fmt: null, ts: new Set() };
  const core = new SendspinCore({ baseUrl: base, clientName: name, codecs: ["pcm", "flac"], storage: null });
  core.onConnectionOpen = () => { st.open = true; };
  core.onStreamStart = (fmt) => { st.starts++; st.fmt = fmt; };
  core.onStreamEnd = () => { st.ends++; };
  core.onAudioData = (c) => {
    st.chunks++;
    st.firstTs ??= c.serverTimeUs;
    st.lastTs = c.serverTimeUs;
    st.ts.add(c.serverTimeUs);
  };
  return { core, st };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const report = (c) => ({
  name: c.st.name, open: c.st.open, connected: c.core.isConnected, chunks: c.st.chunks,
  starts: c.st.starts, fmt: c.st.fmt, sync: c.core.timeSyncInfo, playerState: c.core.playerState,
  isPlaying: c.core.isPlaying,
  group: c.core._stateManager.groupState, meta: c.core._stateManager.serverState?.metadata,
});

const a = makeClient("node-A");
await a.core.connect();
await sleep(seconds * 500);
const b = makeClient("node-B"); // joins mid-stream
await b.core.connect();
await sleep(seconds * 500);
const ra = report(a), rb = report(b);
const common = [...b.st.ts].filter((t) => a.st.ts.has(t)).length;
console.log(JSON.stringify({ a: ra, b: rb, commonTimestamps: common, bTotal: b.st.ts.size }, null, 1));
a.core.disconnect("user_request");
await sleep(300);
const nb = b.st.chunks;
await sleep(1500);
console.log(JSON.stringify({ bStillReceivingAfterADisconnect: b.st.chunks > nb }));
b.core.disconnect("user_request");
await sleep(300);
const ok = ra.chunks > 10 && rb.chunks > 10 && ra.sync.synced && rb.sync.synced && common > 0 && b.st.chunks > nb;
console.log(ok ? "PROBE OK" : "PROBE FAILED");
process.exit(ok ? 0 : 1);

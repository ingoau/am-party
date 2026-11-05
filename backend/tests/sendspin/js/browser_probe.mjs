// Headless Chromium: two real SendspinPlayer instances (two tabs) via the /sendspin route.
// usage: node browser_probe.mjs http://encore:8080
import { chromium } from "playwright";
const base = process.argv[2] ?? "http://encore:8080";
const browser = await chromium.launch({ args: ["--autoplay-policy=no-user-gesture-required"] });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function open(name) {
  const ctx = await browser.newContext(); // separate localStorage => separate client identity
  const page = await ctx.newPage();
  page.on("console", (m) => { if (/error|fail/i.test(m.text())) console.log(`[${name}]`, m.text()); });
  await page.goto(`${base}/?name=${name}`);
  await page.click("#go");
  return page;
}
const snap = (p) => p.evaluate(() => window.encore.snapshot());
const a = await open("chromium-A");
await sleep(5000);
const b = await open("chromium-B");
await sleep(5000);
const sa = await snap(a), sb = await snap(b);
console.log(JSON.stringify({ a: sa, b: sb }, null, 1));
await a.evaluate(() => window.encore.player.setMuted(true));
await sleep(500);
const sa2 = await snap(a), sb2 = await snap(b);
console.log(JSON.stringify({ aMuted: sa2.muted, aPlaying: sa2.playing, bMuted: sb2.muted, bPlaying: sb2.playing }));
await a.evaluate(() => window.encore.player.disconnect("user_request"));
await sleep(1500);
const sb3 = await snap(b);
console.log(JSON.stringify({ bAfterADisconnect: { playing: sb3.playing, state: sb3.playerState } }));
const ok = [sa, sb].every((s) => s.connected && s.playing && s.sync.synced && s.playerState === "synchronized")
  && sa2.muted && !sb2.muted && sb2.playing && sb3.playing;
console.log(ok ? "BROWSER PROBE OK" : "BROWSER PROBE FAILED");
await browser.close();
process.exit(ok ? 0 : 1);

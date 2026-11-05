// Browser test page entry (bundled by esbuild into web/page.js).
import { SendspinPlayer } from "@sendspin/sendspin-js";

const params = new URLSearchParams(location.search);
const states = [];
const player = new SendspinPlayer({
  baseUrl: params.get("base") ?? location.origin, // ws(s)://<host>/sendspin is derived from this
  clientName: params.get("name") ?? "browser",
  productName: "Encore",
  onStateChange: (s) => states.push({ t: Date.now(), playing: s.isPlaying, ps: s.playerState }),
});
window.encore = {
  player,
  states,
  async start() { await player.unlock(); await player.connect(); },
  snapshot() {
    return {
      connected: player.isConnected, playing: player.isPlaying, playerState: player.playerState,
      volume: player.volume, muted: player.muted, format: player.currentFormat,
      sync: player.timeSyncInfo, syncInfo: player.syncInfo, progress: player.trackProgress,
      serverTimeUs: player.getCurrentServerTimeUs(),
    };
  },
};
document.getElementById("go").onclick = () => window.encore.start();

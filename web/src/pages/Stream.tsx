import { Suspense, useState } from "react";
import { Link } from "react-router";
import { Headphones, QrCode as QrIcon, Volume2, VolumeX, WifiOff } from "lucide-react";
import { useLive } from "../hooks/live";
import { useDocumentTitle } from "../hooks/useDocumentTitle";
import { useSendspin } from "../hooks/useSendspin";
import { DeviceSettingsForm } from "../components/DeviceSettings";
import { LazyToaster } from "../components/lazyToast";
import { useListenWithNaming } from "../components/NameSpeakerDialog";
import { QrCode } from "../components/QrCode";
import { cx } from "../util";
import "../styles/stream.css";

/** Listen-only page: connect this device to the party's audio stream, without the TV screen. */
export function StreamPage() {
  const { state, status: wsStatus } = useLive();
  const sp = useSendspin();
  const np = state?.now_playing ?? null;
  const party = state?.party_name || "am-party";
  const listening = sp.status.listening;
  useDocumentTitle(`Listen · ${party}`);
  const { listen, dialog } = useListenWithNaming(sp, state?.speaker_naming_notes ?? "");
  const [showClaim, setShowClaim] = useState(false);
  const base = (state?.public_url || location.origin).replace(/\/+$/, "");

  return (
    <main className="stream">
      <header className="stream__header">
        <h1 className="stream__party">{party}</h1>
        <p className="stream__np" aria-live="polite" aria-atomic="true">
          {np ? (
            <>
              <span className="stream__np-label">{state?.paused ? "Paused" : "Now playing"}</span>{" "}
              <span className="stream__np-track">
                {np.title} · {np.artist}
              </span>
            </>
          ) : state ? (
            "Nothing's playing yet"
          ) : (
            "Tuning in…"
          )}
        </p>
        {wsStatus !== "live" && (
          <span className="chip chip--warn" role="status" title="Live updates">
            <WifiOff aria-hidden />
            {wsStatus === "connecting" ? "Connecting…" : wsStatus === "polling" ? "Reconnecting…" : "Offline"}
          </span>
        )}
      </header>

      <div className="stream__controls">
        <button
          type="button"
          className={cx("btn", "btn--lg", "stream__listen", !listening && "btn--primary")}
          onClick={() => (listening ? sp.stop() : listen())}
        >
          {listening ? (
            <span className="eq" aria-hidden>
              <span />
              <span />
              <span />
              <span />
            </span>
          ) : (
            <Headphones aria-hidden />
          )}
          {listening ? "Stop listening" : "Listen on this device"}
        </button>
        {listening && (
          <button
            type="button"
            className={cx("icon-btn", "stream__mute", sp.muted && "is-muted")}
            onClick={() => sp.setMuted(!sp.muted)}
            disabled={sp.locked}
            aria-label={sp.muted ? "Unmute this device" : "Mute this device"}
            title={sp.locked ? "Controlled by the speaker's owner" : sp.muted ? "Unmute" : "Mute"}
          >
            {sp.muted || sp.volume === 0 ? <VolumeX aria-hidden /> : <Volume2 aria-hidden />}
          </button>
        )}
      </div>

      <section className="stream__settings" aria-label="Device settings">
        <DeviceSettingsForm sp={sp} idPrefix="stream" />
      </section>

      {sp.claimToken && (
        <section className="stream__claim">
          <button type="button" className="btn btn--ghost" aria-expanded={showClaim} onClick={() => setShowClaim((v) => !v)}>
            <QrIcon size={18} aria-hidden />
            {showClaim ? "Hide claim code" : "Claim this speaker"}
          </button>
          {showClaim && (
            <div className="stream__claim-qr">
              <QrCode text={`${base}/?speaker=${encodeURIComponent(sp.claimToken)}`} className="qr" label="QR code: claim this speaker" />
              <p>Scan with a signed-in phone to add “{sp.speaker?.name ?? sp.deviceName}” to your speakers and control it from there.</p>
            </div>
          )}
        </section>
      )}

      <nav className="stream__links">
        <Link to="/">Request a song</Link>
        <Link to="/tv">TV screen</Link>
      </nav>
      {dialog}
      <Suspense fallback={null}>
        <LazyToaster />
      </Suspense>
    </main>
  );
}

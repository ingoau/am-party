import { useEffect, useRef, useState } from "react";
import * as AlertDialog from "@radix-ui/react-alert-dialog";
import { Check, CircleAlert, Loader2 } from "lucide-react";
import { api, ApiError } from "../../api/client";
import { useLive } from "../../hooks/live";
import { errorText } from "./common";

/** What a scanned QR code means to us. */
export type ScannedCode = { kind: "speaker" | "share"; token: string } | { kind: "foreign" } | null;

/**
 * A speaker's claim QR is `${public_url}/?speaker=<token>`; a share code is
 * `${public_url}/admin/speakers?share=<token>`. Either one from an origin not in `origins` is
 * another party's ("foreign"); anything else isn't ours (null).
 */
export function parseScannedCode(text: string, origins: readonly string[]): ScannedCode {
  let url: URL;
  try {
    url = new URL(text.trim());
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  const speaker = url.searchParams.get("speaker");
  const share = /\/admin\/speakers\/?$/.test(url.pathname) ? url.searchParams.get("share") : null;
  if (!speaker && !share) return null;
  if (!origins.includes(url.origin)) return { kind: "foreign" };
  return speaker ? { kind: "speaker", token: speaker } : { kind: "share", token: share! };
}

type Result =
  | { token: string; status: "claiming" }
  | { token: string; status: "claimed" | "already"; name: string }
  | { token: string; status: "error"; message: string };

/** Time between decode attempts, and how long a failed code is left alone before it's tried again. */
const SCAN_INTERVAL = 150;
const RETRY_AFTER = 3000;
/** Frames are scaled down to this width before decoding (plenty for a QR filling part of the view). */
const DECODE_WIDTH = 640;

type Decoder = (video: HTMLVideoElement) => Promise<string[]>;

/** The browser's BarcodeDetector where it reads QR codes (Chrome on Android and macOS), else jsQR. */
async function makeDecoder(): Promise<Decoder> {
  const BD = (globalThis as { BarcodeDetector?: BarcodeDetectorCtor }).BarcodeDetector;
  if (BD) {
    try {
      if ((await BD.getSupportedFormats()).includes("qr_code")) {
        const det = new BD({ formats: ["qr_code"] });
        return async (video) => (await det.detect(video)).map((c) => c.rawValue);
      }
    } catch {
      // fall through to jsQR
    }
  }
  const { default: jsQR } = await import("jsqr");
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  return async (video) => {
    if (!ctx || !video.videoWidth) return [];
    const scale = Math.min(1, DECODE_WIDTH / video.videoWidth);
    const w = Math.round(video.videoWidth * scale);
    const h = Math.round(video.videoHeight * scale);
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    ctx.drawImage(video, 0, 0, w, h);
    const code = jsQR(ctx.getImageData(0, 0, w, h).data, w, h, { inversionAttempts: "dontInvert" });
    return code ? [code.data] : [];
  };
}

interface BarcodeDetectorCtor {
  new (opts: { formats: string[] }): { detect(src: CanvasImageSource): Promise<{ rawValue: string }[]> };
  getSupportedFormats(): Promise<string[]>;
}

const resultText = (r: Result) =>
  r.status === "claiming"
    ? "Claiming…"
    : r.status === "error"
      ? r.message
      : r.status === "already"
        ? `“${r.name}” is already yours`
        : `Claimed “${r.name}”`;

function cameraError(e: unknown): string {
  const name = e instanceof DOMException ? e.name : "";
  if (name === "NotAllowedError" || name === "SecurityError")
    return "Camera access was blocked. Allow it for this site in your browser settings, then try again.";
  if (name === "NotFoundError" || name === "OverconstrainedError") return "No camera found on this device.";
  if (name === "NotReadableError") return "The camera is in use by another app.";
  return "Couldn't start the camera.";
}

/**
 * Camera viewfinder that claims every speaker QR it sees, straight away, and lists what it claimed.
 * A share code (from Share / Hand over) is handed to `onShare` so the accept dialog can take it.
 */
export function SpeakerScanner({ onClose, onShare }: { onClose: () => void; onShare: (token: string) => void }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [camera, setCamera] = useState<"starting" | "on" | { error: string }>("starting");
  const [results, setResults] = useState<Result[]>([]);
  const [hint, setHint] = useState<string | null>(null);
  // token → when it was last handled; `Infinity` once it's settled for good
  const seen = useRef(new Map<string, number>());
  const onShareRef = useRef(onShare);
  onShareRef.current = onShare;
  const { state } = useLive();
  const originsRef = useRef<string[]>([]);
  originsRef.current = [location.origin];
  try {
    if (state?.public_url) originsRef.current.push(new URL(state.public_url).origin);
  } catch {
    // a malformed public_url: only this origin counts
  }

  useEffect(() => {
    let alive = true;
    let stream: MediaStream | null = null;
    let timer = 0;
    let hintTimer = 0;

    const upsert = (r: Result) =>
      setResults((rs) => {
        const i = rs.findIndex((x) => x.token === r.token);
        if (i === -1) return [r, ...rs];
        const next = rs.slice();
        next[i] = r;
        return next;
      });

    const claim = async (token: string) => {
      upsert({ token, status: "claiming" });
      try {
        const preview = await api.claimPreview(token);
        if (preview.already_mine) {
          if (alive) upsert({ token, status: "already", name: preview.speaker.name });
        } else {
          const r = await api.claimSpeaker(token);
          if (alive) upsert({ token, status: "claimed", name: r.speaker.name });
        }
        seen.current.set(token, Infinity);
      } catch (e) {
        // A 4xx answer is final for this code; network trouble or a busy server is tried again if it's still in view.
        const final = e instanceof ApiError && e.status >= 400 && e.status < 500 && e.status !== 408 && e.status !== 429;
        seen.current.set(token, final ? Infinity : Date.now());
        if (alive)
          upsert({
            token,
            status: "error",
            message:
              e instanceof ApiError && e.status === 404
                ? "This code has expired. Reload the speaker's page and scan it again."
                : errorText(e),
          });
      }
    };

    const showHint = (text: string) => {
      setHint(text);
      window.clearTimeout(hintTimer);
      hintTimer = window.setTimeout(() => alive && setHint(null), 2000);
    };

    const handle = (text: string) => {
      const code = parseScannedCode(text, originsRef.current);
      if (!code) return showHint("That isn't a speaker code.");
      if (code.kind === "foreign") return showHint("That code is for a different party.");
      const last = seen.current.get(code.token);
      if (last !== undefined && Date.now() - last < RETRY_AFTER) return;
      seen.current.set(code.token, Infinity);
      navigator.vibrate?.(40);
      if (code.kind === "share") onShareRef.current(code.token);
      else void claim(code.token);
    };

    (async () => {
      if (!navigator.mediaDevices?.getUserMedia) {
        setCamera({
          error: window.isSecureContext
            ? "This browser can't use the camera."
            : "The camera only works over HTTPS. Open this page through its https:// address.",
        });
        return;
      }
      try {
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false });
      } catch (e) {
        if (alive) setCamera({ error: cameraError(e) });
        return;
      }
      const video = videoRef.current;
      if (!alive || !video) {
        stream.getTracks().forEach((t) => t.stop());
        return;
      }
      video.srcObject = stream;
      try {
        await video.play();
      } catch {
        // autoplay of a muted inline video is allowed everywhere we care about; keep going regardless
      }
      let decode: Decoder;
      try {
        decode = await makeDecoder();
      } catch {
        stream.getTracks().forEach((t) => t.stop());
        if (alive) setCamera({ error: "Couldn't load the QR reader. Reload the page and try again." });
        return;
      }
      if (!alive) return;
      // e.g. iOS stops the camera when the app goes to the background
      stream.getVideoTracks()[0]?.addEventListener("ended", () => {
        if (alive) setCamera({ error: "The camera stopped. Close this and tap “Claim speakers” again." });
      });
      setCamera("on");
      const tick = async () => {
        if (!alive) return;
        if (video.readyState >= video.HAVE_CURRENT_DATA) {
          try {
            for (const text of await decode(video)) handle(text);
          } catch {
            // a dropped frame; the next one will do
          }
        }
        if (alive) timer = window.setTimeout(tick, SCAN_INTERVAL);
      };
      void tick();
    })();

    return () => {
      alive = false;
      window.clearTimeout(timer);
      window.clearTimeout(hintTimer);
      stream?.getTracks().forEach((t) => t.stop());
    };
  }, []);

  const claimed = results.filter((r) => r.status === "claimed").length;
  return (
    <AlertDialog.Root open onOpenChange={(o) => !o && onClose()}>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="dialog-overlay" />
        <AlertDialog.Content className="dialog scan-dialog">
          <AlertDialog.Title className="dialog__title">Claim speakers</AlertDialog.Title>
          <AlertDialog.Description className="dialog__desc">
            Point your camera at the QR code on a TV or a phone's Listen page. Each speaker is claimed as soon as its code is
            scanned.
          </AlertDialog.Description>
          <div className="scan-dialog__view">
            {typeof camera === "object" ? (
              <p className="scan-dialog__error" role="alert">
                {camera.error}
              </p>
            ) : (
              <>
                <video ref={videoRef} className="scan-dialog__video" playsInline muted autoPlay aria-label="Camera view" />
                {camera === "starting" && <div className="spinner scan-dialog__spinner" />}
                <div className="scan-dialog__frame" aria-hidden />
                {hint && <p className="scan-dialog__hint">{hint}</p>}
              </>
            )}
          </div>
          <p className="sr-only" role="status">
            {hint ?? (results[0] && resultText(results[0]))}
          </p>
          {results.length > 0 && (
            <ul className="scan-list" aria-label="Scanned speakers">
              {results.map((r) => (
                <li key={r.token} className={`scan-list__item is-${r.status}`}>
                  {r.status === "claiming" ? (
                    <Loader2 size={16} className="spin" aria-hidden />
                  ) : r.status === "error" ? (
                    <CircleAlert size={16} aria-hidden />
                  ) : (
                    <Check size={16} aria-hidden />
                  )}
                  <span>{resultText(r)}</span>
                </li>
              ))}
            </ul>
          )}
          <div className="dialog__actions">
            <AlertDialog.Cancel className="btn btn--primary">{claimed > 0 ? "Done" : "Close"}</AlertDialog.Cancel>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}

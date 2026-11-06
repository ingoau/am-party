import { FastAverageColor } from "fast-average-color";
import { useEffect, useState } from "react";

export type Rgb = [number, number, number];
const cache = new Map<string, Rgb | null>();

function rgbToHsl([r, g, b]: Rgb): [number, number, number] {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  const l = (max + min) / 2;
  if (max === min) return [0, 0, l];
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  let h = max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  h /= 6;
  return [h, s, l];
}

function hslToRgb(h: number, s: number, l: number): Rgb {
  const hue = (p: number, q: number, t: number) => {
    if (t < 0) t += 1;
    if (t > 1) t -= 1;
    if (t < 1 / 6) return p + (q - p) * 6 * t;
    if (t < 1 / 2) return q;
    if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
    return p;
  };
  if (s === 0) return [l * 255, l * 255, l * 255].map(Math.round) as Rgb;
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p = 2 * l - q;
  return [hue(p, q, h + 1 / 3), hue(p, q, h), hue(p, q, h - 1 / 3)].map((v) => Math.round(v * 255)) as Rgb;
}

const fac = new FastAverageColor();

/** Dominant colour of the artwork (via fast-average-color), normalised so it reads as an accent on a dark UI. */
async function sample(url: string): Promise<Rgb | null> {
  const res = await fac.getColorAsync(url, {
    algorithm: "dominant",
    crossOrigin: "anonymous",
    mode: "speed",
    silent: true,
    // Skip near-black / near-white pixels so letterboxing and white covers don't win.
    ignoredColor: [
      [0, 0, 0, 255, 24],
      [255, 255, 255, 255, 16],
    ],
  });
  if (res.error) return null; // e.g. tainted canvas (no CORS headers)
  const [h, s, l] = rgbToHsl([res.value[0], res.value[1], res.value[2]]);
  // Keep it bright & saturated enough to work as an accent on near-black.
  return hslToRgb(h, s < 0.12 ? s : Math.max(0.45, Math.min(0.9, s)), Math.max(0.55, Math.min(0.72, l)));
}

/**
 * Dominant accent colour of an artwork URL, or null if unavailable (CORS taint, error).
 * Pass a size the page already displays so the browser can reuse that download.
 */
export function useArtworkColor(url: string | null | undefined): Rgb | null {
  const [color, setColor] = useState<Rgb | null>(() => (url ? cache.get(url) ?? null : null));
  useEffect(() => {
    if (!url) {
      setColor(null);
      return;
    }
    if (cache.has(url)) {
      setColor(cache.get(url)!);
      return;
    }
    let cancelled = false;
    sample(url)
      .catch(() => null)
      .then((c) => {
        cache.set(url, c);
        if (!cancelled) setColor(c);
      });
    return () => {
      cancelled = true;
    };
  }, [url]);
  return color;
}

/** Apply an accent to the document as CSS custom properties (--accent, --accent-rgb). */
export function useAccentVars(color: Rgb | null) {
  useEffect(() => {
    const root = document.documentElement;
    if (!color) return;
    root.style.setProperty("--accent-rgb", color.join(", "));
    // Also runs on unmount, so the accent doesn't leak into the next page.
    return () => void root.style.removeProperty("--accent-rgb");
  }, [color]);
}

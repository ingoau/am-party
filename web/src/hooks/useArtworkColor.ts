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

/** Pick a vivid-ish dominant colour from the image and normalise it so it reads on a dark UI. */
function sample(img: HTMLImageElement): Rgb | null {
  const size = 32;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) return null;
  ctx.drawImage(img, 0, 0, size, size);
  let data: Uint8ClampedArray;
  try {
    data = ctx.getImageData(0, 0, size, size).data; // throws if the canvas is tainted
  } catch {
    return null;
  }
  // Bucket by hue, weight by saturation so a colourful accent beats large grey areas.
  const buckets = new Map<number, { w: number; r: number; g: number; b: number }>();
  for (let i = 0; i < data.length; i += 4) {
    const px: Rgb = [data[i], data[i + 1], data[i + 2]];
    const [h, s, l] = rgbToHsl(px);
    if (l < 0.08 || l > 0.95) continue;
    const w = 0.05 + s * s * (1 - Math.abs(l - 0.5));
    const key = Math.round(h * 18) % 18;
    const bk = buckets.get(key) ?? { w: 0, r: 0, g: 0, b: 0 };
    bk.w += w; bk.r += px[0] * w; bk.g += px[1] * w; bk.b += px[2] * w;
    buckets.set(key, bk);
  }
  let best: { w: number; r: number; g: number; b: number } | null = null;
  for (const b of buckets.values()) if (!best || b.w > best.w) best = b;
  if (!best || best.w === 0) return null;
  const avg: Rgb = [best.r / best.w, best.g / best.w, best.b / best.w];
  const [h, s, l] = rgbToHsl(avg);
  // Keep it bright & saturated enough to work as an accent on near-black.
  return hslToRgb(h, s < 0.12 ? s : Math.max(0.45, Math.min(0.9, s)), Math.max(0.55, Math.min(0.72, l)));
}

/** Dominant accent colour of an artwork URL, or null if unavailable (CORS taint, error). */
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
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.decoding = "async";
    img.onload = () => {
      const c = sample(img);
      cache.set(url, c);
      if (!cancelled) setColor(c);
    };
    img.onerror = () => {
      cache.set(url, null);
      if (!cancelled) setColor(null);
    };
    img.src = url;
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
    if (color) {
      root.style.setProperty("--accent-rgb", color.join(", "));
    } else {
      root.style.removeProperty("--accent-rgb");
    }
  }, [color]);
}

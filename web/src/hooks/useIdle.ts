import { useEffect, useState } from "react";

/** True after `ms` without pointer/keyboard activity. */
export function useIdle(ms = 3000): boolean {
  const [idle, setIdle] = useState(false);
  useEffect(() => {
    let t: number | undefined;
    const kick = () => {
      setIdle(false);
      window.clearTimeout(t);
      t = window.setTimeout(() => setIdle(true), ms);
    };
    const evs = ["mousemove", "mousedown", "keydown", "touchstart", "wheel"] as const;
    evs.forEach((e) => window.addEventListener(e, kick, { passive: true }));
    kick();
    return () => {
      window.clearTimeout(t);
      evs.forEach((e) => window.removeEventListener(e, kick));
    };
  }, [ms]);
  return idle;
}

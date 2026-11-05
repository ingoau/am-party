import { useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError } from "../../api/client";

/** Small hook for transient feedback messages in admin views. */
export function useFlash() {
  const [flash, setFlash] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const t = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(t.current), []);
  const show = (kind: "ok" | "err", text: string) => {
    setFlash({ kind, text });
    window.clearTimeout(t.current);
    t.current = window.setTimeout(() => setFlash(null), kind === "err" ? 6000 : 3000);
  };
  const node = flash ? (
    <div className={`toast ${flash.kind === "err" ? "toast--err" : ""}`} role="status" onClick={() => setFlash(null)}>
      {flash.text}
    </div>
  ) : null;
  return {
    node,
    ok: (text: string) => show("ok", text),
    err: (e: unknown) => show("err", e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e)),
  };
}

export function PageHeader({ title, sub, children }: { title: string; sub?: string; children?: ReactNode }) {
  return (
    <header className="page-header">
      <div>
        <h1>{title}</h1>
        {sub && <p className="muted">{sub}</p>}
      </div>
      {children && <div className="page-header__actions">{children}</div>}
    </header>
  );
}

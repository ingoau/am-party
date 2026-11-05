import type { ReactNode } from "react";
import { toast } from "sonner";
import { ApiError } from "../../api/client";

export function errorText(e: unknown): string {
  return e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e);
}

/** Transient feedback toasts for admin views (rendered by the <Toaster /> in AdminApp). */
export const flash = {
  ok: (text: string) => void toast.success(text, { duration: 3000 }),
  err: (e: unknown) => void toast.error(errorText(e), { duration: 6000 }),
};

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

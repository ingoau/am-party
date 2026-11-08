import { useEffect, useRef, type ReactNode } from "react";
import { Check, Minus } from "lucide-react";
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

/** Styled checkbox for multi-select lists (supports the indeterminate "some selected" state). */
export function Checkbox({
  checked,
  indeterminate,
  onChange,
  ...rest
}: {
  checked: boolean;
  indeterminate?: boolean;
  onChange: () => void;
  "aria-label": string;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = !!indeterminate;
  }, [indeterminate]);
  return (
    <span className="check">
      <input ref={ref} type="checkbox" checked={checked} onChange={onChange} {...rest} />
      <span className="check__box" aria-hidden>
        {indeterminate ? <Minus size={14} strokeWidth={3} /> : checked ? <Check size={14} strokeWidth={3} /> : null}
      </span>
    </span>
  );
}

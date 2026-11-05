import { useCallback, useRef, useState, type ReactNode } from "react";
import * as AlertDialog from "@radix-ui/react-alert-dialog";
import { cx } from "../../util";

export type ConfirmOptions = {
  title: ReactNode;
  description: ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  icon?: ReactNode;
  /** Red confirm button for destructive actions. */
  danger?: boolean;
};

/**
 * Promise-based accessible confirmation (Radix AlertDialog: focus trap, Esc to cancel,
 * focus returns to the trigger). Render `dialog` once; `await confirm({...})` resolves true/false.
 */
export function useConfirm(): [ReactNode, (o: ConfirmOptions) => Promise<boolean>] {
  const [opts, setOpts] = useState<ConfirmOptions | null>(null);
  const [open, setOpen] = useState(false);
  const resolver = useRef<((v: boolean) => void) | null>(null);

  const confirm = useCallback((o: ConfirmOptions) => {
    resolver.current?.(false);
    setOpts(o);
    setOpen(true);
    return new Promise<boolean>((resolve) => {
      resolver.current = resolve;
    });
  }, []);

  const settle = (v: boolean) => {
    resolver.current?.(v);
    resolver.current = null;
    setOpen(false);
  };

  const dialog = (
    <AlertDialog.Root open={open} onOpenChange={(o) => !o && settle(false)}>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="dialog-overlay" />
        <AlertDialog.Content className="dialog">
          {opts?.icon && <div className={cx("dialog__icon", opts.danger && "dialog__icon--danger")}>{opts.icon}</div>}
          <AlertDialog.Title className="dialog__title">{opts?.title}</AlertDialog.Title>
          <AlertDialog.Description className="dialog__desc">{opts?.description}</AlertDialog.Description>
          <div className="dialog__actions">
            <AlertDialog.Cancel className="btn btn--ghost">{opts?.cancelLabel ?? "Cancel"}</AlertDialog.Cancel>
            <AlertDialog.Action className={cx("btn", opts?.danger ? "btn--danger" : "btn--primary")} onClick={() => settle(true)}>
              {opts?.confirmLabel}
            </AlertDialog.Action>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );

  return [dialog, confirm];
}

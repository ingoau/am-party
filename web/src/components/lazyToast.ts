import { lazy } from "react";
import type { ExternalToast } from "sonner";

/**
 * Deferred sonner for the guest / route: the toast library is fetched after first render
 * (or on the first toast) instead of being on the page's critical path.
 */
export const LazyToaster = lazy(() => import("./Toaster").then((m) => ({ default: m.Toaster })));

export const lazyToast = {
  success: (text: string, opts?: ExternalToast) => void import("sonner").then((m) => m.toast.success(text, opts)),
  error: (text: string, opts?: ExternalToast) => void import("sonner").then((m) => m.toast.error(text, opts)),
};

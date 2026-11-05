import { CircleAlert, CircleCheck } from "lucide-react";
import { Toaster as Sonner } from "sonner";

/** App-wide toast host (sonner), themed to match the dark UI. Mount once per route that toasts. */
export function Toaster() {
  return (
    <Sonner
      theme="dark"
      position="bottom-center"
      gap={8}
      icons={{ success: <CircleCheck size={18} />, error: <CircleAlert size={18} /> }}
      toastOptions={{ className: "toast" }}
    />
  );
}

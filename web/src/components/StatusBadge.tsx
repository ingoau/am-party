import type { RequestStatus } from "../api/types";

export function StatusBadge({ status, admin }: { status: RequestStatus; admin?: boolean }) {
  const label = status === "pending" ? "Pending" : status === "approved" ? "Approved" : admin ? "Rejected" : "Not this time";
  return <span className={`badge badge--${status}`}>{label}</span>;
}

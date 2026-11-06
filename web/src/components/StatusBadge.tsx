import type { RequestStatus } from "../api/types";

const LABEL: Record<RequestStatus, string> = { pending: "Pending", approved: "Approved", rejected: "Rejected", withdrawn: "Withdrawn" };

export function StatusBadge({ status, admin }: { status: RequestStatus; admin?: boolean }) {
  const label = status === "rejected" && !admin ? "Not this time" : LABEL[status];
  return <span className={`badge badge--${status}`}>{label}</span>;
}

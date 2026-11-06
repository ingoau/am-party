import type { RequestStatus } from "../api/types";

const LABEL: Record<RequestStatus, string> = { pending: "Pending", approved: "Approved", rejected: "Declined", withdrawn: "Withdrawn" };

/** Request status as quiet text; only "pending" gets a colour (it needs attention). */
export function StatusBadge({ status }: { status: RequestStatus; admin?: boolean }) {
  return <span className={`status-text status-text--${status}`}>{LABEL[status]}</span>;
}

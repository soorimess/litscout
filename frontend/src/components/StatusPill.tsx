import type { JobStatus } from "../api/types";

const LABELS: Record<JobStatus, string> = {
  queued: "Queued",
  running: "Running",
  awaiting_criteria: "Waiting for your criteria",
  completed: "Finished",
  failed: "Failed",
  cancelled: "Stopped",
};

export default function StatusPill({ status }: { status: JobStatus }) {
  return <span className={`pill ${status}`}>{LABELS[status]}</span>;
}

export const isActive = (s: JobStatus) => s === "queued" || s === "running";

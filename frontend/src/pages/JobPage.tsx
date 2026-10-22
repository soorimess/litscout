import { useCallback, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type { Job, Progress } from "../api/types";
import CriteriaView from "../components/CriteriaView";
import ProgressView from "../components/ProgressView";
import ResultsView from "../components/ResultsView";
import SeedsView from "../components/SeedsView";
import StatusPill, { isActive } from "../components/StatusPill";
import Stepper from "../components/Stepper";
import { fmtCost, fmtDate, fmtDuration, fmtNum } from "../format";
import { usePolling } from "../hooks/usePolling";

const TABS = [["progress", "Progress"], ["results", "Results"], ["criteria", "Review criteria"], ["seeds", "Seed papers"]] as const;
type Tab = (typeof TABS)[number][0];

export default function JobPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const jobPoll = usePolling<Job>(() => api.job(id), 4000, true, [id]);
  const job = jobPoll.data;
  const active = job ? isActive(job.status) : true;
  const progressPoll = usePolling<Progress>(() => api.progress(id), 4000, active, [id]);
  const tab: Tab = (params.get("tab") as Tab) || (job?.status === "awaiting_criteria" ? "criteria" : job?.status === "completed" ? "results" : "progress");

  const refresh = useCallback(() => { void jobPoll.refresh(); void progressPoll.refresh(); }, [jobPoll, progressPoll]);

  async function act(fn: () => Promise<unknown>, after?: () => void) {
    setBusy(true);
    setActionError(null);
    try {
      await fn();
      after ? after() : refresh();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Action failed");
    } finally {
      setBusy(false);
    }
  }

  if (jobPoll.error && !job) return <p className="error">Could not load this review: {jobPoll.error}. <Link to="/">Back to reviews</Link></p>;
  if (!job) return <p className="muted">Loading…</p>;
  const p = progressPoll.data;
  const s = job.summary;

  return (
    <div className="stack">
      <div className="spread">
        <div className="stack" style={{ gap: 6 }}>
          <Link to="/" className="small">← Reviews</Link>
          <div className="row"><h1>{job.title}</h1><StatusPill status={job.status} /></div>
          <p className="muted small">
            {job.mode === "topic" ? `Topic: “${job.topic}” · ${job.n_seeds} seed papers` : `Seed PDFs: ${job.seed_files.join(", ")}`}
            {" · "}started {fmtDate(job.started_at ?? job.created_at)} · {fmtDuration(job.started_at, job.finished_at)}
            {" · "}cost {fmtCost(s.cost_usd)} of {fmtCost(job.max_cost_usd)}
          </p>
        </div>
        <div className="row">
          {job.status === "completed" && (
            <>
              <a className="btn" href={api.exportUrl(job.id, "xlsx")}>Download Excel</a>
              <a className="btn" href={api.exportUrl(job.id, "csv")}>CSV</a>
            </>
          )}
          {isActive(job.status) && <button disabled={busy} onClick={() => void act(() => api.cancel(job.id))}>Stop</button>}
          {(job.status === "failed" || job.status === "cancelled") && (
            <button className="primary" disabled={busy} onClick={() => void act(() => api.resume(job.id))}>Continue</button>
          )}
          {!isActive(job.status) && (
            <button className="danger" disabled={busy} onClick={() => {
              if (window.confirm(`Delete “${job.title}” and all its results? This cannot be undone.`)) {
                void act(() => api.remove(job.id), () => navigate("/"));
              }
            }}>Delete</button>
          )}
        </div>
      </div>

      {job.status === "failed" && job.error && <p className="error">The review stopped: {job.error}</p>}
      {actionError && <p className="error" role="alert">{actionError}</p>}
      {job.status === "queued" && <p className="notice">Waiting for a free slot. Reviews run one or two at a time.</p>}
      {p && <Stepper stage={p.stage} finished={job.status === "completed"} />}

      <div className="tiles">
        <div className="tile"><b>{fmtNum(s.papers)}</b><span>Papers found</span></div>
        <div className="tile"><b>{fmtNum(s.judged)}</b><span>Judged by the LLM</span></div>
        <div className="tile"><b>{fmtNum(s.final)}</b><span>In the final list</span></div>
        <div className="tile"><b>{fmtNum(s.seeds)}</b><span>Seed papers</span></div>
      </div>

      <div className="tabs" role="tablist">
        {TABS.map(([k, label]) => (
          <button key={k} role="tab" aria-selected={tab === k} onClick={() => setParams({ tab: k }, { replace: true })}>{label}</button>
        ))}
      </div>

      {tab === "progress" && (p ? <ProgressView p={p} /> : <p className="muted">Loading progress…</p>)}
      {tab === "results" && <ResultsView jobId={job.id} live={active} />}
      {tab === "criteria" && <CriteriaView job={job} onChanged={refresh} />}
      {tab === "seeds" && <SeedsView jobId={job.id} refreshKey={`${job.status}-${p?.stage.key}`} />}
    </div>
  );
}

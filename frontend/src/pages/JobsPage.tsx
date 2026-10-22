import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { Job } from "../api/types";
import StatusPill, { isActive } from "../components/StatusPill";
import { fmtCost, fmtDate, fmtNum } from "../format";
import { usePolling } from "../hooks/usePolling";

const STAGE_LABEL: Record<string, string> = {
  seeds: "Finding seed papers", criteria: "Review criteria", baseline: "Random baseline",
  crawl: "Snowball and screening", validate: "Final check", done: "Done",
};

export default function JobsPage() {
  const navigate = useNavigate();
  const { data: jobs, error } = usePolling<Job[]>(() => api.jobs(), 5000, true);
  const anyActive = jobs?.some((j) => isActive(j.status));

  return (
    <div className="stack">
      <div className="spread">
        <div>
          <h1>Reviews</h1>
          <p className="muted">{anyActive ? "Updates every 5 seconds while a review runs." : "Your literature reviews."}</p>
        </div>
        <Link to="/new" className="btn primary">New review</Link>
      </div>
      {error && <p className="error">Could not load reviews: {error}</p>}
      {jobs && jobs.length === 0 && (
        <div className="panel empty">
          <h2>No reviews yet</h2>
          <p className="muted">Start one from a topic, or upload the PDFs of papers you already know are relevant.</p>
          <Link to="/new" className="btn primary">Start a review</Link>
        </div>
      )}
      {jobs && jobs.length > 0 && (
        <div className="panel table-wrap" style={{ padding: 0 }}>
          <table>
            <thead>
              <tr>
                <th>Review</th>
                <th>Status</th>
                <th>Step</th>
                <th className="n">Papers found</th>
                <th className="n">Final list</th>
                <th className="n">Cost</th>
                <th>Started</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((j) => (
                <tr key={j.id} className="clickable" onClick={() => navigate(`/jobs/${j.id}`)}>
                  <td className="title-cell">
                    <Link to={`/jobs/${j.id}`} onClick={(e) => e.stopPropagation()}><b>{j.title}</b></Link>
                    <div className="snippet">{j.mode === "topic" ? `Topic · ${j.n_seeds} seed papers` : `${j.seed_files.length} uploaded PDFs`}</div>
                  </td>
                  <td><StatusPill status={j.status} /></td>
                  <td className="muted">{j.stage ? STAGE_LABEL[j.stage] ?? j.stage : "–"}</td>
                  <td className="n">{fmtNum(j.summary.papers)}</td>
                  <td className="n">{fmtNum(j.summary.final)}</td>
                  <td className="n">{fmtCost(j.summary.cost_usd)}</td>
                  <td className="muted">{fmtDate(j.started_at ?? j.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

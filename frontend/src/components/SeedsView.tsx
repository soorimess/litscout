import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Seed } from "../api/types";

export default function SeedsView({ jobId, refreshKey }: { jobId: string; refreshKey: unknown }) {
  const [seeds, setSeeds] = useState<Seed[] | null>(null);
  useEffect(() => { api.seeds(jobId).then(setSeeds).catch(() => setSeeds([])); }, [jobId, refreshKey]);
  if (!seeds) return <p className="muted">Loading…</p>;
  if (!seeds.length) return <p className="muted">No seed papers yet.</p>;
  return (
    <div className="panel table-wrap" style={{ padding: 0 }}>
      <table>
        <thead><tr><th>Seed paper</th><th>Why</th><th>State</th></tr></thead>
        <tbody>
          {seeds.map((s) => (
            <tr key={s.id}>
              <td className="title-cell">
                {s.url ? <a href={s.url} target="_blank" rel="noopener noreferrer"><b>{s.title}</b></a> : <b>{s.title}</b>}
                <div className="snippet">{s.year ?? ""} · {s.origin}</div>
              </td>
              <td className="muted small">{s.reason}</td>
              <td className="muted small">{s.given_up ? `Stopped: ${s.given_up}` : "Searching or not started"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

import type { Progress } from "../api/types";
import { fmt3, fmtCost, fmtNum, fmtPct } from "../format";
import { CalibrationChart, ScoreHistogram } from "./Charts";

export default function ProgressView({ p }: { p: Progress }) {
  const m = p.metrics;
  const seg = m.segments;
  const tiles: [string, string][] = [
    ["Papers scored", fmtNum(m.papers)],
    ["Above relevance threshold", seg ? fmtNum(seg.core + seg.candidate) : "–"],
    ["LLM-confirmed relevant", m.judged ? `${fmtNum(m.judged.relevant)} of ${fmtNum(m.judged.total)}` : "–"],
    ["Seeds added during the run", fmtNum(m.promoted)],
    ["Search rounds", fmtNum(m.search_rounds)],
    ["Relevance threshold", fmt3(m.thresholds?.rel)],
    ["Estimated recall", fmtPct(m.recapture?.recall_est)],
    ["Cost", fmtCost(m.cost_usd)],
  ];
  const seeds = Object.entries(m.seeds ?? {});
  return (
    <div className="stack">
      {m.papers === undefined ? (
        <p className="notice">The crawl has not started yet. Seed papers, review criteria and the random baseline come first.</p>
      ) : (
        <div className="tiles">
          {tiles.map(([k, v]) => (
            <div className="tile" key={k}><b>{v}</b><span>{k}</span></div>
          ))}
        </div>
      )}
      <div className="grid2">
        <section className="panel">
          <h2>Score distribution</h2>
          <p className="muted small">Blue: papers found. Grey: random papers from the same field. Dashed: thresholds{m.thresholds ? ` (${m.thresholds.source})` : ""}.</p>
          <ScoreHistogram m={m} />
        </section>
        <section className="panel">
          <h2>LLM screening</h2>
          <p className="muted small">Green: judged relevant. Grey: not relevant. Line: probability of relevance by score.</p>
          <CalibrationChart m={m} />
        </section>
      </div>
      {seeds.length > 0 && (
        <section className="panel table-wrap">
          <h2>Seed papers</h2>
          <table>
            <thead>
              <tr><th>Seed</th><th>State</th><th className="n">Found</th><th className="n">Relevant</th><th className="n">Best queued score</th></tr>
            </thead>
            <tbody>
              {seeds.map(([id, s]) => (
                <tr key={id}>
                  <td>{s.name}</td>
                  <td>{s.active ? <span className="pill running">Searching</span> : <span className="muted small">{m.given_up?.[id] ?? "Stopped"}</span>}</td>
                  <td className="n">{fmtNum(s.processed)}</td>
                  <td className="n">{fmtNum(s.relevant)}</td>
                  <td className="n">{fmt3(s.frontier_max)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
      <div className="grid2">
        <section className="panel">
          <h2>Decisions</h2>
          {(m.events ?? []).length === 0 ? <p className="muted">None yet.</p> : (
            <ul className="files">
              {(m.events ?? []).slice(0, 15).map((e, i) => (
                <li key={i}>
                  <span><b>{e.kind.replace("_", " ")}</b> <span className="muted">{e.rule}</span></span>
                  <span className="muted small num">{new Date(e.at * 1000).toLocaleTimeString()}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
        <section className="panel">
          <h2>Log</h2>
          <div className="log" aria-live="polite">{p.log.slice(-30).join("\n") || "No output yet."}</div>
        </section>
      </div>
    </div>
  );
}

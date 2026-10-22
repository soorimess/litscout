import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { PaperPage } from "../api/types";
import { fmt3, fmtNum } from "../format";
import { useDebounced } from "../hooks/usePolling";
import PaperDrawer from "./PaperDrawer";

const VIEWS = [["final", "Final list"], ["relevant", "LLM-confirmed"], ["seeds", "Seed papers"], ["all", "All papers"]] as const;
const SORTS = [["rank", "Best first"], ["score", "Score"], ["llm", "LLM score"], ["year", "Newest"], ["citations", "Most cited"], ["title", "Title"]] as const;
const PAGE = 25;

export default function ResultsView({ jobId, live }: { jobId: string; live: boolean }) {
  const [view, setView] = useState<string>("final");
  const [sort, setSort] = useState<string>("rank");
  const [minLlm, setMinLlm] = useState<string>("");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<PaperPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const query = useDebounced(q, 300);

  useEffect(() => setPage(1), [view, sort, minLlm, query]);
  useEffect(() => {
    let stale = false;
    const load = () =>
      api.papers(jobId, { view, sort, q: query, min_llm: minLlm || undefined, page, page_size: PAGE })
        .then((d) => { if (!stale) { setData(d); setError(null); } })
        .catch((e: Error) => !stale && setError(e.message));
    void load();
    const t = live ? window.setInterval(load, 15000) : undefined;
    return () => { stale = true; window.clearInterval(t); };
  }, [jobId, view, sort, minLlm, query, page, live]);

  const pages = data ? Math.max(1, Math.ceil(data.total / PAGE)) : 1;
  const noFinal = view === "final" && data?.total === 0;
  return (
    <div className="stack">
      <div className="row">
        <div className="segmented" role="group" aria-label="Which papers">
          {VIEWS.map(([k, label]) => (
            <button key={k} type="button" aria-pressed={view === k} onClick={() => setView(k)}>{label}</button>
          ))}
        </div>
        <input id="search" type="search" placeholder="Search titles and abstracts" value={q} onChange={(e) => setQ(e.target.value)}
          aria-label="Search" style={{ flex: "1 1 220px" }} />
        <select id="sort" value={sort} onChange={(e) => setSort(e.target.value)} aria-label="Sort">
          {SORTS.map(([k, label]) => <option key={k} value={k}>{label}</option>)}
        </select>
        <select id="min-llm" value={minLlm} onChange={(e) => setMinLlm(e.target.value)} aria-label="Minimum LLM score">
          <option value="">Any LLM score</option>
          <option value="8">LLM 8 or more</option>
          <option value="6">LLM 6 or more</option>
        </select>
      </div>
      {error && <p className="error">{error}</p>}
      {noFinal && <p className="notice">The final list is written when the review finishes. "LLM-confirmed" and "All papers" fill up while it runs.</p>}
      {data && data.total > 0 && (
        <div className="panel table-wrap" style={{ padding: 0 }}>
          <table>
            <thead>
              <tr>
                <th className="n">#</th><th>Title</th><th className="n">Year</th><th className="n">Score</th><th className="n">LLM</th><th>Segment</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((p) => (
                <tr key={p.id} className={`clickable ${open === p.id ? "selected" : ""}`} onClick={() => setOpen(p.id)}>
                  <td className="n muted">{p.rank}</td>
                  <td className="title-cell">
                    <button type="button" className="linklike" style={{ textAlign: "left" }} onClick={(e) => { e.stopPropagation(); setOpen(p.id); }}>
                      <b>{p.title}</b>
                    </button>
                    {p.kind === "repository" && <> <span className="tag">repository</span></>}
                    <div className="snippet">{p.venue ? `${p.venue} · ` : ""}{p.snippet}</div>
                  </td>
                  <td className="n">{p.year ?? "–"}</td>
                  <td className="n">{fmt3(p.score)}</td>
                  <td className="n">{p.llm_score ?? "–"}</td>
                  <td><span className={`tag ${p.segment ?? ""}`}>{p.segment}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data && data.total === 0 && !noFinal && <p className="muted">No papers match.</p>}
      {data && data.total > 0 && (
        <div className="pager">
          <span className="muted small">{fmtNum(data.total)} papers · page {page} of {pages}</span>
          <button disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button>
          <button disabled={page >= pages} onClick={() => setPage(page + 1)}>Next</button>
        </div>
      )}
      {open && <PaperDrawer jobId={jobId} pid={open} onClose={() => setOpen(null)} />}
    </div>
  );
}

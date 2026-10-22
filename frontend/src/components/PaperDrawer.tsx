import { Fragment, useEffect, useState } from "react";
import { api } from "../api/client";
import type { PaperDetail } from "../api/types";
import { fmt3, fmtNum } from "../format";

export default function PaperDrawer({ jobId, pid, onClose }: { jobId: string; pid: string; onClose: () => void }) {
  const [p, setP] = useState<PaperDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setP(null);
    api.paper(jobId, pid).then(setP).catch((e: Error) => setError(e.message));
  }, [jobId, pid]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const how = (f: PaperDetail["found_via"][number]) =>
    f.direction === "search" ? "keyword search" :
      `${f.direction === "refs" ? "reference of" : "cites"} ${f.parent_title ?? "a paper"} (via ${f.seed ?? "seed"})`;

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label="Paper details">
        <div className="spread">
          <span className="muted small">{p?.kind === "repository" ? "GitHub repository" : "Paper"}</span>
          <button onClick={onClose} autoFocus>Close</button>
        </div>
        {error && <p className="error">{error}</p>}
        {!p && !error && <p className="muted">Loading…</p>}
        {p && (
          <>
            <h2>{p.title}</h2>
            <div className="row">
              {p.year && <span className="tag">{p.year}</span>}
              {p.venue && <span className="tag">{p.venue}</span>}
              {p.segment && <span className={`tag ${p.segment}`}>{p.segment}</span>}
              {p.in_final && <span className="tag core">in final list</span>}
            </div>
            {p.url && <a href={p.url} target="_blank" rel="noopener noreferrer">{p.url}</a>}
            {p.llm_score != null && (
              <div className="notice">
                <b>LLM: {p.llm_score}/10 {p.relevant ? "· relevant" : "· not relevant"}</b>
                {p.llm_reason && <p>{p.llm_reason}</p>}
              </div>
            )}
            {p.seed_origin && <div className="notice"><b>Seed paper</b> · {p.seed_origin}{p.seed_reason && <p>{p.seed_reason}</p>}</div>}
            <section>
              <h3>Abstract</h3>
              <p style={{ whiteSpace: "pre-wrap" }}>{p.abstract || <span className="muted">No abstract available.</span>}</p>
            </section>
            <dl className="kv">
              <dt>Score</dt><dd>{fmt3(p.score)}</dd>
              <dt>Similarity to seeds</dt><dd>{fmt3(p.similarity_to_seeds)}</dd>
              <dt>Similarity to topic</dt><dd>{fmt3(p.similarity_to_topic)}</dd>
              <dt>P(relevant)</dt><dd>{fmt3(p.posterior)}</dd>
              <dt>{p.kind === "repository" ? "Stars" : "Citations"}</dt><dd>{fmtNum(p.citations)}</dd>
              {p.doi && (<><dt>DOI</dt><dd className="mono small">{p.doi}</dd></>)}
              {p.arxiv && (<><dt>arXiv</dt><dd className="mono small">{p.arxiv}</dd></>)}
            </dl>
            {p.found_via.length > 0 && (
              <section>
                <h3>How it was found</h3>
                <ul className="files">{p.found_via.slice(0, 8).map((f, i) => <li key={i}>{how(f)}</li>)}</ul>
              </section>
            )}
            {p.per_seed.length > 0 && (
              <section>
                <h3>Similarity to each seed</h3>
                <dl className="kv">{p.per_seed.map((s) => (<Fragment key={s.seed}><dt>{s.seed}</dt><dd>{fmt3(s.cosine)}</dd></Fragment>))}</dl>
              </section>
            )}
          </>
        )}
      </aside>
    </>
  );
}

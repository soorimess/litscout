import { useEffect, useRef, useState, type DragEvent, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { Limits } from "../api/types";

type Mode = "topic" | "pdf";

export default function NewJobPage() {
  const navigate = useNavigate();
  const [limits, setLimits] = useState<Limits | null>(null);
  const [mode, setMode] = useState<Mode>("topic");
  const [topic, setTopic] = useState("");
  const [nSeeds, setNSeeds] = useState(5);
  const [files, setFiles] = useState<File[]>([]);
  const [title, setTitle] = useState("");
  const [reviewCriteria, setReviewCriteria] = useState(false);
  const [cost, setCost] = useState<number | "">("");
  const [over, setOver] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api.limits().then((l) => { setLimits(l); setCost(Math.min(2, l.max_cost_usd)); }).catch(() => undefined);
  }, []);

  function addFiles(list: FileList | null) {
    if (!list) return;
    const pdfs = Array.from(list).filter((f) => f.name.toLowerCase().endsWith(".pdf"));
    const rejected = list.length - pdfs.length;
    const tooBig = limits ? pdfs.filter((f) => f.size > limits.max_pdf_mb * 1024 * 1024) : [];
    setFiles((cur) => {
      const names = new Set(cur.map((f) => f.name));
      return [...cur, ...pdfs.filter((f) => !names.has(f.name) && !tooBig.includes(f))].slice(0, limits?.max_pdfs ?? 20);
    });
    setError(rejected ? `${rejected} file(s) skipped: only PDFs are accepted.` :
      tooBig.length ? `${tooBig.map((f) => f.name).join(", ")}: larger than ${limits?.max_pdf_mb} MB.` : null);
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    setOver(false);
    addFiles(e.dataTransfer.files);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (mode === "pdf" && files.length === 0) return setError("Add at least one PDF.");
    const form = new FormData();
    form.set("mode", mode);
    if (title.trim()) form.set("title", title.trim());
    form.set("review_criteria", String(reviewCriteria));
    if (cost !== "") form.set("max_cost_usd", String(cost));
    if (mode === "topic") {
      form.set("topic", topic);
      form.set("n_seeds", String(nSeeds));
    } else {
      files.forEach((f) => form.append("files", f, f.name));
    }
    setBusy(true);
    try {
      const job = await api.createJob(form);
      navigate(`/jobs/${job.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start the review");
      setBusy(false);
    }
  }

  return (
    <form className="stack" onSubmit={submit} style={{ maxWidth: 760 }}>
      <div>
        <h1>New review</h1>
        <p className="muted">The system finds related papers through citations and searches, ranks them, and has an LLM screen them against review criteria.</p>
      </div>

      <div className="segmented" role="group" aria-label="Start from">
        <button type="button" aria-pressed={mode === "topic"} onClick={() => setMode("topic")}>From a topic</button>
        <button type="button" aria-pressed={mode === "pdf"} onClick={() => setMode("pdf")}>From seed PDFs</button>
      </div>

      <div className="panel stack">
        {mode === "topic" ? (
          <>
            <label htmlFor="topic">
              Topic
              <span className="hint">What the review is about, in a few words or a sentence.</span>
              <textarea id="topic" rows={3} value={topic} onChange={(e) => setTopic(e.target.value)} required minLength={3}
                maxLength={300} placeholder="e.g. machine learning for weather forecasting" />
            </label>
            <label htmlFor="seeds" style={{ maxWidth: 220 }}>
              Seed papers to pick
              <span className="hint">The LLM picks this many of the most relevant search results.</span>
              <input id="seeds" type="number" min={1} max={limits?.max_seeds ?? 20} value={nSeeds}
                onChange={(e) => setNSeeds(Number(e.target.value))} />
            </label>
          </>
        ) : (
          <>
            <div className={`dropzone ${over ? "over" : ""}`} role="button" tabIndex={0}
              onClick={() => input.current?.click()} onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && input.current?.click()}
              onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)} onDrop={onDrop}>
              <b>Drop seed PDFs here</b> or click to choose
              <div className="small muted">
                Up to {limits?.max_pdfs ?? 20} files, {limits?.max_pdf_mb ?? 25} MB each. Files are deleted once the papers are identified; only their links are kept.
              </div>
              <input ref={input} id="files" type="file" accept="application/pdf,.pdf" multiple hidden onChange={(e) => addFiles(e.target.files)} />
            </div>
            {files.length > 0 && (
              <ul className="files">
                {files.map((f) => (
                  <li key={f.name}>
                    <span>{f.name} <span className="muted small">{(f.size / 1024 / 1024).toFixed(1)} MB</span></span>
                    <button type="button" className="linklike" onClick={() => setFiles(files.filter((x) => x !== f))}>Remove</button>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </div>

      <div className="panel stack">
        <label htmlFor="title">
          Name <span className="hint">Optional. Defaults to the topic.</span>
          <input id="title" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} />
        </label>
        <label className="check">
          <input id="review-criteria" type="checkbox" checked={reviewCriteria} onChange={(e) => setReviewCriteria(e.target.checked)} />
          <span>
            Let me check the review criteria first
            <span className="hint" style={{ display: "block" }}>The review pauses after drafting its inclusion and exclusion criteria, so you can edit them before the search starts.</span>
          </span>
        </label>
        <label htmlFor="cost" style={{ maxWidth: 220 }}>
          Cost limit (USD)
          <span className="hint">Up to ${limits?.max_cost_usd.toFixed(2) ?? "–"}. A typical review costs $0.20–$1.</span>
          <input id="cost" type="number" step="0.1" min={0.1} max={limits?.max_cost_usd} value={cost}
            onChange={(e) => setCost(e.target.value === "" ? "" : Number(e.target.value))} />
        </label>
      </div>

      {error && <p className="error" role="alert">{error}</p>}
      <div className="row">
        <button className="primary" disabled={busy}>{busy ? "Starting…" : "Start review"}</button>
        <span className="muted small">A review takes about 30–90 minutes. You can leave this page; it keeps running.</span>
      </div>
    </form>
  );
}

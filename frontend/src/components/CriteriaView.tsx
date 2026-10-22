import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Job } from "../api/types";

export default function CriteriaView({ job, onChanged }: { job: Job; onChanged: () => void }) {
  const [text, setText] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [editable, setEditable] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.criteria(job.id).then((c) => { setText(c.text); setSaved(c.text); setEditable(c.editable); }).catch((e: Error) => setError(e.message));
  }, [job.id, job.status]);

  const dirty = text !== saved;
  const paused = job.status === "awaiting_criteria";

  async function save(thenContinue: boolean) {
    if (text == null) return;
    setBusy(true);
    setError(null);
    try {
      if (dirty) {
        const c = await api.saveCriteria(job.id, text);
        setSaved(c.text);
      }
      if (thenContinue) await api.resume(job.id);
      setMsg(thenContinue ? (paused ? "Saved. The review continues." : "Saved. The review runs again with the new criteria.") : "Saved.");
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save");
    } finally {
      setBusy(false);
    }
  }

  if (text == null && !error) return <p className="muted">The criteria are drafted right after the seed papers are chosen.</p>;
  return (
    <div className="stack">
      {paused && <p className="notice">The review is waiting for you. Check the criteria below, edit them if needed, then continue.</p>}
      {!editable && <p className="muted small">The criteria can be edited when the review is paused or finished.</p>}
      {editable && !paused && job.status === "completed" && (
        <p className="muted small">Editing the criteria of a finished review re-judges its papers under the new text (a few cents) and updates the results.</p>
      )}
      <textarea id="criteria" rows={22} className="mono" value={text ?? ""} readOnly={!editable} onChange={(e) => setText(e.target.value)} aria-label="Review criteria" />
      {error && <p className="error">{error}</p>}
      {msg && <p className="notice">{msg}</p>}
      {editable && (
        <div className="row">
          {paused ? (
            <button className="primary" disabled={busy} onClick={() => void save(true)}>Save and continue</button>
          ) : (
            <>
              <button disabled={busy || !dirty} onClick={() => void save(false)}>Save</button>
              {job.status === "completed" && <button className="primary" disabled={busy || !dirty} onClick={() => void save(true)}>Save and re-run</button>}
            </>
          )}
        </div>
      )}
    </div>
  );
}

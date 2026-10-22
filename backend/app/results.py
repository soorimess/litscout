"""Read-only access to one run's pipeline store and files: progress, papers, seeds, criteria, exports."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

from litscout.report import _url

from .runner import log_tail

# Pipeline stages, recognised from the run log, in order.
STAGES = [
    ("seeds", "Finding seed papers", ("candidate pool", "seed ", "seed:")),
    ("criteria", "Review criteria", ("review criteria written", "Review criteria drafted")),
    ("baseline", "Random baseline", ("building null distribution",)),
    ("crawl", "Snowball and LLM screening", ("round:", "probe:", "confirm:")),
    ("validate", "Final check", ("LLM validation",)),
    ("done", "Done", (" done: ",)),
]
LOG_NOISE = re.compile(r"search (openalex|arxiv|crossref|github|dblp) |HTTP Request|urllib3")
SORTS = {
    "rank": "CASE WHEN p.segment = 'seed' THEN 0 ELSE 1 END, f.score DESC, p.best DESC",
    "score": "p.best DESC",
    "llm": "j.score DESC, p.best DESC",
    "year": "p.year DESC, p.best DESC",
    "citations": "p.cited_by DESC, p.best DESC",
    "title": "p.title COLLATE NOCASE ASC",
}
VIEWS = {
    "final": "(f.include = 1 OR p.segment = 'seed')",
    "relevant": "(j.relevant = 1 OR p.segment = 'seed')",
    "seeds": "p.segment = 'seed'",
    "all": "1 = 1",
}


class RunFiles:
    def __init__(self, run_dir: Path):
        self.dir = run_dir
        self.store_path = run_dir / "store.sqlite"

    # -- low level ----------------------------------------------------------------------------
    def _conn(self) -> Optional[sqlite3.Connection]:
        if not self.store_path.exists():
            return None
        c = sqlite3.connect(f"file:{self.store_path}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        return c

    def _has(self, c: sqlite3.Connection, table: str) -> bool:
        return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

    def meta(self, key: str, default: Any = None) -> Any:
        c = self._conn()
        if not c:
            return default
        with closing(c):
            r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def metrics(self) -> dict[str, Any]:
        p = self.dir / "metrics.json"
        try:
            return json.loads(p.read_text()) if p.exists() else {}
        except (OSError, ValueError):  # being replaced right now; the next poll reads it
            return {}

    # -- progress -----------------------------------------------------------------------------
    def stage(self, tail: str) -> dict[str, Any]:
        reached = 0
        for i, (_, _, marks) in enumerate(STAGES):
            if any(m in tail for m in marks):
                reached = i
        return {"key": STAGES[reached][0], "index": reached,
                "stages": [{"key": k, "label": label} for k, label, _ in STAGES]}

    def progress(self) -> dict[str, Any]:
        tail = log_tail(self.dir / "job.log")
        m = self.metrics()
        lines = [ln for ln in tail.splitlines() if ln.strip() and not LOG_NOISE.search(ln)]
        keep = ("run", "state", "updated", "cost_usd", "thresholds", "null", "papers", "segments", "hist",
                "mixture", "calibration", "recapture", "history", "judged", "promoted", "search_rounds",
                "given_up", "events", "validation")
        out = {k: m.get(k) for k in keep if k in m}
        if "seeds" in m:  # per-seed panels without the long cumulative series
            out["seeds"] = {sid: {k: v for k, v in s.items() if k != "cumulative"} for sid, s in m["seeds"].items()}
        return {"stage": self.stage(tail), "metrics": out, "log": lines[-60:]}

    def summary(self) -> dict[str, Any]:
        c = self._conn()
        m = self.metrics()
        out: dict[str, Any] = {"papers": m.get("papers"), "cost_usd": m.get("cost_usd"), "final": None,
                               "judged": (m.get("judged") or {}).get("total"), "seeds": None}
        if not c:
            return out
        with closing(c):
            if self._has(c, "papers"):
                out["papers"] = c.execute("SELECT COUNT(*) FROM papers WHERE segment IS NOT 'seed' AND best IS NOT NULL").fetchone()[0]
                out["seeds"] = c.execute("SELECT COUNT(*) FROM seeds").fetchone()[0]
            if self._has(c, "final"):
                out["final"] = c.execute("SELECT COUNT(*) FROM final WHERE include=1").fetchone()[0]
            r = c.execute("SELECT value FROM meta WHERE key='cost_usd'").fetchone()
            if r and out["cost_usd"] is None:
                out["cost_usd"] = json.loads(r["value"])
        return out

    # -- papers -------------------------------------------------------------------------------
    def _base(self, c: sqlite3.Connection) -> str:
        final = "LEFT JOIN final f ON f.paper_id = p.id" if self._has(c, "final") else \
            "LEFT JOIN (SELECT NULL AS paper_id, NULL AS score, NULL AS include) f ON f.paper_id = p.id"
        return ("FROM papers p LEFT JOIN judgments j ON j.paper_id = p.id LEFT JOIN seeds s ON s.id = p.id "
                f"{final} WHERE p.best IS NOT NULL")

    def papers(self, view: str = "final", q: str = "", segment: Optional[str] = None, min_llm: Optional[int] = None,
               sort: str = "rank", page: int = 1, page_size: int = 25) -> dict[str, Any]:
        c = self._conn()
        if not c:
            return {"total": 0, "page": page, "page_size": page_size, "items": []}
        where, args = [VIEWS.get(view, VIEWS["final"])], []
        if q:
            where.append("(p.title LIKE ? OR p.abstract LIKE ?)")
            args += [f"%{q}%", f"%{q}%"]
        if segment:
            where.append("p.segment = ?")
            args.append(segment)
        if min_llm is not None:
            where.append("j.score >= ?")
            args.append(min_llm)
        with closing(c):
            base = self._base(c) + "".join(f" AND {w}" for w in where)
            total = c.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
            rows = c.execute(
                "SELECT p.id, p.title, p.year, p.venue, p.cited_by, p.best, p.segment, p.doi, p.arxiv, p.openalex, p.url, "
                "substr(p.abstract, 1, 280) AS snippet, j.score AS llm_score, j.relevant, f.score AS final_score, "
                f"f.include, s.origin AS seed_origin {base} ORDER BY {SORTS.get(sort, SORTS['rank'])} LIMIT ? OFFSET ?",
                args + [page_size, (page - 1) * page_size]).fetchall()
        offset = (page - 1) * page_size
        return {"total": total, "page": page, "page_size": page_size,
                "items": [_paper_row(dict(r), offset + i + 1) for i, r in enumerate(rows)]}

    def paper(self, pid: str) -> Optional[dict[str, Any]]:
        c = self._conn()
        if not c:
            return None
        with closing(c):
            r = c.execute("SELECT p.*, j.score AS llm_score, j.relevant, j.reason AS llm_reason, j.stratum, "
                          "f.score AS final_score, f.include, s.origin AS seed_origin, s.reason AS seed_reason "
                          + self._base(c) + " AND p.id = ?", (pid,)).fetchone()
            if not r:
                return None
            d = dict(r)
            found = [dict(x) for x in c.execute(
                "SELECT d.direction, d.back, d.fwd, d.first, s.name AS seed, pp.title AS parent_title, d.parent_id "
                "FROM discoveries d LEFT JOIN seeds s ON s.id = d.seed_id LEFT JOIN papers pp ON pp.id = d.parent_id "
                "WHERE d.paper_id = ? ORDER BY d.at LIMIT 20", (pid,))]
            sims = [dict(x) for x in c.execute(
                "SELECT s.name AS seed, sc.cosine FROM scores sc JOIN seeds s ON s.id = sc.seed_id "
                "WHERE sc.paper_id = ? ORDER BY sc.cosine DESC", (pid,))]
        item = _paper_row(d, None)
        item.update({"abstract": d.get("abstract"), "llm_reason": d.get("llm_reason"), "judged_as": d.get("stratum"),
                     "similarity_to_seeds": d.get("centroid"), "similarity_to_topic": d.get("topic"),
                     "posterior": d.get("posterior"), "seed_reason": d.get("seed_reason"),
                     "found_via": found, "per_seed": sims})
        return item

    def seeds(self) -> list[dict[str, Any]]:
        c = self._conn()
        if not c:
            return []
        given_up = self.meta("given_up", {}) or {}
        with closing(c):
            rows = [dict(r) for r in c.execute(
                "SELECT s.id, s.name, s.origin, s.reason, p.title, p.year, p.doi, p.arxiv, p.openalex, p.url "
                "FROM seeds s JOIN papers p ON p.id = s.id ORDER BY s.rowid")]
        return [{"id": r["id"], "name": r["name"], "title": r["title"], "year": r["year"], "origin": r["origin"],
                 "reason": r["reason"], "url": _url(r) or None, "given_up": given_up.get(r["id"])} for r in rows]

    # -- criteria and exports -----------------------------------------------------------------------
    @property
    def criteria_path(self) -> Path:
        return self.dir / "criteria.md"

    def criteria(self) -> Optional[str]:
        return self.criteria_path.read_text() if self.criteria_path.exists() else self.meta("criteria")

    def write_criteria(self, text: str) -> None:
        tmp = self.criteria_path.with_suffix(".md.tmp")
        tmp.write_text(text.strip() + "\n")
        tmp.replace(self.criteria_path)

    def export_path(self, fmt: str) -> Optional[Path]:
        p = self.dir / "output" / ("papers.xlsx" if fmt == "xlsx" else "papers.csv")
        return p if p.exists() else None


def _paper_row(d: dict[str, Any], rank: Optional[int]) -> dict[str, Any]:
    is_seed = d.get("segment") == "seed"
    return {
        "id": d["id"], "rank": rank, "title": d.get("title"), "year": d.get("year"), "venue": d.get("venue"),
        "citations": d.get("cited_by"), "score": round(d["best"], 4) if d.get("best") is not None else None,
        "segment": d.get("segment"), "llm_score": d.get("llm_score"),
        "relevant": None if d.get("relevant") is None else bool(d["relevant"]),
        "in_final": True if is_seed else (None if d.get("include") is None else bool(d["include"])),
        "final_score": round(d["final_score"], 4) if d.get("final_score") is not None else None,
        "seed_origin": d.get("seed_origin"), "snippet": d.get("snippet"), "url": _url(d) or None,
        "doi": d.get("doi"), "arxiv": d.get("arxiv"),
        "kind": "repository" if (d.get("url") or "").startswith("https://github.com/") else "paper",
    }

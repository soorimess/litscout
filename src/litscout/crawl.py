"""Best-first snowball crawl per seed, backward (references) and forward (citations), with every
discovered paper embedded and scored against the seeds, and stopping decided by the Monitor."""
from __future__ import annotations

import json
import logging
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .config import Settings
from .llm import LLM, Budget, BudgetExceeded, embed_text
from .monitor import Decision, Monitor
from .sources import Sources
from .sources.http import SourceUnavailable
from .sources.records import merge
from .store import Store

log = logging.getLogger(__name__)
SEED_PRIORITY = 9.0  # seeds are expanded first


class Crawler:
    def __init__(self, s: Settings, store: Store, src: Sources, llm: LLM, budget: Budget, run_dir: Path):
        self.s, self.store, self.src, self.llm, self.budget, self.run_dir = s, store, src, llm, budget, run_dir
        self.c = s.t("crawl", default={}) or {}
        self.monitor: Optional[Monitor] = None
        self.seed_ids: list[str] = []
        self.seed_mat: Optional[np.ndarray] = None
        self.centroid: Optional[np.ndarray] = None
        self.given_up: dict[str, str] = store.meta("given_up", {})
        self.stopped_dirs: dict[str, list[str]] = store.meta("stopped_dirs", {})

    # -- setup ----------------------------------------------------------------------------------
    def prepare(self) -> None:
        seeds = self.store.q("SELECT s.id, s.name, s.expandable, p.title, p.abstract, p.subfield_id "
                             "FROM seeds s JOIN papers p ON p.id = s.id")
        if not seeds:
            raise SystemExit("No seeds in this run.")
        self.seed_ids = [r["id"] for r in seeds]
        missing = [r for r in seeds if not self.store.has_embedding(r["id"])]
        if missing:
            vecs = self.llm.embed([embed_text(r["title"], r["abstract"]) for r in missing])
            self.store.put_embeddings([r["id"] for r in missing], self.s.embedding_model, vecs)
        self.seed_mat = self.store.embeddings(self.seed_ids)
        cen = self.seed_mat.mean(axis=0)
        self.centroid = cen / (np.linalg.norm(cen) or 1.0)
        for r in seeds:
            self.store.x("UPDATE papers SET best=1.0, best_seed=?, centroid=?, segment='seed' WHERE id=?",
                         (r["id"], float(self.store.embeddings([r["id"]])[0] @ self.centroid), r["id"]))
        null = np.array([r["best"] for r in self.store.q("SELECT best FROM null_scores")])
        if len(null) == 0:
            null = self._build_null(seeds)
        self.monitor = Monitor(self.s.t("monitor", default={}) or {}, null, {r["id"]: r["name"] for r in seeds})
        # Resume: replay earlier discoveries into the monitor.
        for r in self.store.q("SELECT d.*, p.best FROM discoveries d JOIN papers p ON p.id = d.paper_id ORDER BY d.at"):
            self.monitor.observe(r["paper_id"], r["seed_id"], r["direction"], r["back"], r["fwd"], r["best"] or 0, bool(r["first"]))
        for r in seeds:
            if r["expandable"] or self.store.paper(r["id"]).get("arxiv") or self.store.paper(r["id"]).get("doi"):
                self.store.x("INSERT OR IGNORE INTO frontier (seed_id, paper_id, priority, back, fwd, state) "
                             "VALUES (?, ?, ?, 0, 0, 'queued')", (r["id"], r["id"], SEED_PRIORITY))
            elif r["id"] not in self.given_up:
                self.given_up[r["id"]] = "local seed (not found online): anchors similarity, cannot be expanded"

    def _build_null(self, seeds: list[Any]) -> np.ndarray:
        n_cfg = self.s.t("null", default={}) or {}
        subfield = None
        if n_cfg.get("scope", "field") == "field":
            subs = Counter(r["subfield_id"] for r in seeds if r["subfield_id"])
            subfield = subs.most_common(1)[0][0] if subs else None
        log.info("building null distribution: %d random papers (%s)", n_cfg.get("sample_size", 2000),
                 f"subfield {subfield}" if subfield else "all fields")
        sample = self.src.openalex.sample(int(n_cfg.get("sample_size", 2000)), int(n_cfg.get("random_seed", 42)), subfield)
        seed_set = set(self.seed_ids)
        sample = [r for r in sample if r["id"] not in seed_set]
        vecs = self.llm.embed([embed_text(r["title"], r.get("abstract")) for r in sample])
        best = (vecs @ self.seed_mat.T).max(axis=1)
        self.store.many("INSERT OR REPLACE INTO null_scores VALUES (?, ?, ?)",
                        [(r["id"], r["title"], float(b)) for r, b in zip(sample, best)])
        self.store.set_meta("null", {"scope": "field" if subfield else "global", "subfield": subfield, "n": len(sample)})
        self.store.event("null", "M1", {"n": len(sample), "subfield": subfield,
                                        "q95": float(np.quantile(best, 0.95)), "q99": float(np.quantile(best, 0.99))})
        return best

    # -- scoring --------------------------------------------------------------------------------
    def _fill_abstracts(self, recs: list[dict[str, Any]]) -> None:
        """Batched: OpenAlex by DOI (also attaches OpenAlex ids to Semantic Scholar records), then arXiv."""
        dois = sorted({r["doi"] for r in recs if r.get("doi") and (not r.get("abstract") or not r.get("openalex"))})
        if dois:
            try:
                got = {g["doi"]: g for g in self.src.openalex.get_by_dois(dois)}
            except SourceUnavailable:
                got = {}
            for r in recs:
                if r.get("doi") in got:
                    g = got[r["doi"]]
                    merge(r, {k: g.get(k) for k in ("openalex", "abstract", "subfield_id", "subfield", "venue", "cited_by")})
        need = [r["arxiv"] for r in recs if not r.get("abstract") and r.get("arxiv")]
        if need:
            try:
                got = {a["arxiv"]: a for a in self.src.arxiv.get_many(need)}
            except SourceUnavailable:
                got = {}
            for r in recs:
                if r.get("arxiv") in got:
                    merge(r, {"abstract": got[r["arxiv"]]["abstract"]})

    def _score(self, ids: list[str]) -> np.ndarray:
        """Embed papers that have no embedding yet, store per-seed cosines and best score."""
        rows = {r["id"]: r for r in self.store.q(
            f"SELECT id, title, abstract FROM papers WHERE id IN ({','.join('?' * len(ids))})", ids)}
        todo = [i for i in ids if not self.store.has_embedding(i)]
        if todo:
            vecs = self.llm.embed([embed_text(rows[i]["title"], rows[i]["abstract"]) for i in todo])
            self.store.put_embeddings(todo, self.s.embedding_model, vecs)
        mat = self.store.embeddings(ids)
        sims = mat @ self.seed_mat.T
        best_idx = sims.argmax(axis=1)
        best = sims.max(axis=1)
        cen = mat @ self.centroid
        self.store.many("INSERT OR REPLACE INTO scores VALUES (?, ?, ?)",
                        [(pid, self.seed_ids[j], float(sims[k, j])) for k, pid in enumerate(ids) for j in range(len(self.seed_ids))])
        self.store.many("UPDATE papers SET best=?, best_seed=?, centroid=? WHERE id=?",
                        [(float(best[k]), self.seed_ids[best_idx[k]], float(cen[k]), pid) for k, pid in enumerate(ids)])
        return best

    # -- expansion ------------------------------------------------------------------------------
    def _fetch(self, seed: str, item: dict[str, Any]) -> dict[str, Any]:
        """Network part of an expansion (runs in a worker thread)."""
        paper = self.store.paper(item["paper_id"])
        out: dict[str, Any] = {"seed": seed, "item": item, "refs": [], "cits": [], "failed": []}
        if not paper or not (paper.get("openalex") or paper.get("doi") or paper.get("arxiv")):
            return out
        dirs = set(("refs", "cits")) - set(self.stopped_dirs.get(seed, []))
        max_depth = self.c.get("max_depth")
        cap_r, cap_c = int(self.c.get("max_references", 500)), int(self.c.get("max_citations", 1000))
        # OpenAlex first; Semantic Scholar when OpenAlex has nothing (common for recent preprints).
        try:
            if "refs" in dirs and (max_depth is None or item["back"] < max_depth):
                if paper.get("openalex"):
                    out["refs"] = self.src.openalex.references({"openalex": paper["openalex"]}, cap_r)
                if not out["refs"]:
                    out["refs"] = self.src.s2.references(paper, cap_r)
            if "cits" in dirs and (max_depth is None or item["fwd"] < max_depth):
                if paper.get("openalex"):
                    out["cits"] = list(self.src.openalex.citations(paper["openalex"], cap_c))
                if not out["cits"]:
                    out["cits"] = self.src.s2.citations(paper, cap_c)
        except SourceUnavailable as e:
            out["failed"].append(str(e))
        return out

    def _absorb(self, res: dict[str, Any]) -> int:
        """Store, score and queue the papers one expansion returned. Returns the number of new papers."""
        seed, item = res["seed"], res["item"]
        if res["failed"]:
            tries = item.get("retries", 0) + 1
            self.store.x("UPDATE frontier SET state=?, retries=? WHERE seed_id=? AND paper_id=?",
                         ("failed" if tries >= 3 else "queued", tries, seed, item["paper_id"]))
            log.warning("expansion of %s failed (%s), attempt %d", item["paper_id"], res["failed"][0][:80], tries)
            return 0
        min_year = self.c.get("min_year")
        batch: list[tuple[str, dict[str, Any]]] = []
        for direction, recs in (("refs", res["refs"]), ("cits", res["cits"])):
            for r in recs:
                if min_year and r.get("year") and r["year"] < min_year:
                    continue
                batch.append((direction, r))
        self._fill_abstracts([r for _, r in batch])
        placed: list[tuple[str, str, bool]] = []
        for direction, r in batch:
            pid, new = self.store.upsert_paper(r)
            if pid in self.seed_ids:
                continue
            placed.append((direction, pid, new))
            src_id, dst_id = (item["paper_id"], pid) if direction == "refs" else (pid, item["paper_id"])
            self.store.x("INSERT OR IGNORE INTO edges VALUES (?, ?, 'cites')", (src_id, dst_id))
        new_ids = sorted({pid for _, pid, new in placed if new})
        if new_ids:
            for i in range(0, len(new_ids), 512):
                self._score(new_ids[i:i + 512])
        best = {r["id"]: r["best"] for r in self.store.q(
            f"SELECT id, best FROM papers WHERE id IN ({','.join('?' * len(placed))})", [p for _, p, _ in placed])} if placed else {}
        now = time.time()
        rows, queue, seen = [], [], set()
        for direction, pid, new in placed:
            if (pid, direction) in seen:
                continue
            seen.add((pid, direction))
            back = item["back"] + (direction == "refs")
            fwd = item["fwd"] + (direction == "cits")
            rows.append((pid, seed, direction, back, fwd, item["paper_id"], int(new), now))
            score = best.get(pid) or 0.0
            self.monitor.observe(pid, seed, direction, back, fwd, score, new)
            if new and score >= self.monitor.t.drop:
                queue.append((seed, pid, score, back, fwd))
        self.store.many("INSERT INTO discoveries VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        self.store.many("INSERT OR IGNORE INTO frontier (seed_id, paper_id, priority, back, fwd, state) "
                        "VALUES (?, ?, ?, ?, ?, 'queued')", queue)
        self.store.x("UPDATE frontier SET state='done' WHERE seed_id=? AND paper_id=?", (seed, item["paper_id"]))
        self.store.x("UPDATE papers SET expanded=1 WHERE id=?", (item["paper_id"],))
        return len(new_ids)

    def _frontier_max(self, seed: str) -> Optional[float]:
        r = self.store.q("SELECT MAX(f.priority) AS m FROM frontier f JOIN papers p ON p.id = f.paper_id "
                         "WHERE f.seed_id=? AND f.state='queued' AND p.expanded=0", (seed,))
        return r[0]["m"] if r and r[0]["m"] is not None else None

    def _pick(self, seed: str, k: int) -> list[dict[str, Any]]:
        rows = self.store.q("SELECT f.*, p.expanded FROM frontier f JOIN papers p ON p.id = f.paper_id "
                            "WHERE f.seed_id=? AND f.state='queued' ORDER BY f.priority DESC LIMIT ?", (seed, k * 4))
        picked = []
        for r in rows:
            if r["expanded"]:  # already expanded through another seed's crawl
                self.store.x("UPDATE frontier SET state='done' WHERE seed_id=? AND paper_id=?", (seed, r["paper_id"]))
                continue
            if r["priority"] < self.monitor.t.rel:
                break
            picked.append(dict(r))
            if len(picked) == k:
                break
        return picked

    # -- main loop ------------------------------------------------------------------------------
    def _active(self) -> set[str]:
        return {s for s in self.seed_ids if s not in self.given_up}

    def _decide(self) -> Decision:
        active = self._active()
        d = self.monitor.decide({s: self._frontier_max(s) for s in self.seed_ids}, active)
        for seed, rule in d.give_up.items():
            if seed not in self.given_up:
                self.given_up[seed] = rule
                self.store.event("give_up", rule, {"seed": seed, "name": self.monitor.seeds.get(seed)})
                log.info("give up seed %s: %s", self.monitor.seeds.get(seed), rule)
        for seed, dirs in d.stop_directions.items():
            if sorted(dirs) != sorted(self.stopped_dirs.get(seed, [])):
                self.stopped_dirs[seed] = dirs
                self.store.event("stop_direction", "M5", {"seed": seed, "directions": dirs})
        self.store.set_meta("given_up", self.given_up)
        self.store.set_meta("stopped_dirs", self.stopped_dirs)
        self.store.set_meta("thresholds", d.snapshot["thresholds"])
        self._write_metrics(d.snapshot, state="crawling")
        return d

    def _write_metrics(self, snap: dict[str, Any], state: str) -> None:
        events = [dict(r) for r in self.store.q("SELECT * FROM events ORDER BY at DESC LIMIT 40")]
        payload = {
            "run": self.store.meta("run", {}), "state": state, "updated": time.time(),
            "cost_usd": round(self.budget.spent, 4), "tokens": self.budget.tokens,
            "given_up": self.given_up, "stopped_dirs": self.stopped_dirs, **snap,
            "events": [{**e, "payload": json.loads(e["payload"])} for e in events],
        }
        tmp = self.run_dir / "metrics.json.tmp"
        tmp.write_text(json.dumps(payload, default=float))
        tmp.replace(self.run_dir / "metrics.json")

    def run(self) -> None:
        self.prepare()
        refit_every = int(self.s.t("monitor", "refit_every", default=200))
        per_seed = int(self.c.get("expand_per_seed_per_round", 4))
        d = self._decide()
        since_fit, stop_rule = 0, d.stop
        with ThreadPoolExecutor(int(self.c.get("workers", 8))) as pool:
            while not stop_rule:
                n_papers = self.store.q("SELECT COUNT(*) AS n FROM papers")[0]["n"]
                if n_papers >= self.s.max_papers:
                    stop_rule = f"budget: MAX_PAPERS={self.s.max_papers}"
                    break
                jobs = [(seed, it) for seed in self._active() for it in self._pick(seed, per_seed)]
                if not jobs:
                    d = self._decide()
                    stop_rule = d.stop or ("no expandable papers left" if not any(self._pick(s, 1) for s in self._active()) else None)
                    continue
                try:
                    for res in pool.map(lambda j: self._fetch(*j), jobs):
                        since_fit += self._absorb(res)
                except BudgetExceeded as e:
                    stop_rule = f"budget: {e}"
                    break
                log.info("round: %d expansions, %d papers, cost $%.3f, tau_rel=%.3f", len(jobs), n_papers,
                         self.budget.spent, self.monitor.t.rel)
                if since_fit >= refit_every:
                    d = self._decide()
                    since_fit, stop_rule = 0, d.stop
        self.store.event("stop", stop_rule or "done", {})
        log.info("crawl stopped: %s", stop_rule)
        self.finalize(stop_rule or "done")

    def finalize(self, rule: str) -> None:
        d = self.monitor.decide({s: self._frontier_max(s) for s in self.seed_ids}, self._active())
        t = d.thresholds
        rows = self.store.q("SELECT id, best FROM papers WHERE segment IS NOT 'seed' AND best IS NOT NULL")
        best = np.array([r["best"] for r in rows], dtype=float)
        post = t.posterior(best) if len(best) else best
        self.store.many("UPDATE papers SET posterior=?, segment=? WHERE id=?",
                        [(float(p), t.segment(float(b)), r["id"]) for r, b, p in zip(rows, best, post)])
        self.store.set_meta("thresholds", d.snapshot["thresholds"])
        self.store.set_meta("cost_usd", self.budget.spent)
        self.store.set_meta("state", "crawled")
        self._write_metrics(d.snapshot, state=f"stopped: {rule}")

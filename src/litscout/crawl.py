"""Best-first snowball crawl per seed, backward (references) and forward (citations). Every discovered
paper is embedded and scored; the Monitor sets thresholds and stopping; the LLM confirms the uncertain
band, confirmed papers are expanded, strong finds become seeds, and keyword searches add new papers."""
from __future__ import annotations

import json
import logging
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

import numpy as np

from . import criteria as crit
from .config import Settings
from .llm import LLM, Budget, BudgetExceeded, embed_text
from .monitor import Decision, Monitor, Thresholds
from .seeds import _search_all
from .sources import Sources
from .sources.http import SourceUnavailable
from .sources.records import dedupe, merge
from .store import Store
from .validate import judge_and_store

log = logging.getLogger(__name__)
SEED_PRIORITY = 9.0     # seeds are expanded first
CONFIRMED_BONUS = 1.0   # LLM-confirmed papers go ahead of every unjudged paper
TOPIC_ID = "_topic_query"

SEARCH_SYSTEM = """You write keyword queries for scholarly search engines (OpenAlex, arXiv, Crossref) to
find more papers for a systematic literature review.

{criteria}

You get titles of papers already confirmed as relevant and the queries already used. Return a JSON
object {{"queries": [...]}} with {k} new short keyword queries (3 to 8 words, no boolean operators or
quotes) that would find relevant papers the earlier queries missed: other phrasings, sub-topics and
named artefacts that appear in the confirmed titles."""


class Crawler:
    def __init__(self, s: Settings, store: Store, src: Sources, llm: LLM, budget: Budget, run_dir: Path):
        self.s, self.store, self.src, self.llm, self.budget, self.run_dir = s, store, src, llm, budget, run_dir
        self.c = s.t("crawl", default={}) or {}
        self.sc = s.t("scoring", default={}) or {}
        self.monitor: Optional[Monitor] = None
        self.seed_ids: list[str] = []
        self.seed_mat: Optional[np.ndarray] = None
        self.centroid: Optional[np.ndarray] = None
        self.topic_vec: Optional[np.ndarray] = None
        self.judge_sys = ""
        self.given_up: dict[str, str] = store.meta("given_up", {})
        self.stopped_dirs: dict[str, list[str]] = store.meta("stopped_dirs", {})

    # -- setup ----------------------------------------------------------------------------------
    def prepare(self) -> None:
        self.judge_sys = crit.judge_system(crit.load(self.store, self.run_dir))
        self._load_seeds()
        if self.store.meta("score_config") != self._score_config() or not self.store.q("SELECT 1 FROM null_embeddings LIMIT 1"):
            self._rescore_all()
        self._build_monitor()
        for r in self.store.q("SELECT s.id, s.expandable, p.arxiv, p.doi FROM seeds s JOIN papers p ON p.id = s.id"):
            if r["expandable"] or r["arxiv"] or r["doi"]:
                self.store.x("INSERT OR IGNORE INTO frontier (seed_id, paper_id, priority, back, fwd, state) "
                             "VALUES (?, ?, ?, 0, 0, 'queued')", (r["id"], r["id"], SEED_PRIORITY))
            elif r["id"] not in self.given_up:
                self.given_up[r["id"]] = "local seed (not found online): anchors similarity, cannot be expanded"

    def _load_seeds(self) -> None:
        seeds = self.store.q("SELECT s.id, p.title, p.abstract FROM seeds s JOIN papers p ON p.id = s.id ORDER BY s.rowid")
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
        w = float(self.sc.get("topic_weight", 0.0))
        if w > 0:
            if not self.store.has_embedding(TOPIC_ID):
                q = f"Instruct: {self.sc.get('topic_instruction', '')}\nQuery: {self.store.meta('topic')}"
                self.store.put_embeddings([TOPIC_ID], self.s.embedding_model, self.llm.embed([q]))
            self.topic_vec = self.store.embeddings([TOPIC_ID])[0]
        for sid in self.seed_ids:
            self.store.x("UPDATE papers SET best=1.0, best_seed=?, segment='seed' WHERE id=?", (sid, sid))

    def _score_config(self) -> dict[str, Any]:
        return {"method": self.sc.get("method", "centroid"), "topic_weight": float(self.sc.get("topic_weight", 0.0)),
                "seeds": sorted(self.seed_ids), "topic": self.store.meta("topic")}

    def _build_monitor(self) -> None:
        null = np.array([r["best"] for r in self.store.q("SELECT best FROM null_scores")])
        names = {r["id"]: r["name"] for r in self.store.q("SELECT id, name FROM seeds")}
        self.monitor = Monitor(self.s.t("monitor", default={}) or {}, null, names)
        for r in self.store.q("SELECT d.*, p.best FROM discoveries d JOIN papers p ON p.id = d.paper_id "
                              "WHERE p.segment IS NOT 'seed' ORDER BY d.at"):
            self.monitor.observe(r["paper_id"], r["seed_id"], r["direction"], r["back"], r["fwd"], r["best"] or 0, bool(r["first"]))
        for r in self.store.q("SELECT paper_id, relevant FROM judgments"):
            self.monitor.label(r["paper_id"], bool(r["relevant"]))
        self.monitor.fit()

    # -- scoring --------------------------------------------------------------------------------
    def _score_mat(self, mat: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, Optional[np.ndarray]]:
        """Paper score: similarity to the seed centroid, mixed with similarity to the topic query (or the
        maximum over seeds with method 'max'). Returns (score, per-seed cosines, centroid, topic)."""
        sims = mat @ self.seed_mat.T
        cen = mat @ self.centroid
        top = mat @ self.topic_vec if self.topic_vec is not None else None
        if self.sc.get("method", "centroid") == "max":
            score = sims.max(axis=1)
        else:
            score = cen
        if top is not None:
            w = float(self.sc.get("topic_weight", 0.0))
            score = (1 - w) * score + w * top
        return score, sims, cen, top

    def _write_scores(self, ids: list[str], mat: np.ndarray) -> np.ndarray:
        score, sims, cen, top = self._score_mat(mat)
        nearest = sims.argmax(axis=1)
        self.store.many("INSERT OR REPLACE INTO scores VALUES (?, ?, ?)",
                        [(pid, self.seed_ids[j], float(sims[k, j])) for k, pid in enumerate(ids) for j in range(len(self.seed_ids))])
        self.store.many("UPDATE papers SET best=?, best_seed=?, centroid=?, topic=? WHERE id=?",
                        [(float(score[k]), self.seed_ids[nearest[k]], float(cen[k]),
                          float(top[k]) if top is not None else None, pid) for k, pid in enumerate(ids)])
        return score

    def _score(self, ids: list[str]) -> np.ndarray:
        """Embed papers that have no embedding yet, then store their scores."""
        rows = {r["id"]: r for r in self.store.q(
            f"SELECT id, title, abstract FROM papers WHERE id IN ({','.join('?' * len(ids))})", ids)}
        todo = [i for i in ids if not self.store.has_embedding(i)]
        if todo:
            vecs = self.llm.embed([embed_text(rows[i]["title"], rows[i]["abstract"]) for i in todo])
            self.store.put_embeddings(todo, self.s.embedding_model, vecs)
        return self._write_scores(ids, self.store.embeddings(ids))

    def _rescore_all(self) -> None:
        """Recompute every score after the seed set or the scoring settings changed (e.g. a promoted seed)."""
        seeds = set(self.seed_ids)
        ids = [r["id"] for r in self.store.q("SELECT p.id FROM papers p JOIN embeddings e ON e.paper_id = p.id") if r["id"] not in seeds]
        for i in range(0, len(ids), 2000):
            chunk = ids[i:i + 2000]
            self._write_scores(chunk, self.store.embeddings(chunk))
        null_ids, null_mat = self.store.null_embeddings()
        if not null_ids:
            self._build_null()
        else:
            score = self._score_mat(null_mat)[0]
            self.store.many("UPDATE null_scores SET best=? WHERE paper_id=?", [(float(b), i) for i, b in zip(null_ids, score)])
        self.store.x("UPDATE frontier SET priority = (SELECT p.best FROM papers p WHERE p.id = frontier.paper_id) + "
                     "CASE WHEN EXISTS (SELECT 1 FROM judgments j WHERE j.paper_id = frontier.paper_id AND j.relevant = 1) "
                     f"THEN {CONFIRMED_BONUS} ELSE 0 END WHERE paper_id != seed_id")
        self.store.set_meta("score_config", self._score_config())
        if ids:
            log.info("rescored %d papers and the random baseline", len(ids))

    def _build_null(self) -> None:
        n_cfg = self.s.t("baseline", default={}) or {}
        subfield = None
        if n_cfg.get("scope", "field") == "field":
            subs = Counter(r["subfield_id"] for r in self.store.q(
                "SELECT p.subfield_id FROM seeds s JOIN papers p ON p.id = s.id WHERE s.origin NOT LIKE 'promoted%'") if r["subfield_id"])
            subfield = subs.most_common(1)[0][0] if subs else None
        log.info("building null distribution: %d random papers (%s)", n_cfg.get("sample_size", 2000),
                 f"subfield {subfield}" if subfield else "all fields")
        sample = self.src.openalex.sample(int(n_cfg.get("sample_size", 2000)), int(n_cfg.get("random_seed", 42)), subfield)
        seed_set = set(self.seed_ids)
        sample = [r for r in sample if r["id"] not in seed_set]
        vecs = self.llm.embed([embed_text(r["title"], r.get("abstract")) for r in sample])
        self.store.put_null_embeddings([r["id"] for r in sample], vecs)
        best = self._score_mat(vecs)[0]
        self.store.x("DELETE FROM null_scores")
        self.store.many("INSERT OR REPLACE INTO null_scores VALUES (?, ?, ?)",
                        [(r["id"], r["title"], float(b)) for r, b in zip(sample, best)])
        self.store.set_meta("null", {"scope": "field" if subfield else "global", "subfield": subfield, "n": len(sample)})
        self.store.event("null", "M1", {"n": len(sample), "subfield": subfield,
                                        "q95": float(np.quantile(best, 0.95)), "q99": float(np.quantile(best, 0.99))})

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

    # -- expansion ------------------------------------------------------------------------------
    def _fetch(self, seed: str, item: dict[str, Any]) -> dict[str, Any]:
        """Network part of an expansion (runs in a worker thread)."""
        paper = self.store.paper(item["paper_id"])
        out: dict[str, Any] = {"seed": seed, "item": item, "refs": [], "cits": [], "failed": []}
        if not paper or not (paper.get("openalex") or paper.get("doi") or paper.get("arxiv")):
            return out
        dirs = set(("refs", "cits")) - set(self.stopped_dirs.get(seed, []))
        if item["priority"] >= CONFIRMED_BONUS:  # LLM-confirmed papers are expanded both ways
            dirs = {"refs", "cits"}
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

    def _queued(self, seeds: set[str]) -> dict[str, float]:
        if not seeds:
            return {}
        rows = self.store.q("SELECT f.paper_id, MAX(f.priority) AS p FROM frontier f JOIN papers p ON p.id = f.paper_id "
                            f"WHERE f.state='queued' AND p.expanded=0 AND f.seed_id IN ({','.join('?' * len(seeds))}) "
                            "GROUP BY f.paper_id", sorted(seeds))
        return {r["paper_id"]: r["p"] for r in rows}

    def _pick(self, seed: str, k: int, confirmed_only: bool = False) -> list[dict[str, Any]]:
        rows = self.store.q("SELECT f.*, p.expanded FROM frontier f JOIN papers p ON p.id = f.paper_id "
                            "WHERE f.seed_id=? AND f.state='queued' ORDER BY f.priority DESC, f.paper_id LIMIT ?", (seed, k * 4))
        picked = []
        for r in rows:
            if r["expanded"]:  # already expanded through another seed's crawl
                self.store.x("UPDATE frontier SET state='done' WHERE seed_id=? AND paper_id=?", (seed, r["paper_id"]))
                continue
            if r["priority"] < (CONFIRMED_BONUS if confirmed_only else self.monitor.t.rel):
                break
            picked.append(dict(r))
            if len(picked) == k:
                break
        return picked

    # -- LLM steps during the crawl ---------------------------------------------------------------
    def _judge(self, ids: list[str], stratum: str) -> list[tuple]:
        if not ids:
            return []
        papers = [dict(r) for r in self.store.q(
            f"SELECT id, title, abstract, year FROM papers WHERE id IN ({','.join('?' * len(ids))})", ids)]
        results = judge_and_store(self.store, self.llm, self.judge_sys, [(p, stratum) for p in papers],
                                  int(self.s.t("validate", "concurrency", default=16)))
        for r in results:
            self.monitor.label(r[0], bool(r[3]))
        return results

    def _probe(self) -> None:
        """M9: the LLM judges a few papers spread over the score range, to calibrate the thresholds."""
        pc = self.s.t("monitor", "probe", default={}) or {}
        n = min(int(pc.get("per_fit", 40)), int(pc.get("max_labels", 400)) - len(self.monitor.labels))
        res = self._judge(self.monitor.probe_candidates(n), "probe")
        if res:
            log.info("probe: LLM judged %d papers (%d relevant), %d labels in total",
                     len(res), sum(r[3] for r in res), len(self.monitor.labels))

    def _confirm(self, t: Thresholds) -> int:
        """The LLM judges every unjudged paper at or above tau_drop, once the thresholds are LLM-calibrated."""
        cc = self.c.get("confirm", {}) or {}
        if not cc.get("enabled", True) or not t.source.startswith("llm"):
            return 0
        rows = self.store.q("SELECT p.id FROM papers p LEFT JOIN judgments j ON j.paper_id = p.id "
                            "WHERE p.best >= ? AND p.segment IS NOT 'seed' AND j.paper_id IS NULL "
                            "ORDER BY p.best DESC LIMIT ?", (t.drop, int(cc.get("max_per_fit", 2000))))
        res = self._judge([r["id"] for r in rows], "confirm")
        if res:
            log.info("confirm: LLM judged %d papers at or above tau_drop, %d relevant", len(res), sum(r[3] for r in res))
            self.store.event("confirm", "LLM check of the uncertain band", {"judged": len(res), "relevant": sum(r[3] for r in res)})
        return len(res)

    def _apply_labels(self, t: Thresholds) -> None:
        """Confirmed papers are queued ahead of the rest (whatever their score); rejected ones below
        tau_core are not expanded."""
        b = CONFIRMED_BONUS
        self.store.x(f"UPDATE frontier SET priority = (SELECT p.best FROM papers p WHERE p.id = frontier.paper_id) + {b}, "
                     "state = CASE WHEN state = 'rejected' THEN 'queued' ELSE state END "
                     "WHERE paper_id != seed_id AND state IN ('queued', 'rejected') AND priority < ? "
                     "AND paper_id IN (SELECT paper_id FROM judgments WHERE relevant = 1)", (b,))
        self.store.x("INSERT OR IGNORE INTO frontier (seed_id, paper_id, priority, back, fwd, state) "
                     f"SELECT d.seed_id, d.paper_id, p.best + {b}, d.back, d.fwd, 'queued' FROM discoveries d "
                     "JOIN papers p ON p.id = d.paper_id JOIN judgments j ON j.paper_id = d.paper_id "
                     "WHERE d.first = 1 AND j.relevant = 1 AND p.expanded = 0 AND p.segment IS NOT 'seed'")
        self.store.x("UPDATE frontier SET state = 'rejected' WHERE state = 'queued' AND paper_id != seed_id "
                     "AND paper_id IN (SELECT j.paper_id FROM judgments j JOIN papers p ON p.id = j.paper_id "
                     "WHERE j.relevant = 0 AND p.best < ?)", (t.core,))

    def _promote(self, t: Thresholds) -> bool:
        """Strong finds (high LLM score and score >= tau_rel) become seeds: they join the centroid and get a frontier."""
        pc = self.c.get("promote", {}) or {}
        done = self.store.q("SELECT COUNT(*) AS n FROM seeds WHERE origin LIKE 'promoted%'")[0]["n"]
        n = min(int(pc.get("max_per_fit", 2)), int(pc.get("max_total", 5)) - done)
        if n <= 0 or not t.source.startswith("llm"):
            return False
        rows = self.store.q("SELECT p.id, p.title, p.best, j.score, j.reason FROM judgments j JOIN papers p ON p.id = j.paper_id "
                            "WHERE j.score >= ? AND p.best >= ? AND p.segment IS NOT 'seed' "
                            "AND (p.openalex IS NOT NULL OR p.doi IS NOT NULL OR p.arxiv IS NOT NULL) "
                            "ORDER BY j.score DESC, p.best DESC LIMIT ?", (int(pc.get("min_llm", 9)), t.rel, n))
        if not rows:
            return False
        from .seeds import _seed_name
        for r in rows:
            self.store.x("INSERT OR REPLACE INTO seeds VALUES (?, ?, ?, ?, 1)",
                         (r["id"], _seed_name({"title": r["title"]}), f"promoted (LLM {r['score']}, score {r['best']:.3f})", r["reason"]))
            self.store.x("UPDATE frontier SET state='done' WHERE paper_id=? AND seed_id != ?", (r["id"], r["id"]))
            # Its own frontier: the paper itself if not yet expanded, else its known neighbours.
            self.store.x("INSERT OR IGNORE INTO frontier (seed_id, paper_id, priority, back, fwd, state) "
                         "SELECT ?, ?, ?, 0, 0, 'queued' FROM papers WHERE id=? AND expanded=0", (r["id"], r["id"], SEED_PRIORITY, r["id"]))
            for sql, back, fwd in (("SELECT dst AS nb FROM edges WHERE src=?", 1, 0), ("SELECT src AS nb FROM edges WHERE dst=?", 0, 1)):
                self.store.x(f"INSERT OR IGNORE INTO frontier (seed_id, paper_id, priority, back, fwd, state) "
                             f"SELECT ?, p.id, p.best, {back}, {fwd}, 'queued' FROM ({sql}) e JOIN papers p ON p.id = e.nb "
                             "WHERE p.expanded = 0 AND p.best >= ?", (r["id"], r["id"], t.drop))
            self.store.event("promote", "strong find promoted to seed", {"seed": r["id"], "title": r["title"], "llm": r["score"]})
            log.info("promote to seed: %s (LLM %d, score %.3f)", r["title"][:80], r["score"], r["best"])
        self._load_seeds()
        self._rescore_all()
        self._build_monitor()
        self._apply_labels(self.monitor.t)
        return True

    def _search(self, t: Thresholds) -> int:
        """Keyword searches written from the confirmed papers: finds recent papers the citation graph lacks."""
        sc = self.c.get("search", {}) or {}
        rounds = self.store.meta("search_rounds", 0)
        n_rel = self.store.q("SELECT COUNT(*) AS n FROM judgments WHERE relevant=1")[0]["n"]
        last = self.store.meta("search_last_relevant", 0)
        if rounds >= int(sc.get("rounds", 5)) or n_rel - last < int(sc.get("every_new_relevant", 15)) or not t.source.startswith("llm"):
            return 0
        titles = [r["title"] for r in self.store.q(
            "SELECT p.title FROM judgments j JOIN papers p ON p.id = j.paper_id WHERE j.relevant=1 "
            "ORDER BY p.year DESC, j.score DESC LIMIT ?", (int(sc.get("examples", 25)),))]
        used = self.store.meta("queries", []) + self.store.meta("crawl_queries", [])
        k = int(sc.get("queries", 5))
        out = self.llm.chat_json(SEARCH_SYSTEM.format(criteria=self.store.meta("criteria"), k=k),
                                 "Confirmed relevant papers:\n" + "\n".join(f"- {x}" for x in titles) +
                                 "\n\nQueries already used:\n" + "\n".join(f"- {q}" for q in used))
        queries = [q for q in out.get("queries", []) if isinstance(q, str) and q not in used][:k]
        recs = dedupe(_search_all(self.src, queries, sc.get("per_query", {"openalex": 25, "arxiv": 25, "crossref": 15})))
        self._fill_abstracts(recs)
        new_ids = []
        for r in recs:
            pid, new = self.store.upsert_paper(r)
            if new:
                new_ids.append(pid)
        for i in range(0, len(new_ids), 512):
            self._score(new_ids[i:i + 512])
        rows, queue, now = [], [], time.time()
        for r in (self.store.q(f"SELECT id, best, best_seed FROM papers WHERE id IN ({','.join('?' * len(new_ids))})", new_ids) if new_ids else []):
            rows.append((r["id"], r["best_seed"], "search", 0, 0, None, 1, now))
            self.monitor.observe(r["id"], r["best_seed"], "search", 0, 0, r["best"], True)
            if r["best"] >= t.drop:
                queue.append((r["best_seed"], r["id"], r["best"], 0, 0))
        self.store.many("INSERT INTO discoveries VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        self.store.many("INSERT OR IGNORE INTO frontier (seed_id, paper_id, priority, back, fwd, state) "
                        "VALUES (?, ?, ?, ?, ?, 'queued')", queue)
        self.store.set_meta("crawl_queries", self.store.meta("crawl_queries", []) + queries)
        self.store.set_meta("search_rounds", rounds + 1)
        self.store.set_meta("search_last_relevant", n_rel)
        self.store.event("search", "keyword search from confirmed papers",
                         {"queries": queries, "hits": len(recs), "new": len(new_ids), "queued": len(queue)})
        log.info("search round %d: %d queries, %d hits, %d new papers, %d queued", rounds + 1, len(queries), len(recs), len(new_ids), len(queue))
        return len(new_ids)

    # -- main loop ------------------------------------------------------------------------------
    def _active(self) -> set[str]:
        return {s for s in self.seed_ids if s not in self.given_up}

    def _jobs(self, k: int) -> list[tuple[str, dict[str, Any]]]:
        """Best papers of the active seeds, plus LLM-confirmed papers queued by seeds already given up:
        a confirmed paper is always expanded."""
        jobs = []
        for seed in self.seed_ids:
            jobs += [(seed, it) for it in self._pick(seed, k, confirmed_only=seed in self.given_up)]
        return jobs

    def _decide(self) -> Decision:
        self._probe()
        t = self.monitor.fit()
        self._confirm(t)
        self._apply_labels(t)
        if self._promote(t):
            t = self.monitor.t
        self._search(t)
        for seed in list(self.given_up):  # a seed given up for an empty frontier comes back when confirmed papers arrive
            if self.given_up[seed].startswith("M6") and (self._frontier_max(seed) or 0) >= t.rel:
                del self.given_up[seed]
                self.store.event("revive", "confirmed papers queued", {"seed": seed, "name": self.monitor.seeds.get(seed)})
                log.info("revive seed %s: confirmed papers queued", self.monitor.seeds.get(seed))
        active = self._active()
        d = self.monitor.decide({s: self._frontier_max(s) for s in self.seed_ids}, active, self._queued(active))
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
        self._assign_segments(d.thresholds)
        self._write_metrics(d.snapshot, state="crawling")
        return d

    def _assign_segments(self, t: Thresholds) -> None:
        rows = self.store.q("SELECT id, best FROM papers WHERE segment IS NOT 'seed' AND best IS NOT NULL")
        best = np.array([r["best"] for r in rows], dtype=float)
        post = t.posterior(best) if len(best) else best
        self.store.many("UPDATE papers SET posterior=?, segment=? WHERE id=?",
                        [(float(p), t.segment(float(b)), r["id"]) for r, b, p in zip(rows, best, post)])

    def _write_metrics(self, snap: dict[str, Any], state: str) -> None:
        events = [dict(r) for r in self.store.q("SELECT * FROM events ORDER BY at DESC LIMIT 40")]
        j = self.store.q("SELECT COUNT(*) AS n, COALESCE(SUM(relevant), 0) AS r FROM judgments")[0]
        payload = {
            "run": self.store.meta("run", {}), "state": state, "updated": time.time(),
            "cost_usd": round(self.budget.spent, 4), "tokens": self.budget.tokens,
            "given_up": self.given_up, "stopped_dirs": self.stopped_dirs, **snap,
            "judged": {"total": j["n"], "relevant": j["r"]},
            "promoted": self.store.q("SELECT COUNT(*) AS n FROM seeds WHERE origin LIKE 'promoted%'")[0]["n"],
            "search_rounds": self.store.meta("search_rounds", 0),
            "events": [{**e, "payload": json.loads(e["payload"])} for e in events],
        }
        tmp = self.run_dir / "metrics.json.tmp"
        tmp.write_text(json.dumps(payload, default=float))
        tmp.replace(self.run_dir / "metrics.json")
        self.store.set_meta("cost_usd", self.budget.spent)

    def run(self) -> None:
        self.prepare()
        refit_every = int(self.s.t("monitor", "refit_every", default=200))
        per_seed = int(self.c.get("expand_per_seed_per_round", 4))
        stop_rule: Optional[str] = None
        try:
            d = self._decide()
            since_fit, stop_rule = 0, d.stop
            with ThreadPoolExecutor(int(self.c.get("workers", 8))) as pool:
                while True:
                    n_papers = self.store.q("SELECT COUNT(*) AS n FROM papers")[0]["n"]
                    if n_papers >= self.s.max_papers:
                        stop_rule = f"budget: MAX_PAPERS={self.s.max_papers}"
                        break
                    jobs = self._jobs(per_seed)
                    if stop_rule:  # after a stop rule, only confirmed papers still waiting are expanded
                        jobs = [j for j in jobs if j[1]["priority"] >= CONFIRMED_BONUS]
                        if not jobs:
                            break
                    if not jobs:
                        d = self._decide()
                        since_fit = 0
                        stop_rule = d.stop or (None if self._jobs(1) else "no expandable papers left")
                        continue
                    for res in pool.map(lambda j: self._fetch(*j), jobs):
                        since_fit += self._absorb(res)
                    log.info("round: %d expansions, %d papers, cost $%.3f, tau_rel=%.3f", len(jobs), n_papers,
                             self.budget.spent, self.monitor.t.rel)
                    if since_fit >= refit_every:
                        d = self._decide()
                        since_fit, stop_rule = 0, d.stop
        except BudgetExceeded as e:
            stop_rule = f"budget: {e}"
        self.store.event("stop", stop_rule or "done", {})
        log.info("crawl stopped: %s", stop_rule)
        self.finalize(stop_rule or "done")

    def finalize(self, rule: str) -> None:
        d = self.monitor.decide({s: self._frontier_max(s) for s in self.seed_ids}, self._active(), {})
        self._assign_segments(d.thresholds)
        self.store.set_meta("thresholds", d.snapshot["thresholds"])
        self.store.set_meta("state", "crawled")
        self._write_metrics(d.snapshot, state=f"stopped: {rule}")

    def unexpanded_relevant(self) -> int:
        """LLM-confirmed papers that could still be expanded (e.g. confirmed by the final check)."""
        return self.store.q("SELECT COUNT(*) AS n FROM judgments j JOIN papers p ON p.id = j.paper_id WHERE j.relevant = 1 "
                            "AND p.expanded = 0 AND p.segment IS NOT 'seed' "
                            "AND (p.openalex IS NOT NULL OR p.doi IS NOT NULL OR p.arxiv IS NOT NULL)")[0]["n"]

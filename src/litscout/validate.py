"""LLM check against the review criteria: every paper at or above tau_drop, plus a sample of the discard
segment; then ranking quality, calibration and the final list."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
from scipy.stats import spearmanr
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import roc_auc_score

from . import criteria as crit
from .config import Settings
from .llm import LLM, BudgetExceeded
from .store import Store

log = logging.getLogger(__name__)
SEGMENTS = ("core", "candidate", "boundary", "discard")


def _judge(llm: LLM, system: str, p: dict[str, Any], stratum: str) -> tuple:
    user = f"Title: {p['title']}\nYear: {p['year'] or 'unknown'}\nAbstract: {p['abstract'] or '[abstract unavailable]'}"
    try:
        out = llm.chat_json(system, user)
        score = int(out.get("score", 0))
        rel = bool(out.get("relevant", score >= 6)) and score >= 6
        return (p["id"], stratum, score, int(rel), str(out.get("reason", ""))[:500], llm.s.llm_model)
    except BudgetExceeded:
        raise
    except Exception as e:  # keep going; the paper stays unjudged and is retried on the next run
        log.warning("judge failed for %s: %s", p["id"], e)
        return None


def judge_many(llm: LLM, system: str, items: list[tuple[dict[str, Any], str]], workers: int) -> list[tuple]:
    """Judge papers in parallel; on a budget stop, keep what was judged so far."""
    out: list[tuple] = []
    with ThreadPoolExecutor(workers) as pool:
        try:
            for r in pool.map(lambda t: _judge(llm, system, *t), items):
                if r:
                    out.append(r)
        except BudgetExceeded as e:
            log.warning("judging stopped: %s", e)
    return out


def judge_and_store(store: Store, llm: LLM, system: str, items: list[tuple[dict[str, Any], str]], workers: int) -> list[tuple]:
    results = judge_many(llm, system, items, workers)
    store.many("INSERT OR REPLACE INTO judgments VALUES (?, ?, ?, ?, ?, ?)", results)
    return results


def validate(s: Settings, store: Store, llm: LLM, run_dir: Path) -> dict[str, Any]:
    cfg = s.t("validate", default={}) or {}
    system = crit.judge_system(crit.load(store, run_dir))
    rng = np.random.default_rng(42)
    done = {r["paper_id"] for r in store.q("SELECT paper_id FROM judgments")}  # includes the crawl's judgments
    seg_rows = {seg: [dict(r) for r in store.q("SELECT id, title, abstract, year, best, best_seed FROM papers WHERE segment=?", (seg,))]
                for seg in SEGMENTS}
    # Everything at or above tau_drop is judged; discard is sampled up to sample_per_stratum judged papers.
    todo = [(p, seg) for seg in ("core", "candidate", "boundary") for p in seg_rows[seg] if p["id"] not in done]
    cap = int(cfg.get("max_judge", 20000))
    if len(todo) > cap:
        log.warning("validation: %d papers above tau_drop, judging the %d best-scored", len(todo), cap)
        todo = sorted(todo, key=lambda t: -(t[0]["best"] or 0))[:cap]
    disc = seg_rows["discard"]
    need = max(0, int(cfg.get("sample_per_stratum", 200)) - sum(1 for p in disc if p["id"] in done))
    free = [p for p in disc if p["id"] not in done]
    todo += [(free[i], "discard") for i in rng.choice(len(free), min(need, len(free)), replace=False)] if free else []
    log.info("LLM validation: %d papers to judge (%d already judged)", len(todo), len(done))
    judge_and_store(store, llm, system, todo, int(cfg.get("concurrency", 16)))

    rows = [dict(r) for r in store.q("SELECT j.*, p.best, p.best_seed, p.segment FROM judgments j "
                                     "JOIN papers p ON p.id = j.paper_id WHERE p.segment IS NOT 'seed'")]
    if len(rows) < 10 or len({r["relevant"] for r in rows}) < 2:
        report = {"note": "too few or one-sided judgments to evaluate the ranking", "judged": len(rows)}
        store.set_meta("validation", report)
        return report
    # Each judged paper stands for its segment: weight = segment size / judged papers in that segment.
    judged_per_seg: dict[str, int] = {}
    for r in rows:
        judged_per_seg[r["segment"]] = judged_per_seg.get(r["segment"], 0) + 1
    weights = {seg: len(seg_rows.get(seg, [])) / n if seg in seg_rows else 1.0 for seg, n in judged_per_seg.items()}
    c = np.array([r["best"] for r in rows])
    y = np.array([r["relevant"] for r in rows])
    w = np.array([weights.get(r["segment"], 1.0) for r in rows])
    llm_score = np.array([r["score"] for r in rows])
    auc_v = float(roc_auc_score(y, c, sample_weight=w))
    rho = float(spearmanr(c, llm_score).correlation)
    order = np.argsort(-c)
    prec_at = {k: float(y[order[:k]].mean()) for k in (50, 100, 250, 500, 1000) if k <= len(order)}
    per_seed = {}
    for seed in {r["best_seed"] for r in rows}:
        m = np.array([r["best_seed"] == seed for r in rows])
        if m.sum() >= 20 and len(set(y[m])) == 2:
            per_seed[seed] = round(float(roc_auc_score(y[m], c[m], sample_weight=w[m])), 4)
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1).fit(c, y, sample_weight=w)
    # Cut: the highest score that still reaches the target recall (weighted by stratum sampling rates).
    target = float(cfg.get("target_recall", 0.95))
    total_rel = float((w * y).sum())
    cut, est_prec = float(c.min()), None
    for th in np.unique(c)[::-1]:
        sel = c >= th
        if (w * y * sel).sum() / total_rel >= target:
            cut, est_prec = float(th), float((w * y * sel).sum() / (w * sel).sum())
            break
    # Relevant papers the sample suggests are still unjudged in the discard segment.
    disc_judged = [r for r in rows if r["segment"] == "discard"]
    missed = (sum(r["relevant"] for r in disc_judged) / len(disc_judged) * len(disc)) if disc_judged else None
    weak = auc_v < float(cfg.get("weak_auc", 0.8))
    allp = store.q("SELECT id, best FROM papers WHERE best IS NOT NULL AND segment IS NOT 'seed'")
    judged = {r["paper_id"]: r for r in rows}
    final = []
    for r in allp:
        p_cal = float(iso.predict([r["best"]])[0])
        j = judged.get(r["id"])
        # Judged papers rank by the LLM score and the calibrated score together (the LLM read the
        # abstract); isotonic calibration is a step function, so the raw score breaks its ties.
        score = (0.5 * p_cal + 0.5 * j["score"] / 10 if j else p_cal) + 1e-3 * r["best"]
        include = (bool(j["relevant"]) if j else (r["best"] >= cut and p_cal >= 0.5))
        final.append((score, int(include), r["id"]))
    store.x("CREATE TABLE IF NOT EXISTS final (paper_id TEXT PRIMARY KEY, score REAL, include INTEGER)")
    store.many("INSERT OR REPLACE INTO final VALUES (?, ?, ?)", [(i, sc, inc) for sc, inc, i in final])
    report = {
        "judged": len(rows), "relevant_judged": int(y.sum()), "auc": round(auc_v, 4), "spearman": round(rho, 4),
        "precision_at": {str(k): round(v, 4) for k, v in prec_at.items()}, "per_seed_auc": per_seed,
        "cut": round(cut, 4), "target_recall": target, "est_precision_at_cut": round(est_prec, 4) if est_prec else None,
        "est_relevant_left_in_discard": round(missed, 1) if missed is not None else None,
        "ranking": "judged: 0.5 calibrated score + 0.5 LLM score; others: calibrated score; ties by score",
        "score_ranking_weak": weak,
        "final_included": sum(inc for _, inc, _ in final),
        "criteria_hash": store.meta("criteria_hash"),
    }
    store.set_meta("validation", report)
    store.event("validation", "final check", report)
    log.info("validation: AUC %.3f, Spearman %.3f, cut %.3f, %d papers in the final list",
             auc_v, rho, cut, report["final_included"])
    return report

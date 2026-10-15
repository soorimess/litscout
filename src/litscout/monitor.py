"""Live analysis of the similarity stream (docs/distribution-analysis.md, methods M1-M8).

The crawl reports every discovery; the monitor refits periodically and returns thresholds and
decisions: which seeds or directions to give up and whether to stop globally."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
from sklearn.mixture import GaussianMixture

EPS = 1e-6


def _atanh(c: np.ndarray) -> np.ndarray:
    return np.arctanh(np.clip(c, -1 + 1e-4, 1 - 1e-4))


def auc(pos: np.ndarray, neg: np.ndarray) -> float:
    """P(random pos > random neg), ties counted half (Mann-Whitney U / (n_pos * n_neg))."""
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    allv = np.concatenate([pos, neg])
    ranks = _rankdata(allv)
    r_pos = ranks[: len(pos)].sum()
    return float((r_pos - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def _rankdata(v: np.ndarray) -> np.ndarray:
    order = np.argsort(v, kind="mergesort")
    ranks = np.empty(len(v), dtype=float)
    sv = v[order]
    i = 0
    while i < len(v):
        j = i
        while j + 1 < len(v) and sv[j + 1] == sv[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def knee(scores_desc: np.ndarray) -> Optional[float]:
    """Kneedle-style knee of a descending curve: point farthest from the chord between its ends."""
    n = len(scores_desc)
    if n < 10:
        return None
    x = np.linspace(0, 1, n)
    y = (scores_desc - scores_desc[-1]) / max(scores_desc[0] - scores_desc[-1], EPS)
    gap = (1 - x) - y  # vertical gap to the chord from (0, 1) to (1, 0), proportional to distance
    return float(scores_desc[int(np.argmax(np.abs(gap)))])


def chapman(n_a: int, n_b: int, m: int) -> float:
    return (n_a + 1) * (n_b + 1) / (m + 1) - 1


@dataclass
class Thresholds:
    drop: float
    rel: float
    core: float
    source: str = "null"
    components: list[dict[str, float]] = field(default_factory=list)
    knee: Optional[float] = None

    def posterior(self, c: np.ndarray) -> np.ndarray:
        """P(relevant | c) from the fitted mixture; a step function around rel before a fit exists."""
        if not self.components:
            return np.where(c >= self.core, 0.95, np.where(c >= self.rel, 0.6, np.where(c >= self.drop, 0.3, 0.05)))
        x = _atanh(np.asarray(c, dtype=float))
        dens = [comp["w"] * np.exp(-0.5 * ((x - comp["mu"]) / comp["sd"]) ** 2) / comp["sd"] for comp in self.components]
        total = np.sum(dens, axis=0) + 1e-300
        return 1 - dens[0] / total  # component 0 is the background

    def segment(self, c: float) -> str:
        if c >= self.core:
            return "core"
        if c >= self.rel:
            return "candidate"
        if c >= self.drop:
            return "boundary"
        return "discard"


@dataclass
class Decision:
    thresholds: Thresholds
    give_up: dict[str, str]                  # seed -> rule that fired
    stop_directions: dict[str, list[str]]    # seed -> directions no longer expanded
    stop: Optional[str]                      # global stop rule, or None
    snapshot: dict[str, Any]


class Monitor:
    def __init__(self, cfg: dict[str, Any], null_scores: np.ndarray, seeds: dict[str, str]):
        self.cfg, self.seeds = cfg, seeds  # seeds: id -> display name
        self.null = np.sort(np.asarray(null_scores, dtype=float))
        if len(self.null) < 50:
            raise ValueError("null distribution needs at least 50 papers")
        q = lambda p: float(np.quantile(self.null, p))  # noqa: E731
        self.nq = {"q95": q(0.95), "q99": q(0.99), "q999": q(0.999), "mean": float(self.null.mean()),
                   "sd": float(self.null.std())}
        # discovery stream: (paper_id, seed, direction, back, fwd, score, first)
        self.events: list[tuple[str, str, str, int, int, float, bool]] = []
        self.scores: dict[str, float] = {}  # unique papers (not seeds) -> best cosine
        self.t = self._null_thresholds()
        self.history: list[dict[str, Any]] = []

    # -- input ------------------------------------------------------------------------------
    def observe(self, paper_id: str, seed: str, direction: str, back: int, fwd: int, score: float, first: bool) -> None:
        self.events.append((paper_id, seed, direction, back, fwd, float(score), first))
        if first:
            self.scores[paper_id] = float(score)

    # -- thresholds (M1, M2, M3) ------------------------------------------------------------------
    def _null_thresholds(self) -> Thresholds:
        return Thresholds(drop=self.nq["q95"], rel=self.nq["q99"], core=max(self.nq["q999"], self.nq["q99"] + 0.02))

    def fit(self) -> Thresholds:
        m = self.cfg
        obs = np.array(list(self.scores.values()), dtype=float)
        if len(obs) < m.get("min_fit", 60):
            t = self._null_thresholds()
        else:
            t = self._mixture(obs) or self._null_thresholds()
        t.knee = knee(np.sort(obs)[::-1]) if len(obs) else None
        self.t = t
        return t

    def _mixture(self, obs: np.ndarray) -> Optional[Thresholds]:
        x = _atanh(obs).reshape(-1, 1)
        null_mu = float(_atanh(self.null).mean())
        best: Optional[GaussianMixture] = None
        for k in self.cfg.get("components", [2, 3]):
            init = np.array([null_mu] + list(np.quantile(x, np.linspace(0.6, 0.95, k - 1)))).reshape(-1, 1)
            try:
                g = GaussianMixture(k, means_init=init, random_state=0, max_iter=300).fit(x)
            except ValueError:
                continue
            if best is None or g.bic(x) < best.bic(x):
                best = g
        if best is None:
            return None
        order = np.argsort(best.means_.ravel())
        comps = [{"w": float(best.weights_[i]), "mu": float(best.means_[i, 0]),
                  "sd": float(math.sqrt(best.covariances_.ravel()[i]))} for i in order]
        # A usable fit has a background component near the null and some relevant mass above it.
        if math.tanh(comps[0]["mu"]) > self.nq["q99"] or sum(c["w"] for c in comps[1:]) < 0.02:
            return None
        t = Thresholds(0, 0, 0, source=f"mixture-{len(comps)}", components=comps)
        grid = np.linspace(min(obs.min(), self.null.min()), max(obs.max(), self.nq["q999"]), 2000)
        post = t.posterior(grid)
        p = self.cfg.get("posterior", {})

        def cross(level: float) -> float:
            idx = np.where(post >= level)[0]
            return float(grid[idx[0]]) if len(idx) else float(grid[-1])

        floor = float(np.quantile(self.null, self.cfg.get("null_floor_quantile", 0.95)))
        t.rel = max(cross(p.get("rel", 0.5)), floor)
        t.drop = min(cross(p.get("drop", 0.1)), t.rel)
        t.core = max(cross(p.get("core", 0.9)), t.rel)
        return t

    # -- per-seed signals (M4, M5) and global ones (M7, M8) ------------------------------------
    def _seed_stats(self, seed: str) -> dict[str, Any]:
        y = self.cfg.get("yield", {})
        w, alpha, eps = y.get("window", 100), y.get("ewma_alpha", 0.3), y.get("epsilon", 0.02)
        firsts = [(e[2], e[3], e[4], e[5]) for e in self.events if e[1] == seed and e[6]]
        rel = [s >= self.t.rel for *_, s in firsts]
        windows = [float(np.mean(rel[i:i + w])) for i in range(0, len(rel) - len(rel) % w, w)]
        ewma, series, low = None, [], 0
        for v in windows:
            ewma = v if ewma is None else alpha * v + (1 - alpha) * ewma
            series.append(round(ewma, 4))
            low = low + 1 if ewma < eps else 0
        levels = {}
        for d, b, f, s in firsts:
            depth = b if d == "refs" else f
            levels.setdefault((d, depth), []).append(s)
        dmin = self.cfg.get("drift", {}).get("min_level_size", 30)
        lv = [{"direction": d, "depth": depth, "n": len(v), "auc": round(auc(np.array(v), self.null), 4)}
              for (d, depth), v in sorted(levels.items()) if len(v) >= dmin]
        return {"processed": len(firsts), "relevant": int(sum(rel)), "yield": series, "low_windows": low,
                "levels": lv, "cumulative": np.cumsum(rel)[::max(1, len(rel) // 200)].tolist() if rel else []}

    def _recapture(self) -> dict[str, Any]:
        sets: dict[str, set[str]] = {s: set() for s in self.seeds}
        for pid, seed, *_ in self.events:
            if self.scores.get(pid, -1) >= self.t.rel and seed in sets:
                sets[seed].add(pid)
        union = set().union(*sets.values()) if sets else set()
        ests = []
        names = list(sets)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = sets[names[i]], sets[names[j]]
                if a and b:
                    ests.append(chapman(len(a), len(b), len(a & b)))
        n_hat = float(np.median(ests)) if ests else None
        recall = min(1.0, len(union) / n_hat) if n_hat else None
        w = self.cfg.get("yield", {}).get("window", 100)
        recent = [e for e in self.events if self.scores.get(e[0], -1) >= self.t.rel][-w:]
        redisc = (sum(1 for e in recent if not e[6]) / len(recent)) if len(recent) >= 20 else None
        return {"relevant_found": len(union), "n_hat": n_hat, "recall_est": recall, "rediscovery": redisc}

    # -- decisions -----------------------------------------------------------------------------
    def decide(self, frontier_max: dict[str, Optional[float]], active: set[str]) -> Decision:
        t = self.fit()
        y, dr, rc = self.cfg.get("yield", {}), self.cfg.get("drift", {}), self.cfg.get("recapture", {})
        give_up: dict[str, str] = {}
        stop_dirs: dict[str, list[str]] = {}
        seeds_out = {}
        for seed in self.seeds:
            st = self._seed_stats(seed)
            fmax = frontier_max.get(seed)
            if seed in active:
                if fmax is None or fmax < t.rel:
                    give_up[seed] = "M6 frontier exhausted (best queued paper below tau_rel)"
                elif st["processed"] >= y.get("min_processed", 200) and st["low_windows"] >= y.get("windows", 3):
                    give_up[seed] = f"M4 yield below {y.get('epsilon', 0.02):.0%} for {st['low_windows']} windows"
                dirs = []
                for d in ("refs", "cits"):
                    lv = [l for l in st["levels"] if l["direction"] == d]
                    if lv and lv[-1]["auc"] < dr.get("auc_min", 0.6):
                        dirs.append(d)
                if dirs:
                    stop_dirs[seed] = dirs
                    if len(dirs) == 2 and seed not in give_up:
                        give_up[seed] = "M5 deepest levels in both directions look like the null"
            seeds_out[seed] = {"name": self.seeds[seed], "active": seed in active and seed not in give_up,
                               "frontier_max": fmax, **st}
        rec = self._recapture()
        stop = None
        still = active - set(give_up)
        if not still:
            stop = "all seeds given up"
        elif (rec["recall_est"] is not None and rec["recall_est"] >= rc.get("target_recall", 0.95)
              and (rec["rediscovery"] or 0) >= rc.get("min_rediscovery", 0.8)):
            stop = f"M7 estimated recall {rec['recall_est']:.2f} with rediscovery {rec['rediscovery']:.2f}"
        snap = self.snapshot(t, seeds_out, rec)
        return Decision(t, give_up, stop_dirs, stop, snap)

    # -- dashboard ----------------------------------------------------------------------------
    def snapshot(self, t: Thresholds, seeds: dict[str, Any], rec: dict[str, Any]) -> dict[str, Any]:
        obs = np.array(list(self.scores.values()), dtype=float)
        lo = float(min(self.null.min(), obs.min() if len(obs) else 1.0))
        hi = float(max(self.null.max(), obs.max() if len(obs) else 0.0))
        edges = np.linspace(lo, hi, 51)
        h_obs = np.histogram(obs, edges)[0].tolist() if len(obs) else [0] * 50
        h_null = np.histogram(self.null, edges)[0].tolist()
        grid = np.linspace(lo, hi, 200)
        curves = []
        if t.components:
            x = _atanh(grid)
            for comp in t.components:
                dens = comp["w"] * np.exp(-0.5 * ((x - comp["mu"]) / comp["sd"]) ** 2) / (comp["sd"] * math.sqrt(2 * math.pi))
                curves.append((dens / np.clip(1 - grid ** 2, 1e-4, None)).round(5).tolist())
        self.history.append({"papers": len(obs), "rel": t.rel, "core": t.core, "relevant": rec["relevant_found"],
                             "recall_est": rec["recall_est"], "rediscovery": rec["rediscovery"]})
        return {
            "thresholds": {"drop": t.drop, "rel": t.rel, "core": t.core, "source": t.source, "knee": t.knee},
            "null": self.nq, "papers": len(obs),
            "segments": {s: int(sum(1 for c in obs if t.segment(c) == s)) for s in ("core", "candidate", "boundary", "discard")},
            "hist": {"edges": edges.round(4).tolist(), "observed": h_obs, "null": h_null},
            "mixture": {"grid": grid.round(4).tolist(), "components": curves},
            "seeds": seeds, "recapture": rec, "history": self.history[-500:],
        }

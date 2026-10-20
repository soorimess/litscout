"""Offline end-to-end run on a synthetic citation graph, with fake sources and a fake LLM."""
import copy
import re
from pathlib import Path

import numpy as np
import yaml

from litscout.cli import _pipeline
from litscout.config import ROOT, Settings
from litscout.llm import Budget
from litscout.store import Store

DIM = 48
rng = np.random.default_rng(7)
U = rng.normal(size=DIM)          # topic direction
V = rng.normal(size=DIM)          # neighbouring field


def unit(v):
    return v / np.linalg.norm(v)


class World:
    """REL papers sit near U; NOISE near V. HIDDEN relevant papers score low but lead to more relevant ones."""

    def __init__(self):
        self.papers, self.vec, self.refs = {}, {}, {}
        def add(pid, title, v):
            self.papers[pid] = {"openalex": pid, "doi": None, "arxiv": None, "title": title,
                                "abstract": f"abstract of {title}", "year": 2025, "venue": "X", "cited_by": 1,
                                "subfield_id": "1712", "subfield": "Software", "oa_pdf": None}
            self.vec[title] = unit(v)
            self.refs[pid] = []
        for i in range(60):
            add(f"W{i}", f"REL paper {i}", U + 0.35 * rng.normal(size=DIM))
        for i in range(8):  # relevant, but phrased so that cosine misses them
            add(f"WH{i}", f"REL hidden {i}", 0.45 * U + V + 0.3 * rng.normal(size=DIM))
        for i in range(25):  # reachable only through the hidden papers
            add(f"WB{i}", f"REL behind {i}", U + 0.35 * rng.normal(size=DIM))
        for i in range(400):
            add(f"N{i}", f"NOISE paper {i}", V + 0.6 * rng.normal(size=DIM))
        for i in range(5):  # only keyword search finds these
            add(f"WS{i}", f"REL searchable {i}", U + 0.3 * rng.normal(size=DIM))
        rel = [f"W{i}" for i in range(60)]
        noise = [f"N{i}" for i in range(400)]
        for pid in rel:
            self.refs[pid] = list(rng.choice(rel, 4, replace=False)) + list(rng.choice(noise, 6, replace=False))
        for i in range(8):
            self.refs[f"W{i}"].append(f"WH{i}")
            self.refs[f"WH{i}"] = [f"WB{j}" for j in range(i * 3, min(25, i * 3 + 4))] + list(rng.choice(noise, 3, replace=False))
        for pid in noise:
            self.refs[pid] = list(rng.choice(noise, 5, replace=False))
        self.cits = {pid: [] for pid in self.papers}
        for a, lst in self.refs.items():
            for b in lst:
                self.cits[b].append(a)

    def rec(self, pid):
        return copy.deepcopy(self.papers[pid])


class FakeOpenAlex:
    def __init__(self, w): self.w = w
    def references(self, rec, cap): return [self.w.rec(p) for p in self.w.refs.get(rec["openalex"], [])][:cap]
    def citations(self, pid, cap): return [self.w.rec(p) for p in self.w.cits.get(pid, [])][:cap]
    def get_by_dois(self, dois): return []
    def search(self, q, n): return [self.w.rec(f"WS{i}") for i in range(5)] + [self.w.rec("N1")]
    def sample(self, n, seed, subfield):
        out = []
        for i in range(n):
            t = f"RANDOM {i}"
            self.w.vec[t] = unit(rng.normal(size=DIM))
            out.append({"id": f"R{i}", "openalex": f"R{i}", "title": t, "abstract": "x"})
        return out


class Empty:
    def __getattr__(self, name): return lambda *a, **k: []


class FakeSources:
    def __init__(self, w):
        self.openalex, self.arxiv, self.crossref, self.dblp, self.s2 = FakeOpenAlex(w), Empty(), Empty(), Empty(), Empty()


class FakeLLM:
    def __init__(self, s, w):
        self.s, self.w = s, w
        self.budget = Budget(100, {"chat_input": 0.2, "chat_output": 1.2, "embedding": 0.01})
        self.calls = {"judge": 0, "search": 0, "criteria": 0}

    def chat_json(self, system, user):
        if system.startswith("You write the inclusion"):
            self.calls["criteria"] += 1
            return {"definition": "Synthetic topic", "include": ["is REL"], "exclude": ["is NOISE"]}
        if system.startswith("You write keyword queries"):
            self.calls["search"] += 1
            return {"queries": [f"query {self.calls['search']}"]}
        self.calls["judge"] += 1
        title = re.search(r"Title: (.*)", user).group(1)
        rel = title.startswith("REL")
        return {"score": 9 if rel and "hidden" not in title else (7 if rel else 1), "relevant": rel, "reason": "synthetic"}

    def embed(self, texts):
        out = []
        for t in texts:
            if t.startswith("Instruct:"):
                out.append(unit(U))
            else:
                out.append(self.w.vec.get(t.split("\n")[0], unit(rng.normal(size=DIM))))
        return np.array(out, dtype=np.float32)


def test_pipeline_on_synthetic_graph(tmp_path):
    tuning = yaml.safe_load((ROOT / "config" / "default.yaml").read_text())
    tuning["monitor"]["refit_every"] = 40
    tuning["monitor"]["min_fit"] = 40
    tuning["crawl"]["workers"] = 2
    tuning["baseline"]["sample_size"] = 300
    s = Settings("http://x", "k", "m", "e", 10, None, None, None, 100000, 100, tuning=tuning, runs_dir=tmp_path)
    w = World()
    run_dir = tmp_path / "syn"
    store = Store(run_dir / "store.sqlite")
    store.set_meta("run", {"name": "syn", "mode": "topic", "topic": "synthetic topic", "n_seeds": 3})
    store.set_meta("topic", "synthetic topic")
    for pid in ("W0", "W1", "W2"):
        store.upsert_paper(w.rec(pid))
        store.x("INSERT INTO seeds VALUES (?, ?, 'test', 'test', 1)", (pid, pid))
    llm = FakeLLM(s, w)
    _pipeline(s, run_dir, store, llm=llm, src=FakeSources(w))

    assert (run_dir / "criteria.md").read_text().startswith("# Review criteria")
    assert (run_dir / "output" / "papers.xlsx").exists()
    final = {r["paper_id"] for r in store.q("SELECT paper_id FROM final WHERE include=1")}
    seeds = {r["id"] for r in store.q("SELECT id FROM seeds")}
    found = final | seeds
    behind = {f"WB{i}" for i in range(25)}
    assert len({f"W{i}" for i in range(60)} & found) >= 55
    assert len(behind & found) >= 24, "papers behind low-cosine relevant papers need LLM-confirmed expansion"
    assert {f"WS{i}" for i in range(5)} <= found, "keyword search finds papers outside the graph"
    assert not any(p.startswith("N") for p in final)
    assert store.q("SELECT COUNT(*) AS n FROM seeds WHERE origin LIKE 'promoted%'")[0]["n"] >= 1
    assert store.meta("search_rounds") >= 1
    assert store.meta("thresholds")["source"].startswith("llm")

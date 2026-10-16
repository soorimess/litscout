import numpy as np
from sklearn.metrics import roc_auc_score

from litscout.monitor import Monitor, auc, chapman, knee

CFG = {"min_fit": 60, "components": [2, 3], "posterior": {"core": 0.9, "rel": 0.5, "drop": 0.1},
       "null_floor_quantile": 0.95, "yield": {"window": 50, "ewma_alpha": 0.3, "epsilon": 0.02, "windows": 3,
       "min_processed": 100}, "drift": {"auc_min": 0.6, "min_level_size": 30},
       "recapture": {"target_recall": 0.95, "min_rediscovery": 0.8},
       "probe": {"per_fit": 40, "max_labels": 400, "min_labels": 40, "min_each": 5, "bins": 8, "C": 10.0}}
rng = np.random.default_rng(0)


def null():
    return np.clip(rng.normal(0.30, 0.05, 2000), -1, 1)


def test_auc_matches_sklearn():
    pos, neg = rng.normal(1, 1, 300), rng.normal(0, 1, 400)
    y = np.r_[np.ones(300), np.zeros(400)]
    assert abs(auc(pos, neg) - roc_auc_score(y, np.r_[pos, neg])) < 1e-9


def test_chapman():
    assert chapman(100, 100, 100) == 100.0
    assert chapman(50, 50, 10) > 200


def test_knee_finds_drop():
    curve = np.r_[np.linspace(0.9, 0.7, 50), np.linspace(0.45, 0.4, 500)]
    k = knee(curve)
    assert 0.4 <= k <= 0.7


def test_null_thresholds_before_fit():
    m = Monitor(CFG, null(), {"s1": "seed"})
    t = m.fit()
    assert t.source == "null" and t.drop <= t.rel <= t.core


def test_mixture_separates_relevant_from_background():
    m = Monitor(CFG, null(), {"s1": "seed"})
    obs = np.r_[rng.normal(0.32, 0.05, 1400), rng.normal(0.62, 0.05, 600)]
    for i, c in enumerate(obs):
        m.observe(f"p{i}", "s1", "refs", 1, 0, float(c), True)
    t = m.fit()
    assert t.source.startswith("mixture")
    assert 0.40 < t.rel < 0.58, t.rel
    assert t.core >= t.rel >= t.drop
    post = t.posterior(np.array([0.3, 0.65]))
    assert post[0] < 0.1 and post[1] > 0.9


def test_give_up_when_frontier_exhausted_and_low_yield():
    m = Monitor(CFG, null(), {"s1": "a", "s2": "b"})
    for i in range(400):  # s2 finds only background papers
        m.observe(f"q{i}", "s2", "cits", 0, 1 + i // 100, float(rng.normal(0.3, 0.05)), True)
    for i in range(200):
        m.observe(f"r{i}", "s1", "refs", 1, 0, float(rng.normal(0.65, 0.04)), True)
    d = m.decide({"s1": 0.7, "s2": 0.2}, {"s1", "s2"})
    assert "s2" in d.give_up and "s1" not in d.give_up
    assert d.stop is None


def test_recapture_stop_waits_for_queued_relevant_papers():
    m = Monitor(CFG, null(), {"s1": "a", "s2": "b"})
    scores = rng.normal(0.65, 0.03, 200)
    for s in ("s1", "s2"):  # s2 re-finds every paper s1 found: estimated recall 1.0, rediscovery 1.0
        for i, c in enumerate(scores):
            m.observe(f"r{i}", s, "refs", 1, 0, float(c), s == "s1")
    queued = {f"q{i}": 0.66 for i in range(30)}
    assert m.decide({"s1": 0.66, "s2": 0.66}, {"s1", "s2"}, queued).stop is None
    assert m.decide({"s1": 0.66, "s2": 0.66}, {"s1", "s2"}, {}).stop.startswith("M7")


def test_llm_calibration_overrides_a_degenerate_mixture():
    # The whole crawl sits far above the null (a citation neighbourhood), relevance starts near 0.70.
    m = Monitor(CFG, null(), {"s1": "seed"})
    obs = np.clip(rng.normal(0.58, 0.08, 1500), -1, 1)
    for i, c in enumerate(obs):
        m.observe(f"p{i}", "s1", "refs", 1, 0, float(c), True)
    probes = m.probe_candidates(120)
    assert len(probes) == len(set(probes)) and 60 <= len(probes) <= 120
    for pid in probes:
        m.label(pid, m.scores[pid] >= 0.70)
    t = m.fit()
    assert t.source.startswith("llm-calibrated"), t.source
    assert 0.66 < t.rel < 0.74, t.rel
    assert t.drop <= t.rel <= t.core
    assert not set(m.probe_candidates(40)) & set(probes)


def test_no_calibration_when_labels_are_one_sided():
    m = Monitor(CFG, null(), {"s1": "seed"})
    for i, c in enumerate(rng.normal(0.58, 0.08, 500)):
        m.observe(f"p{i}", "s1", "refs", 1, 0, float(c), True)
    for pid in m.probe_candidates(60):
        m.label(pid, False)
    assert not m.fit().source.startswith("llm")

# Live similarity distribution: methods for finding the relevant segment and stopping

During snowballing, every discovered paper is embedded (title + abstract) and compared with the seed
papers. This document describes how to read that stream of cosine similarities while the crawl runs:
which papers are useless, which are the most related, and when to give up on a seed.

## Notation

- Seeds `s_1 … s_k`, embeddings `e(·)`, all L2-normalised.
- Per-seed similarity of a paper `p`: `c_i(p) = cos(e(p), e(s_i))`.
- Paper score: `c(p) = 0.7 · cos(e(p), centroid) + 0.3 · cos(e(p), e(topic))`, where the centroid is the
  normalised mean of the seed embeddings and `e(topic)` embeds the topic as an instructed query. The
  paper is attributed to its nearest seed `i*(p) = argmax_i c_i(p)`.
  On the first end-to-end run's 795 LLM labels, the AUC was 0.878 for the earlier `max_i c_i(p)`, 0.907
  for the centroid alone and 0.911 for this mix. The random baseline is scored the same way, and both
  are rescored when a seed is promoted.
- Every discovered paper also records the seed whose crawl found it, the direction (backward through
  references or forward through citations), and its depth.

Raw cosine values are not comparable across embedding models. Many models place unrelated texts at a
cosine of 0.2–0.5 (anisotropy), so no fixed threshold such as 0.6 is meaningful. Every method below
therefore measures similarity **relative to a background distribution** estimated for the current run.

## Why a random baseline, if the crawl produces off-topic papers anyway?

The crawl's own scores do eventually split into an off-topic group and a related group (M2). But
relying on that alone fails in three situations:

1. **Early in the run**, the first papers are the seeds' own references and are almost all on topic.
   The histogram has only the related group, so any cut would split relevant papers in two.
2. **Gradual drift** produces one long slope instead of two groups with a gap. Neither a mixture fit
   nor a knee then finds a clear cut.
3. **Model scale:** some embedding models give unrelated papers 0.4–0.5. Without a reference, a
   score of 0.5 can't be read as "related" or "noise".

The random baseline shows from the start where unrelated papers from the same field score. It costs
one OpenAlex call and about 2,000 abstract embeddings (roughly one cent).

## M1. Background (null) distribution

Embed a random sample of papers and score them against the seeds. This gives the distribution of `c`
for papers unrelated to the topic, `F0`.

- Source: OpenAlex `GET /works?sample=2000&seed=<n>&filter=primary_topic.subfield.id:<id>`, where the
  subfield is the most common one among the seeds. This is the **field null**: papers from the same
  field but on random topics. It is the default, because it separates "on topic" from merely "in the
  same field". A global sample (no filter) can be added as a diagnostic; against it, almost any paper
  from the seeds' field looks relevant.
- From it: mean `μ0`, standard deviation `σ0` and quantiles `q0.95`, `q0.99`.
- Per paper: `z(p) = (c(p) − μ0) / σ0` and an empirical p-value `1 − F0(c(p))`.
- A paper with `c(p) ≤ q0.95` of the field null is indistinguishable from a random paper in the field:
  **useless**.
- With p-values for all candidates, Benjamini–Hochberg gives a relevant set with a controlled share
  of false discoveries.

Optional: subtract the null sample's mean embedding from every vector and renormalise. This removes the
shared direction that causes anisotropy and widens the gap between relevant and unrelated papers.

## M2. Mixture model on the observed similarities

The similarities of discovered papers are a mixture: a background component (citations that left the
topic) and one or two relevant components (core, peripheral).

- Fit a 2- or 3-component Gaussian mixture to Fisher-transformed scores `atanh(c)`, or a Beta mixture
  to `(c + 1) / 2`. Choose 2 or 3 components by BIC.
- Initialise the background component from M1, so it cannot drift onto the relevant mass.
- Refit every `K` new papers (for example 200). A one-dimensional fit takes milliseconds.
- The posterior `P(relevant | c)` gives the thresholds used everywhere else:
  - `τ_core`: posterior ≥ 0.9
  - `τ_rel`: posterior = 0.5
  - `τ_drop`: posterior ≤ 0.1
- Segments: **core** (≥ 0.9), **candidate** (0.5–0.9), **boundary** (0.1–0.5), **discard** (< 0.1).

## M3. Knee of the ranked curve

Sort all scores in descending order and find the knee with the Kneedle method: the point farthest
from the straight line joining the first and last points. Above the knee is the dense core; below it
is the long tail. This is a model-free cross-check of `τ_rel` from M2. If the two disagree strongly,
the mixture fit is suspect and should be inspected.

## M4. Per-seed yield (the main "give up on a seed" signal)

For each seed, follow the stream of papers its own crawl discovers.

- Yield `y_i` is the fraction of the last `W` new papers (for example 100) with `P(relevant) ≥ 0.5`,
  smoothed with an exponentially weighted moving average.
- **Give up on seed `i`** when `y_i < ε` (for example 0.02) for `m` consecutive windows (for example 3).
- Cumulative-gain knee, borrowed from stopping rules in technology-assisted review (Cormack and
  Grossman's knee method):
  - Plot relevant papers found against papers processed for the seed.
  - Detect the knee and compute `ρ = slope before the knee / slope after it`.
  - Stop once `ρ` is above a threshold (for example 6) and a minimum number of papers has been
    processed.
- A seed that keeps producing near-zero yield from the start marks a poor seed choice and is
  reported, not only stopped.

## M5. Depth drift test

Each seed, direction and depth level gets its own distribution of scores. Compare each level with the
null `F0`:

- AUC of "level vs null": the probability that a random paper from the level scores higher than a
  random null paper.
- Or the Wasserstein distance between the level's distribution and `F0`.
- When a level is indistinguishable from the null (AUC < 0.6, or distance below `δ`), stop expanding
  that seed in that direction beyond that depth.
- Also stop when the share of the deepest level at or above `τ_rel` drops below `ε` (2%). Inside a
  citation neighbourhood every level is far from the null (AUC ≈ 0.98 in the first end-to-end run),
  so the AUC test alone never fires; the relevant rate does once `τ_rel` is calibrated (M9).

This catches topic drift: forward citations of a popular paper usually drift faster than its
references.

## M6. Best-first crawl and frontier gating

Order each seed's frontier as a priority queue by `c(p)`, instead of breadth-first.

- The crawl always expands the most similar unexpanded paper next, so relevant papers arrive early and
  the yield curve (M4) is meaningful.
- A paper is expanded only if `c(p) ≥ τ_rel`. Papers below it are stored and scored but not expanded.
- **Seed exhausted:** the highest priority left in its frontier is below `τ_rel`. Nothing promising
  remains, whatever the depth. This is the simplest and most robust stopping rule. M4 and M5 make
  it fire earlier when expansion keeps returning mostly noise.

## M7. Capture–recapture estimate of the total (global stop)

Treat each seed's crawl as an independent "capture" of the relevant population.

- Two seeds: `N̂ = (n_A + 1)(n_B + 1) / (m + 1) − 1` (Chapman's form of Lincoln–Petersen), where
  `n_A` and `n_B` are the relevant papers each found and `m` the overlap.
- More seeds: Schnabel's estimator, or a log-linear model over the capture histories.
- Estimated recall = relevant papers found / `N̂`. **Global stop** when it reaches the target (for
  example 0.95) and the relevant papers still queued for expansion are at most `1 − target` of those
  found, or every seed is exhausted. Without the queue check, overlapping seeds give `N̂` ≈ found and
  the crawl stopped with relevant papers still waiting (first end-to-end run).
- Only citation crawls count as captures: papers found by keyword search (direction `search`) are
  counted as found but not as captures, and seeds with fewer than 10 relevant captures are left out.
- Caveat: crawls from different seeds share the citation graph, so they are not independent. The
  overlap is inflated and `N̂` underestimates. Treat it as a signal, not a guarantee.

## M8. Rediscovery rate (saturation)

The share of relevant papers in a window that were already in the store, reached through another seed
or path. A value close to 1 means the relevant region is saturated.

## M9. LLM probe calibration of the thresholds

The null tells unrelated papers apart from the field, but not "on the topic" from "in the same
neighbourhood". In the first end-to-end test run, the crawled scores formed one
broad group around 0.58, far above the null (mean 0.28, q95 0.40). The mixture's background
component landed on the null side, so `τ_rel` fell to the null floor and 1,268 of 1,375 papers were
"core". An LLM check of 48 papers spread over the score range put the real boundary near 0.70:
papers from neighbouring areas (broad surveys, related but different tasks) scored 0.6–0.7 and were not relevant.

So the monitor asks the LLM directly, in small amounts:

- At each refit, the LLM judges up to 40 unlabeled papers spread evenly over the observed score range
  (8 bins), with the same prompt as the final check. At most 400 labels per run (about $0.12).
- Once there are at least 40 labels with at least 5 of each class, a logistic regression of
  P(relevant | atanh(c)) replaces the mixture. `τ_drop`, `τ_rel` and `τ_core` are the scores where it
  crosses 0.1, 0.5 and 0.9, and `τ_rel` never goes below the null's q95.
- If the slope is not positive (higher cosine is not more often relevant), the calibration is
  rejected and the mixture or null thresholds stay.
- The labels are kept in the `judgments` table and reused by the final check.

## Recommended default policy

| Decision | Rule |
|---|---|
| Thresholds | M9 LLM calibration once enough labels exist; before that the M2 mixture anchored by the M1 field null; refit every 200 papers; M3 knee as a cross-check |
| Expand a paper | `c(p) ≥ τ_rel`, best-first per seed (M6); every LLM-confirmed paper whatever its score, in both directions, even after its seed was given up; never an LLM-rejected paper below `τ_core` |
| LLM checks during the crawl | probes (M9); once calibrated, every paper `≥ τ_drop`; promotion of LLM 9–10 papers to seeds; keyword searches from confirmed papers |
| Give up on a seed | frontier maximum `< τ_rel` (M6), or EWMA yield `< 2%` for 3 windows of 100 (M4), or the deepest levels in both directions indistinguishable from the null or almost free of relevant papers (M5) |
| Global stop | all seeds given up, or estimated recall ≥ 0.95 (M7) with rediscovery ≥ 0.8 (M8) and almost no relevant papers queued, or a budget cap; confirmed papers still queued are expanded first |
| Output segments | core / candidate / boundary / discard from the posterior (M9, else M2) |

## Live dashboard

The monitor writes one metrics snapshot per refit. The dashboard renders:

1. Score histogram of all papers, with the null distribution overlaid and the fitted mixture
   components and thresholds drawn on top.
2. Per-seed small multiples: histogram by depth and direction, coloured by AUC against the null (M5).
3. Per-seed yield curves (EWMA) with the give-up threshold.
4. Cumulative relevant papers against papers processed, per seed, with the detected knee (M4).
5. Frontier maximum priority per seed against `τ_rel` (M6).
6. Estimated total `N̂`, estimated recall and rediscovery rate over time (M7, M8).
7. Decision log: every threshold change and every seed given up, with the rule that fired.

## Final check: are the ranking and the cut right?

After the crawl stops, an LLM judge reads the title and abstract of papers against the review criteria
(`criteria.md`).

- **Reuse every judgment from the crawl** (probes and band checks) and weight each judged paper by its
  segment's sampling rate.
- **Judge every paper at or above `τ_drop`** (core, candidate, boundary), at about $0.00017 per paper.
- **Top up a sample of the discard segment to 200 judged papers.** This estimates how many relevant
  papers the crawl left below `τ_drop`.
- **If the check confirms papers that were never expanded,** the crawl runs again (at most 3 passes).
- **Evaluate the ranking against the LLM labels:**
  - AUC of the score
  - Spearman correlation between the score and the LLM score
  - precision@k curve
  - per-seed AUC, to find seeds that pull in off-topic regions
- **Calibrate:** fit isotonic regression from cosine score to P(LLM says relevant), then choose the
  final cut for a target recall or the best F1.
- **Final order:** judged papers rank by 0.5 × calibrated probability + 0.5 × LLM score / 10, the
  others by the calibrated probability; the raw score breaks ties (isotonic calibration is a step
  function, which left the order inside a step arbitrary). A score AUC below 0.8 is reported as a weak
  ranking.

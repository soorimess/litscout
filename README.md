# LitScout

A systematic literature review system. It starts from seed papers (a folder of PDFs) or from a topic,
snowballs through the citation graph in both directions, ranks every discovered paper by embedding
similarity to the seeds and the topic, decides live when a seed stops yielding relevant papers, and
has an LLM screen the uncertain papers against written review criteria.

## How it works

1. **Seeds**
   - PDF folder: each PDF's DOI, arXiv id or title is resolved on OpenAlex. Papers that can't be
     resolved still anchor the similarity but can't be expanded.
   - Topic: the LLM turns the topic into search queries; OpenAlex, arXiv and Crossref are searched;
     the pool is ranked by embedding similarity to the topic and cut to `SEED_POOL_MAX`; the LLM picks
     the N most relevant papers from their titles and abstracts.
2. **Review criteria:** the LLM drafts a definition and inclusion/exclusion criteria from the topic
   and the seeds, written to `runs/<run>/criteria.md`. Edit them (or pass `--criteria FILE`); every
   LLM judgment uses them.
3. **Random baseline:** about 2,000 random papers from the seeds' OpenAlex subfield are embedded and
   scored. They show what an unrelated paper in the same field scores.
4. **Snowball:** each seed has its own priority frontier and expands its best-scored paper next,
   backward (references) and forward (citations). OpenAlex is the main graph source; Semantic Scholar
   fills in for recent preprints. Every paper is embedded (title + abstract) and scored as
   0.7 × similarity to the seed centroid + 0.3 × similarity to the topic.
5. **Live monitor and LLM steps,** every 200 new papers:
   - the LLM judges a few papers spread over the score range, and a logistic fit of those labels sets
     the thresholds;
   - it then judges every paper above the lowest threshold; confirmed papers are always expanded,
     rejected ones are not;
   - papers the LLM rates 9–10 become new seeds;
   - keyword searches written from the confirmed papers add recent papers the citation graph misses;
   - per-seed yield, depth drift, frontier exhaustion and capture–recapture decide which seeds to give
     up and when to stop. Details: [docs/distribution-analysis.md](docs/distribution-analysis.md).
6. **Final check:** the LLM judges the remaining papers above the lowest threshold and a sample below
   it. The run reports how well the score ranking agrees with the LLM (AUC, Spearman, precision@k),
   calibrates the score to P(relevant), and chooses the cut for the target recall. Confirmed papers
   that were never expanded trigger another crawl pass (at most 3).
7. **Export:** `runs/<run>/output/papers.xlsx` and `papers.csv`, with the final list, all scored
   papers, seeds and run summary.

Design documents:

- [docs/system-design.md](docs/system-design.md)
- [docs/sequence-diagrams.md](docs/sequence-diagrams.md)
- [docs/distribution-analysis.md](docs/distribution-analysis.md)

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # fill in the provider URL and key, models, optional OpenAlex key
pytest -q                 # unit tests for the monitor math and record handling
```

| Variable | Purpose |
|---|---|
| `LLM_BASE_URL`, `LLM_API_KEY` | OpenAI-compatible provider for chat and embeddings |
| `LLM_MODEL` | chat model (query expansion, seed picking, validation) |
| `EMBEDDING_MODEL` | embedding model, default `qwen/qwen3-embedding-8b` |
| `SEED_POOL_MAX` | topic mode: candidate pool size before the LLM picks seeds |
| `OPENALEX_API_KEY`, `S2_API_KEY` | optional: own quotas for OpenAlex and Semantic Scholar |
| `GITHUB_TOKEN` | optional: faster GitHub repository search, with READMEs (grey literature) |
| `CONTACT_EMAIL` | polite-pool contact for OpenAlex and Crossref |
| `MAX_PAPERS`, `MAX_COST_USD` | budget caps per run |
| `DASHBOARD_PASSWORD` | optional dashboard password (user name `review`) |

Tuning (thresholds, window sizes, null sample size, pricing) is in `config/default.yaml`.

## Usage

```bash
litscout run --topic "machine learning for weather forecasting" --seeds 5   # topic mode
litscout run --seeds-dir ./my-seed-pdfs                         # PDF mode
litscout run --topic "..." --review-criteria                    # stop to edit criteria.md, then resume
litscout criteria <run>         # show the review criteria and where to edit them
litscout dashboard <run>        # live dashboard at http://localhost:8765
litscout dashboard <run> --host 0.0.0.0   # reachable from other machines (serves only the page and metrics.json)
litscout status <run>           # thresholds, counts, decisions, cost
litscout resume <run>           # continue an interrupted run
litscout resume <run> --recrawl # continue crawling after a stop (e.g. a raised cap)
litscout validate <run>         # re-run the LLM check
litscout report <run>           # re-export the tables
```

Each run lives in `runs/<run>/`: `store.sqlite` (all state, so runs resume), `metrics.json`
(dashboard data), `index.html` (dashboard) and `output/`.

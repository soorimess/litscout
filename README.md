# LitScout

A systematic literature review system. It starts from seed papers (a folder of PDFs) or from a topic,
snowballs through the citation graph in both directions, ranks every discovered paper by embedding
similarity to the seeds, decides live when a seed stops yielding relevant papers, and confirms the
most related segment with an LLM.

## How it works

1. **Seeds**
   - PDF folder: each PDF's DOI, arXiv id or title is resolved on OpenAlex. Papers that can't be
     resolved still anchor the similarity but can't be expanded.
   - Topic: the LLM turns the topic into search queries; OpenAlex, arXiv, Crossref and DBLP are
     searched; the pool is ranked by embedding similarity to the topic and cut to `SEED_POOL_MAX`;
     the LLM picks the N most relevant papers from their titles and abstracts.
2. **Random baseline:** about 2,000 random papers from the seeds' OpenAlex subfield are embedded and
   scored against the seeds. They show what an unrelated paper in the same field scores.
3. **Snowball:** each seed has its own priority frontier and always expands its most similar paper
   next, backward (references) and forward (citations). OpenAlex is the main graph source; Semantic
   Scholar fills in where OpenAlex has no references or citations (common for recent preprints).
   Every discovered paper is embedded (title + abstract) and scored against every seed.
4. **Live monitor:** periodically refits thresholds and decides which seeds or directions to give up
   and when to stop. At each refit the LLM judges a few papers spread over the score range, and a
   logistic fit of those labels sets the thresholds (before enough labels exist, a mixture model
   against the random baseline does). It also tracks per-seed yield,
   drift by depth, frontier exhaustion, and capture–recapture recall. It writes `metrics.json` for
   the dashboard. Details: [docs/distribution-analysis.md](docs/distribution-analysis.md).
5. **LLM validation:** the LLM judges every core and candidate paper, plus a stratified sample below
   them. The run reports how well the cosine ranking agrees with the LLM (AUC, Spearman,
   precision@k), calibrates cosine to P(relevant), and chooses the cut for the target recall.
6. **Export:** `runs/<run>/output/papers.xlsx` and `papers.csv`, with the final list, all scored
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
| `CONTACT_EMAIL` | polite-pool contact for OpenAlex and Crossref |
| `MAX_PAPERS`, `MAX_COST_USD` | budget caps per run |

Tuning (thresholds, window sizes, null sample size, pricing) is in `config/default.yaml`.

## Usage

```bash
litscout run --topic "machine learning for weather forecasting" --seeds 5   # topic mode
litscout run --seeds-dir ./my-seed-pdfs                         # PDF mode
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

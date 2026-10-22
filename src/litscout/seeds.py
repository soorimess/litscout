"""Seed selection: import a folder of PDFs, or find seeds for a topic (search pool + LLM picker)."""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

import pymupdf
import numpy as np

from .llm import LLM, embed_text
from .sources import Sources
from .sources.http import SourceUnavailable
from .sources.records import ARXIV_RE, canonical_id, dedupe, merge, titles_match
from .store import Store

log = logging.getLogger(__name__)
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>,;]+)", re.I)

QUERY_SYSTEM = """You turn a research topic into search queries for scholarly search engines
(OpenAlex, arXiv, Crossref, DBLP). Return a JSON object {{"queries": [...]}} with {k} short keyword
queries (3 to 8 words each) that cover the topic's main phrasings, synonyms and sub-aspects.
Do not use boolean operators or quotes."""

PICK_SYSTEM = """You select seed papers for a systematic literature review.

Topic: {topic}

You get a numbered list of candidate papers with title, year and abstract. Pick exactly {n} papers that
are the MOST relevant to the topic. Judge relevance only from the topic and each paper's title and
abstract: prefer papers whose main subject is the topic itself over papers that only mention it or use
it as a tool. Return a JSON object
{{"seeds": [{{"index": <candidate number>, "reason": "<one sentence on why it is relevant>"}}]}}
ordered from most to least relevant, with exactly {n} entries and no duplicates."""

TOPIC_FROM_SEEDS = """You read the titles and abstracts of the seed papers of a systematic literature
review and name the review's topic: the research area and the kind of contribution the seeds make, in
one or two sentences of at most 40 words (like a review title plus its scope). Describe the field, not
the seeds: do not mention their particular methods, datasets, settings, metrics or findings, because the
review must also include papers that make the same kind of contribution in other ways.
Return a JSON object {"topic": "..."}."""


def _seed_name(rec: dict[str, Any]) -> str:
    words = re.findall(r"[\w\-]+", rec.get("title") or "seed")
    return " ".join(words[:6])


def _store_seed(store: Store, rec: dict[str, Any], origin: str, reason: str) -> str:
    pid, _ = store.upsert_paper(rec)
    store.x("INSERT OR REPLACE INTO seeds VALUES (?, ?, ?, ?, ?)",
            (pid, _seed_name(rec), origin, reason, int(bool(rec.get("openalex") or rec.get("doi") or rec.get("arxiv")))))
    return pid


# -- PDF folder -------------------------------------------------------------------------------
def _pdf_fields(path: Path) -> dict[str, Any]:
    with pymupdf.open(path) as doc:
        text = "\n".join(doc[i].get_text() for i in range(min(2, doc.page_count)))
        meta_title = (doc.metadata or {}).get("title") or ""
        title = meta_title.strip() if len(meta_title.strip()) > 10 else ""
        if not title and doc.page_count:
            spans = [(s["size"], s["text"].strip()) for b in doc[0].get_text("dict")["blocks"]
                     for line in b.get("lines", []) for s in line["spans"] if len(s["text"].strip()) > 3]
            if spans:
                top = max(sz for sz, _ in spans)
                title = " ".join(t for sz, t in spans if sz >= top - 0.5)
    doi = DOI_RE.search(text)
    arxiv = ARXIV_RE.search(text) or ARXIV_RE.search(path.name)
    abstract = None
    m = re.search(r"abstract[\s.:\-—]*(.{200,3000}?)(?:\n\s*(?:1\.?\s*)?introduction|\n\s*keywords|\n\s*index terms)",
                  text, re.I | re.S)
    if m:
        abstract = " ".join(m.group(1).split())
    return {"title": " ".join(title.split()), "doi": doi.group(1).rstrip(".").lower() if doi else None,
            "arxiv": arxiv.group(1) if arxiv else None, "abstract": abstract}


def import_pdf_folder(folder: Path, store: Store, src: Sources) -> list[str]:
    pdfs = sorted(folder.glob("*.pdf"))
    if not pdfs:
        raise SystemExit(f"No PDF files in {folder}")
    ids = []
    for path in pdfs:
        local = _pdf_fields(path)
        rec: Optional[dict[str, Any]] = None
        try:
            if local["doi"]:
                rec = src.openalex.get(f"doi:{local['doi']}")
            if rec is None and local["arxiv"]:
                rec = src.openalex.get(local["arxiv"])
            if rec is None and local["title"]:
                rec = next((r for r in src.openalex.search(local["title"], 5) if titles_match(r["title"], local["title"])), None)
        except SourceUnavailable as e:
            log.warning("could not resolve %s on OpenAlex: %s", path.name, e)
        if rec:
            merge(rec, {"abstract": local["abstract"]})
            origin = f"pdf:{path.name}"
        else:
            rec = {**local, "id": None}
            rec["id"] = canonical_id(rec)
            origin = f"pdf:{path.name} (not found online; anchors similarity only)"
            log.warning("%s not found online; used as a local seed that cannot be expanded", path.name)
        ids.append(_store_seed(store, rec, origin, "given by the user"))
        log.info("seed %-60s %s", (rec.get("title") or path.name)[:60], "expandable" if rec.get("openalex") else "local")
    return ids


def topic_from_seeds(store: Store, llm: LLM) -> str:
    rows = store.q("SELECT p.title, p.abstract FROM seeds s JOIN papers p ON p.id = s.id")
    text = "\n\n".join(f"Title: {r['title']}\nAbstract: {(r['abstract'] or '')[:1500]}" for r in rows)
    return llm.chat_json(TOPIC_FROM_SEEDS, text).get("topic", "")


# -- topic mode -------------------------------------------------------------------------------
def _search_all(src: Sources, queries: list[str], per_query: dict[str, int]) -> list[dict[str, Any]]:
    fns = {name: getattr(src, name).search for name, n in per_query.items() if n}  # openalex, arxiv, crossref, dblp, github
    out: list[dict[str, Any]] = []
    with ThreadPoolExecutor(8) as pool:
        futs = {pool.submit(fns[name], q, n): (name, q) for q in queries for name, n in per_query.items() if n}
        for fut in as_completed(futs):
            name, q = futs[fut]
            try:
                hits = fut.result()
                out.extend(hits)
                log.info("search %-8s %-50s %3d hits", name, q[:50], len(hits))
            except SourceUnavailable as e:
                log.warning("search %s failed for %r: %s", name, q, e)
    return out


def _resolve(src: Sources, recs: list[dict[str, Any]]) -> None:
    """Attach OpenAlex ids, abstracts and reference lists, in batches where possible."""
    dois = [r["doi"] for r in recs if r.get("doi") and not r.get("openalex")]
    try:
        by_doi = {r["doi"]: r for r in src.openalex.get_by_dois(dois)}
    except SourceUnavailable:
        by_doi = {}
    for r in recs:
        if r.get("doi") in by_doi:
            merge(r, by_doi[r["doi"]])
    need_abs = [r["arxiv"] for r in recs if r.get("arxiv") and not r.get("abstract")]
    if need_abs:
        by_ax = {a["arxiv"]: a for a in src.arxiv.get_many(need_abs)}
        for r in recs:
            if r.get("arxiv") in by_ax:
                merge(r, by_ax[r["arxiv"]])
    for r in recs:
        r["id"] = canonical_id(r)


def find_from_topic(topic: str, n: int, pool_max: int, store: Store, src: Sources, llm: LLM,
                    cfg: dict[str, Any]) -> list[str]:
    k = int(cfg.get("queries", 5))
    queries = [topic] + [q for q in llm.chat_json(QUERY_SYSTEM.format(k=k), f"Topic: {topic}").get("queries", [])][:k]
    store.set_meta("queries", queries)
    cands = dedupe(_search_all(src, queries, cfg.get("per_query", {})))
    _resolve(src, cands)
    cands = dedupe(cands)
    log.info("candidate pool: %d unique papers", len(cands))
    if len(cands) < n:
        raise SystemExit(f"Only {len(cands)} candidates found for the topic; broaden it or lower the seed count.")

    vecs = llm.embed([embed_text(c["title"], c.get("abstract")) for c in cands])
    topic_vec = llm.embed([topic])[0]
    sims = vecs @ topic_vec
    order = np.argsort(-sims)[:pool_max]
    pool = [cands[i] for i in order]
    for c, i in zip(pool, order):
        c["topic_cos"] = float(sims[i])
    # Expandable seeds need an OpenAlex id: resolve the pool members still missing one.
    for c in pool:
        if not c.get("openalex"):
            try:
                hit = src.openalex.get(c["arxiv"]) if c.get("arxiv") else next(
                    (r for r in src.openalex.search(c["title"], 3) if titles_match(r["title"], c["title"])), None)
            except SourceUnavailable:
                hit = None
            if hit:
                merge(c, hit)
                c["id"] = canonical_id(c)
    store.set_meta("seed_pool", [{"title": c["title"], "year": c.get("year"), "topic_cos": round(c["topic_cos"], 4),
                                  "id": c["id"]} for c in pool])

    chars = int(cfg.get("abstract_chars_in_prompt", 1200))
    listing = "\n\n".join(f"[{i}] {c['title']} ({c.get('year') or 'n.d.'})\n{(c.get('abstract') or '(no abstract)')[:chars]}"
                          for i, c in enumerate(pool))
    picks = llm.chat_json(PICK_SYSTEM.format(topic=topic, n=n), listing).get("seeds", [])
    chosen: list[tuple[int, str]] = []
    for p in picks:
        try:
            idx = int(p.get("index"))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(pool) and idx not in [c for c, _ in chosen]:
            chosen.append((idx, str(p.get("reason", ""))))
    for idx in range(len(pool)):  # top up from the embedding ranking if the model returned too few
        if len(chosen) >= n:
            break
        if idx not in [c for c, _ in chosen]:
            chosen.append((idx, "added from the embedding ranking (picker returned fewer seeds)"))
    ids = []
    for idx, reason in chosen[:n]:
        pid = _store_seed(store, pool[idx], f"topic pool rank {idx}", reason)
        store.put_embeddings([pid], llm.s.embedding_model, vecs[order[idx]][None, :])
        ids.append(pid)
        log.info("seed: %s", pool[idx]["title"][:90])
    return ids

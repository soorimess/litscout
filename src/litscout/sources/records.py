"""Common paper record shape shared by all sources, and helpers to merge and identify records."""
from __future__ import annotations

import difflib
import hashlib
import re
from typing import Any, Optional

# Keys every source fills where it can. "id" is the canonical key in the store:
# the OpenAlex work id when known, else doi:/arxiv:/local: forms.
FIELDS = ("id", "openalex", "doi", "arxiv", "title", "abstract", "year", "venue",
          "cited_by", "subfield_id", "subfield", "oa_pdf", "type", "url")
NON_PAPER_TYPES = {"dataset", "paratext", "peer-review", "erratum", "editorial", "letter", "retraction",
                   "supplementary-materials", "grant", "standard"}
NON_PAPER_TITLE = re.compile(r"^(replication package|supplementary|artifact for|dataset for|data for)\b", re.I)
ARXIV_RE = re.compile(r"(?:arxiv\.org/(?:abs|pdf)/|arxiv[:\s]*|10\.48550/arxiv\.)(\d{4}\.\d{4,5})", re.I)


def norm_title(title: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]", "", (title or "").lower())


def titles_match(a: Optional[str], b: Optional[str], cutoff: float = 0.9) -> bool:
    na, nb = norm_title(a), norm_title(b)
    return bool(na and nb) and difflib.SequenceMatcher(None, na, nb).ratio() >= cutoff


def canonical_id(rec: dict[str, Any]) -> str:
    if rec.get("openalex"):
        return rec["openalex"]
    if rec.get("doi"):
        return f"doi:{rec['doi'].lower()}"
    if rec.get("arxiv"):
        return f"arxiv:{rec['arxiv']}"
    if (rec.get("url") or "").startswith("https://github.com/"):
        return "github:" + rec["url"][len("https://github.com/"):]
    return "local:" + hashlib.sha1(norm_title(rec.get("title")).encode()).hexdigest()[:16]


def is_paper(rec: dict[str, Any]) -> bool:
    title = (rec.get("title") or "").strip()
    return bool(title) and rec.get("type") not in NON_PAPER_TYPES and not NON_PAPER_TITLE.match(title)


def clean_doi(doi: Optional[str]) -> Optional[str]:
    if not doi:
        return None
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi.strip(), flags=re.I)
    return doi.lower() or None


def merge(base: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    """Fill empty fields of base from other."""
    for k, v in other.items():
        if v not in (None, "", []) and base.get(k) in (None, "", []):
            base[k] = v
    return base


def dedupe(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge records describing the same paper (same DOI, arXiv id or near-identical title)."""
    out: list[dict[str, Any]] = []
    index: dict[str, int] = {}
    for rec in records:
        keys = [f"doi:{rec['doi']}" if rec.get("doi") else None,
                f"arxiv:{rec['arxiv']}" if rec.get("arxiv") else None,
                f"t:{norm_title(rec.get('title'))}" if len(norm_title(rec.get("title"))) > 20 else None]
        hit = next((index[k] for k in keys if k and k in index), None)
        if hit is None:
            out.append(dict(rec))
            hit = len(out) - 1
        else:
            merge(out[hit], rec)
        for k in keys:
            if k:
                index[k] = hit
    for rec in out:
        rec["id"] = canonical_id(rec)
    return out

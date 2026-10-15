"""Semantic Scholar: fallback citation graph for papers OpenAlex covers poorly (mainly recent preprints).
Works without a key on the shared rate limit; S2_API_KEY raises it."""
from __future__ import annotations

from typing import Any, Optional

from .http import RateLimitedClient, SourceUnavailable
from .records import clean_doi, is_paper

API = "https://api.semanticscholar.org/graph/v1"
FIELDS = "title,abstract,year,venue,externalIds,citationCount,openAccessPdf"


def to_record(d: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not d or not d.get("title"):
        return None
    ext = d.get("externalIds") or {}
    rec = {"doi": clean_doi(ext.get("DOI")), "arxiv": ext.get("ArXiv"), "title": d["title"].strip(),
           "abstract": d.get("abstract"), "year": d.get("year"), "venue": d.get("venue") or None,
           "cited_by": d.get("citationCount"), "oa_pdf": (d.get("openAccessPdf") or {}).get("url") or None}
    # Without a DOI or arXiv id the record can't be merged or expanded reliably.
    return rec if is_paper(rec) and (rec["doi"] or rec["arxiv"]) else None


class SemanticScholar:
    def __init__(self, api_key: Optional[str] = None):
        self.http = RateLimitedClient(1.05 if api_key else 1.6, {"x-api-key": api_key} if api_key else None,
                                      max_retries=7)

    @staticmethod
    def ident(p: dict[str, Any]) -> Optional[str]:
        if p.get("arxiv"):
            return f"ARXIV:{p['arxiv']}"
        if p.get("doi"):
            return f"DOI:{p['doi']}"
        return None

    def _edges(self, ident: str, kind: str, cap: int) -> list[dict[str, Any]]:
        side = "citedPaper" if kind == "references" else "citingPaper"
        out, offset = [], 0
        while offset < cap:
            r = self.http.request("GET", f"{API}/paper/{ident}/{kind}",
                                  params={"fields": FIELDS, "offset": offset, "limit": min(1000, cap - offset)})
            if r.status_code == 404:
                return out
            if r.status_code != 200:
                raise SourceUnavailable(f"Semantic Scholar {kind} of {ident}: HTTP {r.status_code}")
            d = r.json()
            out.extend(x for x in (to_record(e.get(side)) for e in d.get("data") or []) if x)
            if "next" not in d:
                break
            offset = d["next"]
        return out

    def references(self, p: dict[str, Any], cap: int) -> list[dict[str, Any]]:
        ident = self.ident(p)
        return self._edges(ident, "references", cap) if ident else []

    def citations(self, p: dict[str, Any], cap: int) -> list[dict[str, Any]]:
        ident = self.ident(p)
        return self._edges(ident, "citations", cap) if ident else []

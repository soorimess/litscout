"""OpenAlex: the backbone for search, references, citations, abstracts and random samples."""
from __future__ import annotations

from typing import Any, Iterator, Optional

from .http import RateLimitedClient, SourceUnavailable
from .records import ARXIV_RE, clean_doi, is_paper

API = "https://api.openalex.org"
SELECT = ("id,doi,display_name,publication_year,primary_location,locations,cited_by_count,"
          "abstract_inverted_index,primary_topic,best_oa_location,type,referenced_works")


def _abstract(inv: Optional[dict[str, list[int]]]) -> Optional[str]:
    if not inv:
        return None
    return " ".join(w for _, w in sorted((p, w) for w, ps in inv.items() for p in ps))


def to_record(w: dict[str, Any]) -> Optional[dict[str, Any]]:
    if not w or not w.get("display_name"):
        return None
    doi = clean_doi(w.get("doi"))
    arxiv = None
    for loc in [w.get("primary_location") or {}] + (w.get("locations") or []):
        m = ARXIV_RE.search((loc or {}).get("landing_page_url") or "")
        if m:
            arxiv = m.group(1)
            break
    if not arxiv and doi:
        m = ARXIV_RE.search(doi)
        arxiv = m.group(1) if m else None
    topic = w.get("primary_topic") or {}
    sub = topic.get("subfield") or {}
    src = ((w.get("primary_location") or {}).get("source") or {})
    rec = {
        "openalex": w["id"].rsplit("/", 1)[-1],
        "doi": doi,
        "arxiv": arxiv,
        "title": w["display_name"].strip(),
        "abstract": _abstract(w.get("abstract_inverted_index")),
        "year": w.get("publication_year"),
        "venue": src.get("display_name"),
        "cited_by": w.get("cited_by_count"),
        "subfield_id": (sub.get("id") or "").rsplit("/", 1)[-1] or None,
        "subfield": sub.get("display_name"),
        "oa_pdf": (w.get("best_oa_location") or {}).get("pdf_url"),
        "type": w.get("type"),
        "refs": [r.rsplit("/", 1)[-1] for r in w.get("referenced_works") or []],
    }
    rec["id"] = rec["openalex"]
    return rec if is_paper(rec) else None


class OpenAlex:
    def __init__(self, min_interval: float = 0.11, api_key: Optional[str] = None, email: Optional[str] = None):
        params = {"api_key": api_key} if api_key else {}
        if email:
            params["mailto"] = email
        self.http = RateLimitedClient(min_interval, default_params=params)

    def _works(self, params: dict[str, Any]) -> dict[str, Any]:
        d = self.http.get_json(f"{API}/works", params={"select": SELECT, **params})
        if d is None:
            raise SourceUnavailable(f"OpenAlex /works {params}")
        return d

    def search(self, query: str, n: int) -> list[dict[str, Any]]:
        d = self._works({"search": query, "per-page": min(n, 200)})
        return [r for r in map(to_record, d.get("results") or []) if r]

    def get(self, ident: str) -> Optional[dict[str, Any]]:
        """ident: an OpenAlex id (W…), 'doi:<doi>' or an arXiv id."""
        if ident.startswith("doi:"):
            path = f"doi:{ident[4:]}"
        elif ident[:1] == "W":
            path = ident
        else:
            path = f"doi:10.48550/arxiv.{ident}"
        w = self.http.get_json(f"{API}/works/{path}", params={"select": SELECT})
        return to_record(w) if w else None

    def get_many(self, ids: list[str]) -> list[dict[str, Any]]:
        """Batch lookup of OpenAlex ids, 50 per request."""
        out = []
        for i in range(0, len(ids), 50):
            d = self._works({"filter": "openalex_id:" + "|".join(ids[i:i + 50]), "per-page": 50})
            out.extend(r for r in map(to_record, d.get("results") or []) if r)
        return out

    def get_by_dois(self, dois: list[str]) -> list[dict[str, Any]]:
        out = []
        for i in range(0, len(dois), 50):
            d = self._works({"filter": "doi:" + "|".join(dois[i:i + 50]), "per-page": 50})
            out.extend(r for r in map(to_record, d.get("results") or []) if r)
        return out

    def references(self, rec: dict[str, Any], cap: int) -> list[dict[str, Any]]:
        refs = rec.get("refs")
        if refs is None:
            full = self.get(rec["openalex"])
            refs = (full or {}).get("refs") or []
        return self.get_many(refs[:cap])

    def citations(self, openalex_id: str, cap: int) -> Iterator[dict[str, Any]]:
        cursor, n = "*", 0
        while cursor and n < cap:
            d = self._works({"filter": f"cites:{openalex_id}", "per-page": 200, "cursor": cursor})
            for r in map(to_record, d.get("results") or []):
                if r and n < cap:
                    n += 1
                    yield r
            cursor = (d.get("meta") or {}).get("next_cursor")

    def sample(self, n: int, seed: int, subfield_id: Optional[str] = None) -> list[dict[str, Any]]:
        """Random works for the null distribution (only those with an abstract are useful)."""
        params: dict[str, Any] = {"sample": n, "seed": seed, "per-page": 200}
        filters = ["has_abstract:true"]
        if subfield_id:
            filters.append(f"primary_topic.subfield.id:{subfield_id}")
        params["filter"] = ",".join(filters)
        out, page = [], 1
        while len(out) < n:
            d = self._works({**params, "page": page})
            batch = [r for r in map(to_record, d.get("results") or []) if r]
            if not batch:
                break
            out.extend(batch)
            page += 1
        return out[:n]

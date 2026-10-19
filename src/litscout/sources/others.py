"""Secondary sources: Crossref (DOI metadata, abstracts), arXiv (preprints) and DBLP (CS search)."""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from typing import Any, Optional

from .http import RateLimitedClient, SourceUnavailable
from .records import clean_doi, is_paper

TAG = re.compile(r"<[^>]+>")


class Crossref:
    API = "https://api.crossref.org/works"

    def __init__(self, min_interval: float = 0.25, email: Optional[str] = None):
        self.http = RateLimitedClient(min_interval, default_params={"mailto": email} if email else None)

    @staticmethod
    def to_record(m: dict[str, Any]) -> Optional[dict[str, Any]]:
        title = " ".join((m.get("title") or [""])[0].split())
        year = ((m.get("issued") or {}).get("date-parts") or [[None]])[0][0]
        abstract = m.get("abstract")
        rec = {
            "doi": clean_doi(m.get("DOI")),
            "title": html.unescape(title),
            "abstract": " ".join(html.unescape(TAG.sub(" ", abstract)).split()) if abstract else None,
            "year": year,
            "venue": (m.get("container-title") or [None])[0],
            "cited_by": m.get("is-referenced-by-count"),
            "type": m.get("type"),
        }
        return rec if is_paper(rec) else None

    def search(self, query: str, n: int) -> list[dict[str, Any]]:
        d = self.http.get_json(self.API, params={"query.bibliographic": query, "rows": n})
        if d is None:
            raise SourceUnavailable(f"Crossref search {query!r}")
        return [r for r in map(self.to_record, (d.get("message") or {}).get("items") or []) if r]

    def get(self, doi: str) -> Optional[dict[str, Any]]:
        d = self.http.get_json(f"{self.API}/{doi}")
        return self.to_record(d["message"]) if d and d.get("message") else None


class Arxiv:
    API = "https://export.arxiv.org/api/query"
    NS = {"a": "http://www.w3.org/2005/Atom"}

    def __init__(self, min_interval: float = 3.1):
        self.http = RateLimitedClient(min_interval)

    def _parse(self, xml: str) -> list[dict[str, Any]]:
        out = []
        for e in ET.fromstring(xml).findall("a:entry", self.NS):
            title = " ".join(e.findtext("a:title", "", self.NS).split())
            if not title or title == "Error":
                continue
            aid = re.sub(r"v\d+$", "", e.findtext("a:id", "", self.NS).split("/abs/")[-1])
            out.append({"arxiv": aid, "title": title,
                        "abstract": " ".join(e.findtext("a:summary", "", self.NS).split()),
                        "year": int(e.findtext("a:published", "0", self.NS)[:4] or 0) or None,
                        "venue": "arXiv", "oa_pdf": f"https://arxiv.org/pdf/{aid}"})
        return out

    def search(self, query: str, n: int) -> list[dict[str, Any]]:
        terms = " AND ".join(f"all:{w}" for w in re.findall(r"\w+", query) if len(w) > 2)
        r = self.http.request("GET", self.API, params={"search_query": terms, "max_results": n})
        if r.status_code != 200:
            raise SourceUnavailable(f"arXiv search {query!r}")
        return self._parse(r.text)

    def get_many(self, ids: list[str]) -> list[dict[str, Any]]:
        out = []
        for i in range(0, len(ids), 100):
            r = self.http.request("GET", self.API, params={"id_list": ",".join(ids[i:i + 100]), "max_results": 100})
            if r.status_code == 200:
                out.extend(self._parse(r.text))
        return out


class DBLP:
    API = "https://dblp.org/search/publ/api"

    def __init__(self, min_interval: float = 1.0):
        self.http = RateLimitedClient(min_interval)

    def search(self, query: str, n: int) -> list[dict[str, Any]]:
        d = self.http.get_json(self.API, params={"q": query, "format": "json", "h": n})
        if d is None:
            raise SourceUnavailable(f"DBLP search {query!r}")
        hits = (((d.get("result") or {}).get("hits") or {}).get("hit")) or []
        out = []
        for h in hits:
            info = h.get("info") or {}
            rec = {"title": html.unescape(str(info.get("title", ""))).rstrip("."), "year": int(info["year"]) if info.get("year") else None,
                   "venue": info.get("venue") if isinstance(info.get("venue"), str) else None,
                   "doi": clean_doi(info.get("doi")), "abstract": None}
            ee = info.get("ee") if isinstance(info.get("ee"), str) else ""
            m = re.search(r"arxiv\.org/abs/(\d{4}\.\d{4,5})", ee)
            rec["arxiv"] = m.group(1) if m else None
            if is_paper(rec):
                out.append(rec)
        return out

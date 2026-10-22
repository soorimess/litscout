"""GitHub repository search: grey literature (benchmarks published as a repo, blog or leaderboard,
without a paper). A repo whose README links an arXiv paper is reported as that paper instead."""
from __future__ import annotations

import re
from typing import Any, Optional

from .http import RateLimitedClient, SourceUnavailable
from .records import ARXIV_RE

MD_NOISE = re.compile(r"!\[[^\]]*\]\([^)]*\)|<[^>]+>|\[!\[.*?\]\(.*?\)\]\(.*?\)|https?://\S+|[#*`>|]+")


class GitHub:
    API = "https://api.github.com"

    def __init__(self, token: Optional[str] = None):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        # Search allows 10 requests/min without a token and 30 with one; READMEs need the core quota.
        self.http = RateLimitedClient(2.1 if token else 6.5, headers=headers)
        self.readme = bool(token)

    def _readme(self, full: str) -> str:
        d = self.http.get_json(f"{self.API}/repos/{full}/readme")
        if not d or "content" not in d:
            return ""
        import base64
        try:
            return base64.b64decode(d["content"]).decode("utf-8", "replace")
        except ValueError:
            return ""

    def search(self, query: str, n: int) -> list[dict[str, Any]]:
        d = self.http.get_json(f"{self.API}/search/repositories", params={"q": query, "per_page": n})
        if d is None:
            raise SourceUnavailable(f"GitHub search {query!r}")
        out = []
        for r in d.get("items", [])[:n]:
            desc = (r.get("description") or "").strip()
            if not desc:
                continue
            readme = self._readme(r["full_name"]) if self.readme else ""
            ax = ARXIV_RE.search(readme) or ARXIV_RE.search(desc)
            text = " ".join(MD_NOISE.sub(" ", readme).split())[:2500]
            out.append({
                "title": f"{r['name']}: {desc}"[:300],
                "abstract": " ".join(x for x in (desc, text) if x),
                "year": int(r["created_at"][:4]) if r.get("created_at") else None,
                "venue": "GitHub repository",
                "cited_by": r.get("stargazers_count"),
                "url": r.get("html_url"),
                "linked_arxiv": ax.group(1) if ax else None,
                "type": "repository",
            })
        return out

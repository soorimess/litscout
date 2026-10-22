"""Bibliographic sources bundled with their rate limits from the settings."""
from __future__ import annotations

from ..config import Settings
from .openalex import OpenAlex
from .github import GitHub
from .others import DBLP, Arxiv, Crossref
from .semanticscholar import SemanticScholar


class Sources:
    def __init__(self, s: Settings):
        iv = lambda name, default: float(s.t("sources", name, "min_interval_s", default=default))  # noqa: E731
        self.openalex = OpenAlex(iv("openalex", 0.11), s.openalex_api_key, s.contact_email)
        self.crossref = Crossref(iv("crossref", 0.25), s.contact_email)
        self.arxiv = Arxiv(iv("arxiv", 3.1))
        self.dblp = DBLP(iv("dblp", 1.0))
        self.s2 = SemanticScholar(s.s2_api_key)
        self.github = GitHub(s.github_token)

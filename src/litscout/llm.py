"""OpenAI-compatible client for chat (JSON answers) and embeddings, with a shared cost budget."""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import numpy as np
import requests

from .config import Settings

log = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """Thread-safe running cost in USD, from token usage reported by the provider."""

    def __init__(self, limit_usd: float, pricing: dict[str, float], spent: float = 0.0):
        self.limit, self.pricing, self.spent = limit_usd, pricing, spent
        self.tokens = {"chat_input": 0, "chat_output": 0, "embedding": 0}
        self._lock = threading.Lock()

    def add(self, kind: str, tokens: int) -> None:
        with self._lock:
            self.tokens[kind] += tokens
            self.spent += tokens * self.pricing.get(kind, 0.0) / 1e6

    def check(self) -> None:
        if self.spent >= self.limit:
            raise BudgetExceeded(f"cost budget reached: ${self.spent:.2f} of ${self.limit:.2f}")


class LLM:
    def __init__(self, s: Settings, budget: Budget):
        self.s, self.budget = s, budget
        self.headers = {"Authorization": f"Bearer {s.llm_api_key}"}
        self.batch = int(s.t("embedding", "batch_size", default=64))

    def _post(self, path: str, body: dict[str, Any], retries: int = 8) -> dict[str, Any]:
        err: Exception = RuntimeError("no attempt")
        for attempt in range(retries):
            wait = min(2 ** attempt * 2, 120)
            try:
                r = requests.post(f"{self.s.llm_base_url}{path}", json=body, headers=self.headers, timeout=180)
                if r.status_code == 429 and "budget" in r.text.lower():
                    # The provider's spending cap for this key is used up: retrying cannot help.
                    raise BudgetExceeded(f"LLM provider budget exhausted: {r.text[:300]}")
                if r.status_code in (429, 500, 502, 503, 504):
                    wait = max(wait, float(r.headers.get("retry-after") or 0))
                    raise requests.HTTPError(f"HTTP {r.status_code}: {r.text[:200]}")
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError) as e:
                err = e
                log.warning("LLM provider call %s failed (%s), retry in %.0fs", path, str(e)[:120], wait)
                time.sleep(wait)
        raise RuntimeError(f"LLM provider call {path} failed: {err}")

    def chat_json(self, system: str, user: str) -> dict[str, Any]:
        """One chat call that must answer with a JSON object."""
        self.budget.check()
        body = {"model": self.s.llm_model, "temperature": 0, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        for attempt in range(3):
            d = self._post("/chat/completions", body)
            u = d.get("usage") or {}
            self.budget.add("chat_input", u.get("prompt_tokens", 0))
            self.budget.add("chat_output", u.get("completion_tokens", 0))
            try:
                return json.loads(d["choices"][0]["message"]["content"])
            except (KeyError, IndexError, TypeError, ValueError):
                log.warning("malformed JSON from chat model, retrying (%d)", attempt)
        raise RuntimeError("chat model did not return valid JSON")

    def embed(self, texts: list[str]) -> np.ndarray:
        """L2-normalised float32 embeddings, one row per text."""
        rows: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            self.budget.check()
            chunk = [t[:24000] or " " for t in texts[i:i + self.batch]]
            d = self._post("/embeddings", {"model": self.s.embedding_model, "input": chunk})
            self.budget.add("embedding", (d.get("usage") or {}).get("prompt_tokens", 0))
            data = sorted(d["data"], key=lambda x: x["index"])
            rows.extend(x["embedding"] for x in data)
        m = np.asarray(rows, dtype=np.float32)
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return m / norms


def embed_text(title: str, abstract: str | None) -> str:
    return f"{title}\n\n{abstract}" if abstract else title

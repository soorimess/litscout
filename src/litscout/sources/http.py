"""Rate-limited HTTP with retry and backoff, shared by all bibliographic sources."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)
RETRY_STATUS = {429, 500, 502, 503, 504}


class SourceUnavailable(RuntimeError):
    """A request kept failing (rate limit, timeout, server error); the caller may retry later."""


class RateLimitedClient:
    def __init__(self, min_interval: float, headers: Optional[dict[str, str]] = None,
                 default_params: Optional[dict[str, str]] = None, max_retries: int = 6, timeout: int = 60):
        self.min_interval = min_interval
        self.headers = {"User-Agent": "LitScout/0.1 (systematic literature review)", **(headers or {})}
        self.default_params = default_params or {}
        self.max_retries, self.timeout = max_retries, timeout
        self._lock = threading.Lock()
        self._next = 0.0
        self.session = requests.Session()

    def _wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.min_interval
        if delay > 0:
            time.sleep(delay)

    def request(self, method: str, url: str, **kw: Any) -> requests.Response:
        """Return the response for any non-retryable status; raise SourceUnavailable after retries."""
        if self.default_params:
            kw["params"] = {**self.default_params, **(kw.get("params") or {})}
        backoff = 3.0
        for attempt in range(self.max_retries):
            self._wait()
            try:
                r = self.session.request(method, url, headers=self.headers, timeout=self.timeout, **kw)
            except requests.RequestException as e:
                log.debug("network error on %s: %s (attempt %d)", url[:100], e, attempt)
            else:
                if r.status_code not in RETRY_STATUS:
                    return r
                log.debug("HTTP %s on %s (attempt %d)", r.status_code, url[:100], attempt)
            time.sleep(backoff)
            backoff = min(backoff * 2, 90)
        raise SourceUnavailable(f"{method} {url[:120]} failed after {self.max_retries} attempts")

    def get_json(self, url: str, **kw: Any) -> Optional[Any]:
        """JSON body for 200, None for other statuses (e.g. 404); raises SourceUnavailable."""
        r = self.request("GET", url, **kw)
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None

"""Small HTTP helper with timeouts and retries."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)


class HttpClient:
    def __init__(self, timeout: float = 15.0, retries: int = 3, backoff: float = 1.0):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "memebot/0.1"
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code} from {url}", response=resp)
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                resp_obj = getattr(exc, "response", None)
                status = getattr(resp_obj, "status_code", None)
                # Don't retry client errors other than rate limits.
                if status is not None and 400 <= status < 500 and status != 429:
                    break
                sleep = self.backoff * (2**attempt)
                log.debug("Request %s %s failed (%s); retrying in %.1fs", method, url, exc, sleep)
                time.sleep(sleep)
        raise RuntimeError(f"{method} {url} failed: {last_exc}") from last_exc

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)

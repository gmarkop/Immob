"""A deliberately slow, well-behaved HTTP client for crawling.

- checks robots.txt before fetching anything from a site
- waits a few seconds (plus random jitter) between requests
- backs off and retries on 429 "Too Many Requests" and 5xx errors
- can cache responses on disk, so development doesn't hit the site repeatedly
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from collections.abc import Callable
from pathlib import Path
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; immob/0.1; personal apartment search)"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class RobotsDisallowed(Exception):
    """The site's robots.txt asks crawlers not to fetch this URL."""


class PoliteClient:
    def __init__(
        self,
        *,
        delay: float = 3.0,
        jitter: float = 2.0,
        user_agent: str = DEFAULT_USER_AGENT,
        max_retries: int = 3,
        cache_dir: Path | None = None,
        cache_ttl: float = 3600,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.user_agent = user_agent
        self._delay = delay
        self._jitter = jitter
        self._max_retries = max_retries
        self._cache_dir = cache_dir
        self._cache_ttl = cache_ttl
        self._sleep = sleep  # injectable so tests don't actually wait
        self._last_request: float | None = None
        self._robots: dict[str, RobotFileParser] = {}
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Language": "de-AT,de;q=0.9"},
            timeout=30,
            follow_redirects=True,
            transport=transport,
        )

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get_text(self, url: str, params: dict[str, str | int] | None = None) -> str:
        # Merge into the URL's own query string: httpx.URL(url, params=...) would
        # replace it, dropping e.g. the filters of a search URL pasted from a browser.
        full_url = str(httpx.URL(url).copy_merge_params(params or {}))
        self._check_robots(full_url)

        if (cached := self._cache_read(full_url)) is not None:
            log.debug("cache hit: %s", full_url)
            return cached

        for attempt in range(self._max_retries + 1):
            response = self._get(full_url)
            if response.status_code in RETRY_STATUSES and attempt < self._max_retries:
                wait = _retry_after(response) or 5 * 2**attempt
                log.warning("HTTP %s for %s, retrying in %ss", response.status_code, full_url, wait)
                self._sleep(wait)
                continue
            response.raise_for_status()
            self._cache_write(full_url, response.text)
            return response.text
        raise AssertionError("unreachable")  # the loop always returns or raises

    # --- internals -----------------------------------------------------------

    def _get(self, url: str) -> httpx.Response:
        """GET with the minimum delay since the previous request enforced."""
        if self._last_request is not None:
            elapsed = time.monotonic() - self._last_request
            wait = self._delay + random.uniform(0, self._jitter) - elapsed
            if wait > 0:
                self._sleep(wait)
        log.info("GET %s", url)
        try:
            return self._client.get(url)
        finally:
            self._last_request = time.monotonic()

    def _check_robots(self, url: str) -> None:
        parsed = httpx.URL(url)
        origin = f"{parsed.scheme}://{parsed.host}"
        parser = self._robots.get(origin)
        if parser is None:
            parser = RobotFileParser()
            response = self._get(f"{origin}/robots.txt")
            # Same rules as the standard library's RobotFileParser.read():
            # 401/403 means "keep out", any other error means "no rules".
            if response.status_code in (401, 403):
                parser.disallow_all = True
            elif response.status_code >= 400:
                parser.allow_all = True
            else:
                parser.parse(response.text.splitlines())
            self._robots[origin] = parser
        if not parser.can_fetch(self.user_agent, url):
            raise RobotsDisallowed(f"robots.txt of {origin} disallows fetching {url}")

    def _cache_path(self, url: str) -> Path | None:
        if self._cache_dir is None:
            return None
        return self._cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".html")

    def _cache_read(self, url: str) -> str | None:
        path = self._cache_path(url)
        if path is None or not path.exists():
            return None
        if time.time() - path.stat().st_mtime > self._cache_ttl:
            return None
        return path.read_text(encoding="utf-8")

    def _cache_write(self, url: str, text: str) -> None:
        path = self._cache_path(url)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["Retry-After"])
    except (KeyError, ValueError):
        return None

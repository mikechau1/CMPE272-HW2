"""Thin, opinionated async client for the GitHub Issues REST API.

Responsibilities kept here rather than in the routers:

* auth + the headers GitHub asks for (``Accept: application/vnd.github+json``,
  ``X-GitHub-Api-Version``);
* retry with exponential backoff, but **only for safe/idempotent methods** --
  retrying ``POST /issues`` after a 502 could create the issue twice, so it
  isn't retried;
* rate-limit awareness: the last seen ``x-ratelimit-*`` values are cached, and
  an exhausted budget short-circuits before burning another call;
* conditional GET: ETags are remembered per URL+query and replayed as
  ``If-None-Match``, so a 304 costs no rate-limit quota at all.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from . import __version__
from .config import Settings
from .errors import (
    AppError,
    NotConfigured,
    Unauthorized,
    from_github_response,
    from_transport_error,
    is_rate_limited,
)
from .logging_config import get_logger

logger = get_logger(__name__)

GITHUB_ACCEPT = "application/vnd.github+json"
GITHUB_API_VERSION = "2022-11-28"
USER_AGENT = f"cmpe272-issues-gateway/{__version__}"

# Methods whose repetition cannot create a second resource.
RETRYABLE_METHODS = frozenset({"GET", "HEAD", "PATCH", "PUT", "DELETE"})


@dataclass
class RateLimitSnapshot:
    """Last ``x-ratelimit-*`` values GitHub returned."""

    limit: int | None = None
    remaining: int | None = None
    reset: int | None = None
    used: int | None = None
    resource: str | None = None

    def as_headers(self) -> dict[str, str]:
        out = {}
        if self.limit is not None:
            out["X-RateLimit-Limit"] = str(self.limit)
        if self.remaining is not None:
            out["X-RateLimit-Remaining"] = str(self.remaining)
        if self.reset is not None:
            out["X-RateLimit-Reset"] = str(self.reset)
        return out


@dataclass
class GitHubResult:
    """A successful upstream call, normalised for the routers."""

    data: Any
    status_code: int
    headers: httpx.Headers
    from_cache: bool = False

    @property
    def link(self) -> str | None:
        return self.headers.get("link")

    @property
    def etag(self) -> str | None:
        return self.headers.get("etag")


@dataclass
class _CacheEntry:
    etag: str
    data: Any
    headers: dict[str, str] = field(default_factory=dict)


class ETagCache:
    """Bounded LRU of ``ETag`` -> last good body, keyed by method+URL+query."""

    def __init__(self, max_entries: int = 256) -> None:
        self.max_entries = max_entries
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(method: str, path: str, params: dict[str, Any] | None) -> str:
        query = urlencode(sorted((k, str(v)) for k, v in (params or {}).items() if v is not None))
        return f"{method.upper()} {path}?{query}"

    def get(self, key: str) -> _CacheEntry | None:
        entry = self._entries.get(key)
        if entry is not None:
            self._entries.move_to_end(key)
        return entry

    def put(self, key: str, entry: _CacheEntry) -> None:
        self._entries[key] = entry
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()
        self.hits = self.misses = 0

    def __len__(self) -> int:
        return len(self._entries)


class GitHubClient:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        *,
        cache: ETagCache | None = None,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self._client = client or httpx.AsyncClient(timeout=settings.github_timeout_s)
        self._owns_client = client is None
        self.cache = cache if cache is not None else ETagCache()
        self.rate_limit = RateLimitSnapshot()
        self._sleep = sleep

    # -- lifecycle ---------------------------------------------------------

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- plumbing ----------------------------------------------------------

    @property
    def repo_path(self) -> str:
        return f"/repos/{self.settings.github_owner}/{self.settings.github_repo}"

    def _require_config(self) -> None:
        if not self.settings.has_token:
            raise Unauthorized(
                "GITHUB_TOKEN is not set, so the gateway has no credentials to call "
                "GitHub with. Set a fine-grained PAT with 'Issues: Read and write' "
                "on the configured repository.",
                code="missing_credentials",
            )
        if not self.settings.has_repo:
            raise NotConfigured(
                "GITHUB_OWNER and GITHUB_REPO must both be set for the gateway to "
                "know which repository to operate on."
            )

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Accept": GITHUB_ACCEPT,
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {self.settings.github_token}",
        }
        if extra:
            headers.update({k: v for k, v in extra.items() if v is not None})
        return headers

    def _absorb_rate_limit(self, response: httpx.Response) -> None:
        def as_int(name: str) -> int | None:
            raw = response.headers.get(name)
            if raw is None:
                return None
            try:
                return int(raw)
            except ValueError:
                return None

        self.rate_limit = RateLimitSnapshot(
            limit=as_int("x-ratelimit-limit"),
            remaining=as_int("x-ratelimit-remaining"),
            reset=as_int("x-ratelimit-reset"),
            used=as_int("x-ratelimit-used"),
            resource=response.headers.get("x-ratelimit-resource"),
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        extra_headers: dict[str, str] | None = None,
        context: str = "",
        use_cache: bool | None = None,
    ) -> GitHubResult:
        """Issue one upstream call, with retry, backoff and conditional GET."""
        self._require_config()

        method = method.upper()
        url = f"{self.settings.github_api_url}{path}"
        clean_params = {k: v for k, v in (params or {}).items() if v is not None and v != ""}

        caching = (
            self.settings.enable_etag_cache if use_cache is None else use_cache
        ) and method == "GET"
        cache_key = ETagCache.key(method, path, clean_params) if caching else None
        cached = self.cache.get(cache_key) if cache_key else None

        headers = self._headers(extra_headers)
        if cached is not None:
            headers["If-None-Match"] = cached.etag

        attempts = self.settings.github_max_retries + 1
        last_error: AppError | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.request(
                    method, url, params=clean_params, json=json, headers=headers
                )
            except httpx.HTTPError as exc:
                last_error = from_transport_error(exc, context=context)
                if method in RETRYABLE_METHODS and attempt < attempts:
                    await self._backoff(attempt, reason="transport")
                    continue
                raise last_error from exc

            self._absorb_rate_limit(response)

            # 304: the cached body is still current and this cost no quota.
            if response.status_code == 304 and cached is not None:
                self.cache.hits += 1
                merged = httpx.Headers(cached.headers)
                for name in ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"):
                    if name in response.headers:
                        merged[name] = response.headers[name]
                logger.debug(
                    "github conditional GET hit", extra={"path": path, "etag": cached.etag}
                )
                return GitHubResult(cached.data, 200, merged, from_cache=True)

            if response.is_success:
                data = self._decode(response)
                if cache_key and (etag := response.headers.get("etag")):
                    self.cache.misses += 1
                    self.cache.put(
                        cache_key,
                        _CacheEntry(etag=etag, data=data, headers=dict(response.headers)),
                    )
                return GitHubResult(data, response.status_code, response.headers)

            retryable_status = response.status_code >= 500 or (
                is_rate_limited(response) and response.status_code == 429
            )
            if retryable_status and method in RETRYABLE_METHODS and attempt < attempts:
                await self._backoff(attempt, response=response, reason="status")
                continue

            raise from_github_response(response, context=context)

        raise last_error or from_transport_error(  # pragma: no cover - loop always returns
            httpx.HTTPError("exhausted retries"), context=context
        )

    async def _backoff(
        self, attempt: int, *, response: httpx.Response | None = None, reason: str = ""
    ) -> None:
        """Exponential backoff, but obey ``Retry-After`` when GitHub sets one."""
        delay = min(0.25 * (2 ** (attempt - 1)), 8.0)
        if response is not None and (raw := response.headers.get("retry-after")):
            # Capped: an upstream answering `retry-after: 99999` must not pin a
            # worker for a day.
            with contextlib.suppress(ValueError):
                delay = max(delay, min(float(raw), 30.0))
        logger.warning(
            "retrying github request",
            extra={"attempt": attempt, "delay_s": round(delay, 3), "reason": reason},
        )
        await self._sleep(delay)

    @staticmethod
    def _decode(response: httpx.Response) -> Any:
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    # -- Issues API --------------------------------------------------------

    async def create_issue(
        self, *, title: str, body: str | None = None, labels: list[str] | None = None
    ) -> GitHubResult:
        payload: dict[str, Any] = {"title": title}
        if body is not None:
            payload["body"] = body
        if labels:
            payload["labels"] = labels
        return await self.request(
            "POST", f"{self.repo_path}/issues", json=payload, context="creating an issue"
        )

    async def list_issues(
        self,
        *,
        state: Literal["open", "closed", "all"] = "open",
        labels: str | None = None,
        page: int = 1,
        per_page: int = 30,
        sort: str | None = None,
        direction: str | None = None,
        if_none_match: str | None = None,
    ) -> GitHubResult:
        params = {
            "state": state,
            "labels": labels,
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "direction": direction,
        }
        extra = {"If-None-Match": if_none_match} if if_none_match else None
        return await self.request(
            "GET",
            f"{self.repo_path}/issues",
            params=params,
            extra_headers=extra,
            context="listing issues",
        )

    async def get_issue(self, number: int) -> GitHubResult:
        return await self.request(
            "GET", f"{self.repo_path}/issues/{number}", context=f"fetching issue #{number}"
        )

    async def update_issue(self, number: int, changes: dict[str, Any]) -> GitHubResult:
        return await self.request(
            "PATCH",
            f"{self.repo_path}/issues/{number}",
            json=changes,
            context=f"updating issue #{number}",
        )

    async def create_comment(self, number: int, body: str) -> GitHubResult:
        return await self.request(
            "POST",
            f"{self.repo_path}/issues/{number}/comments",
            json={"body": body},
            context=f"commenting on issue #{number}",
        )

    async def list_comments(
        self, number: int, *, page: int = 1, per_page: int = 30
    ) -> GitHubResult:
        return await self.request(
            "GET",
            f"{self.repo_path}/issues/{number}/comments",
            params={"page": page, "per_page": per_page},
            context=f"listing comments on issue #{number}",
        )

    async def get_repo(self) -> GitHubResult:
        return await self.request("GET", self.repo_path, context="checking repository access")

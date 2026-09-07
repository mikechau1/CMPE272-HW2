"""One error shape for the whole API, and the GitHub -> gateway translation.

Every failure the client can see is an :class:`AppError` serialised as::

    {"error": {"code": "not_found", "message": "...", "status": 404,
               "request_id": "...", "details": [...]}}

GitHub's own status codes are deliberately *not* passed through verbatim --
a 403 that means "rate limited" is a very different thing for our caller than
a 403 that means "your token lacks Issues: write", so they map to 429 and 403
respectively.  See :func:`from_github_response`.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .logging_config import request_id_ctx

# GitHub documents this on every error body it returns.
_GITHUB_DOC_HINT = "See https://docs.github.com/rest for the upstream contract."


class AppError(Exception):
    """Base class for every client-visible failure."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: Any = None,
        headers: dict[str, str] | None = None,
        upstream_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details = details
        self.headers = headers or {}
        self.upstream_status = upstream_status

    def to_dict(self, request_id: str | None = None) -> dict[str, Any]:
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "status": self.status_code,
        }
        # An unhandled exception unwinds past the middleware, which resets the
        # contextvar on its way out, so the caller passes the id explicitly.
        if (rid := request_id or request_id_ctx.get()) is not None:
            error["request_id"] = rid
        if self.details is not None:
            error["details"] = self.details
        if self.upstream_status is not None:
            error["upstream_status"] = self.upstream_status
        return {"error": error}


class BadRequest(AppError):
    status_code = 400
    code = "bad_request"


class ValidationFailed(BadRequest):
    code = "validation_error"


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class PayloadTooLarge(AppError):
    status_code = 413
    code = "payload_too_large"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, message: str, *, retry_after: int, **kwargs: Any) -> None:
        headers = kwargs.pop("headers", {}) or {}
        headers.setdefault("Retry-After", str(max(int(retry_after), 0)))
        super().__init__(message, headers=headers, **kwargs)
        self.retry_after = retry_after


class UpstreamError(AppError):
    status_code = 502
    code = "upstream_error"


class UpstreamTimeout(AppError):
    status_code = 504
    code = "upstream_timeout"


class NotConfigured(AppError):
    status_code = 503
    code = "not_configured"


# ---------------------------------------------------------------------------
# GitHub -> gateway mapping
# ---------------------------------------------------------------------------


def _github_message(payload: Any) -> str | None:
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str) and message:
            return message
    return None


def _github_details(payload: Any) -> Any:
    """GitHub's 422 bodies carry an ``errors`` array; surface it as details."""
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            cleaned = []
            for item in errors:
                if isinstance(item, dict):
                    cleaned.append(
                        {
                            k: v
                            for k, v in item.items()
                            if k in {"resource", "field", "code", "message", "value"}
                        }
                    )
                else:
                    cleaned.append({"message": str(item)})
            return cleaned
    return None


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (ValueError, UnicodeDecodeError):
        return None


def _retry_after_seconds(response: httpx.Response, *, now: float | None = None) -> int | None:
    """Seconds to wait, from Retry-After or the rate-limit reset epoch."""
    now = time.time() if now is None else now

    raw = response.headers.get("retry-after")
    if raw:
        try:
            return max(int(float(raw.strip())), 1)
        except ValueError:
            pass

    reset = response.headers.get("x-ratelimit-reset")
    if reset:
        try:
            return max(int(float(reset.strip())) - int(now), 1)
        except ValueError:
            pass
    return None


def is_rate_limited(response: httpx.Response) -> bool:
    """True when GitHub is telling us to slow down rather than denying access.

    GitHub signals three different things with 403/429:
      * primary rate limit  -> 403/429 with ``x-ratelimit-remaining: 0``
      * secondary rate limit -> 403/429 with ``retry-after``
      * genuine permission denial -> 403 with neither
    """
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    if response.headers.get("x-ratelimit-remaining") == "0":
        return True
    if response.headers.get("retry-after"):
        return True
    body = _safe_json(response)
    message = (_github_message(body) or "").lower()
    return "rate limit" in message or "abuse detection" in message


def from_github_response(response: httpx.Response, *, context: str = "") -> AppError:
    """Translate a failed GitHub response into the gateway's error type."""
    payload = _safe_json(response)
    upstream = _github_message(payload)
    details = _github_details(payload)
    where = f" while {context}" if context else ""
    status = response.status_code

    if status == 401:
        return Unauthorized(
            "GitHub rejected the configured GITHUB_TOKEN. Check that the token is "
            "valid, unexpired, and scoped to this repository. "
            f"GitHub said: {upstream or 'Bad credentials'}.",
            code="github_unauthorized",
            details=details,
            upstream_status=status,
        )

    if is_rate_limited(response):
        retry_after = _retry_after_seconds(response) or 60
        limit = response.headers.get("x-ratelimit-limit")
        scope = "primary" if response.headers.get("x-ratelimit-remaining") == "0" else "secondary"
        return RateLimited(
            f"GitHub {scope} rate limit exhausted{where}"
            + (f" (limit {limit}/hour)" if limit else "")
            + f". Retry after {retry_after}s. GitHub said: {upstream or 'rate limit exceeded'}.",
            retry_after=retry_after,
            code="rate_limited",
            details=details,
            upstream_status=status,
        )

    if status == 403:
        return Forbidden(
            "GitHub denied the request. The token is valid but lacks the required "
            "permission (Issues: Read and write) on this repository, or the repository "
            f"has issues disabled. GitHub said: {upstream or 'Forbidden'}.",
            code="github_forbidden",
            details=details,
            upstream_status=status,
        )

    if status == 404:
        return NotFound(
            f"Not found{where}. Either the resource does not exist or the token "
            "cannot see it (GitHub returns 404 rather than 403 for private "
            f"resources). GitHub said: {upstream or 'Not Found'}.",
            code="not_found",
            details=details,
            upstream_status=status,
        )

    if status == 410:
        return NotFound(
            f"Gone{where}. Issues are disabled for this repository or the resource "
            f"was permanently removed. GitHub said: {upstream or 'Gone'}.",
            code="gone",
            upstream_status=status,
        )

    if status in (400, 422):
        return ValidationFailed(
            f"GitHub rejected the payload{where}: {upstream or 'Validation Failed'}. "
            + _GITHUB_DOC_HINT,
            details=details,
            upstream_status=status,
        )

    if 500 <= status < 600:
        return UpstreamError(
            f"GitHub returned {status}{where} and the request could not be completed. "
            "This is an upstream fault; retry shortly.",
            code="github_unavailable",
            upstream_status=status,
        )

    return UpstreamError(
        f"Unexpected GitHub response {status}{where}. "
        f"GitHub said: {upstream or response.reason_phrase or 'unknown error'}.",
        upstream_status=status,
    )


def from_transport_error(exc: Exception, *, context: str = "") -> AppError:
    """Translate an httpx transport-level failure."""
    where = f" while {context}" if context else ""
    if isinstance(exc, httpx.TimeoutException):
        return UpstreamTimeout(
            f"Timed out talking to the GitHub API{where}. The gateway gave up "
            "waiting; the upstream request may or may not have been applied.",
            code="github_timeout",
        )
    return UpstreamError(
        f"Could not reach the GitHub API{where}: {type(exc).__name__}. "
        "Check network egress and GITHUB_API_URL.",
        code="github_unreachable",
    )

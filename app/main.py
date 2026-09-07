"""Application factory, lifespan, and the contract-first OpenAPI wiring.

``openapi.yaml`` at the repository root is the source of truth for the API.
The app serves *that file* at ``/openapi.json`` and ``/openapi.yaml`` instead
of a schema reverse-engineered from the code, so ``/docs`` shows the contract
the client was written against.  ``tests/unit/test_openapi_contract.py`` keeps
the two from drifting apart.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .config import Settings, get_settings
from .errors import AppError, BadRequest
from .github_client import GitHubClient
from .logging_config import configure_logging, get_logger, redact, request_id_ctx
from .middleware import REQUEST_ID_HEADER, RequestContextMiddleware
from .routers import health, issues, webhook
from .store import EventStore

logger = get_logger("app.main")

CONTRACT_PATH = Path(
    os.environ.get("OPENAPI_PATH") or Path(__file__).resolve().parent.parent / "openapi.yaml"
)

_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
}


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Pydantic hands back raw input in errors; make it safe to serialise."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")[:200]
    if isinstance(value, str):
        return value[:200]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in list(value.items())[:20]}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value[:20]]
    if isinstance(value, int | float | bool) or value is None:
        return value
    return str(value)[:200]


def _pointer(loc: tuple[Any, ...]) -> str:
    return ".".join(str(part) for part in loc) or "body"


def _request_id(request: Request) -> str | None:
    """The id assigned by the middleware.

    Read from the scope-backed request state rather than the contextvar: an
    unhandled exception unwinds past the middleware (resetting the contextvar)
    before the catch-all handler runs, and a 500 with no correlation id is
    exactly the one a reader most needs.
    """
    return getattr(request.state, "request_id", None) or request_id_ctx.get()


def _error_response(request: Request, exc: AppError) -> JSONResponse:
    rid = _request_id(request)
    headers = dict(exc.headers)
    if rid:
        headers.setdefault(REQUEST_ID_HEADER, rid)
    return JSONResponse(exc.to_dict(rid), status_code=exc.status_code, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        level = logger.warning if exc.status_code < 500 else logger.error
        level(
            "request failed",
            extra={"error_code": exc.code, "status": exc.status_code, "detail": exc.message},
        )
        return _error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default is 422; the contract specifies 400 for bad input.
        details = [
            {
                "field": _pointer(err.get("loc", ())),
                "message": err.get("msg", "invalid value"),
                "type": err.get("type", "value_error"),
                "input": _jsonable(err.get("input")),
            }
            for err in exc.errors()
        ]
        summary = (
            "; ".join(f"{d['field']}: {d['message']}" for d in details[:5]) or "invalid request"
        )
        error = BadRequest(
            f"Request validation failed -- {summary}",
            code="validation_error",
            details=details,
        )
        logger.warning(
            "request rejected",
            extra={"error_code": error.code, "fields": [d["field"] for d in details]},
        )
        return _error_response(request, error)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        error = AppError(
            str(exc.detail) if exc.detail else "Request could not be completed.",
            code=_STATUS_CODES.get(exc.status_code, "http_error"),
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None) or {},
        )
        return _error_response(request, error)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled exception", extra={"error_type": type(exc).__name__})
        error = AppError(
            "The gateway hit an unexpected internal error. The request id in this "
            "response correlates with the server logs.",
            code="internal_error",
            status_code=500,
        )
        return _error_response(request, error)


# ---------------------------------------------------------------------------
# OpenAPI contract
# ---------------------------------------------------------------------------


def load_contract(path: Path = CONTRACT_PATH) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            spec = yaml.safe_load(handle)
    except FileNotFoundError:
        logger.warning(
            "openapi contract not found; falling back to generated schema",
            extra={"path": str(path)},
        )
        return None
    except yaml.YAMLError as exc:
        logger.error(
            "openapi contract is not valid YAML",
            extra={"path": str(path), "detail": str(exc)},
        )
        return None
    return spec if isinstance(spec, dict) else None


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        missing = settings.missing_vars()
        logger.info(
            "starting issues-gateway",
            extra={
                "version": __version__,
                "port": settings.port,
                "repo": settings.repo_slug if settings.has_repo else None,
                "github_api_url": settings.github_api_url,
                "github_token": redact(settings.github_token),
                "webhook_secret": redact(settings.webhook_secret),
                "etag_cache": settings.enable_etag_cache,
                "missing_env": missing,
            },
        )
        if missing:
            logger.warning(
                "incomplete configuration; affected routes will return 401/503",
                extra={"missing_env": missing},
            )

        client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.github_timeout_s),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=True,
        )
        app.state.settings = settings
        app.state.http = client
        app.state.github = GitHubClient(settings, client)
        app.state.store = EventStore(settings.event_store_path, settings.event_retention)
        try:
            yield
        finally:
            await client.aclose()
            app.state.store.close()
            logger.info("issues-gateway stopped")

    app = FastAPI(
        title="GitHub Issues Gateway",
        version=__version__,
        description="A thin, validated HTTP gateway over the GitHub Issues REST API.",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Settings/store must exist even without the lifespan (TestClient without a
    # context manager, or an ASGI probe), so bind eagerly too.
    app.state.settings = settings

    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)

    app.include_router(health.router)
    app.include_router(issues.router)
    app.include_router(webhook.router)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/docs")

    @app.get("/openapi.yaml", include_in_schema=False)
    async def openapi_yaml() -> FileResponse:
        if not CONTRACT_PATH.exists():
            raise AppError(
                "openapi.yaml is not bundled with this deployment.",
                code="contract_missing",
                status_code=404,
            )
        return FileResponse(CONTRACT_PATH, media_type="application/yaml", filename="openapi.yaml")

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        contract = load_contract()
        if contract is None:
            from fastapi.openapi.utils import get_openapi

            contract = get_openapi(title=app.title, version=app.version, routes=app.routes)
        app.openapi_schema = contract
        return contract

    app.openapi = custom_openapi  # type: ignore[method-assign]
    return app


app = create_app()


def main() -> None:  # pragma: no cover - process entrypoint
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",  # noqa: S104 - containers must bind all interfaces
        port=settings.port,
        log_config=None,
    )


if __name__ == "__main__":  # pragma: no cover
    main()

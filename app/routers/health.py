"""Liveness and readiness.

``/healthz`` answers "is this process up?" without calling GitHub, so an
upstream outage or an exhausted rate limit never takes the container down.
``/readyz?deep=true` is the opt-in probe that actually reaches GitHub.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import __version__
from ..config import Settings
from ..deps import get_github, get_settings_dep, get_store
from ..errors import AppError
from ..github_client import GitHubClient
from ..store import EventStore

router = APIRouter(tags=["ops"])

_STARTED_AT = time.time()


@router.get("/healthz")
async def healthz(
    settings: Annotated[Settings, Depends(get_settings_dep)],
    store: Annotated[EventStore, Depends(get_store)],
) -> JSONResponse:
    try:
        stored = await run_in_threadpool(store.count)
        store_ok = True
    except Exception:  # noqa: BLE001 - health must not raise
        stored, store_ok = -1, False

    missing = settings.missing_vars()
    return JSONResponse(
        {
            "status": "ok" if store_ok else "degraded",
            "version": __version__,
            "uptime_s": round(time.time() - _STARTED_AT, 3),
            "repo": settings.repo_slug if settings.has_repo else None,
            "config": {
                "complete": not missing,
                "missing_env": missing,
                "etag_cache": settings.enable_etag_cache,
            },
            "event_store": {"ok": store_ok, "path": settings.event_store_path, "events": stored},
        },
        status_code=200 if store_ok else 503,
    )


@router.get("/readyz")
async def readyz(
    github: Annotated[GitHubClient, Depends(get_github)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    deep: Annotated[bool, Query(description="Also verify GitHub reachability and auth.")] = False,
) -> JSONResponse:
    missing = settings.missing_vars()
    body: dict[str, object] = {"status": "ok", "missing_env": missing}

    if missing:
        body["status"] = "not_ready"
        return JSONResponse(body, status_code=503)

    if deep:
        try:
            await github.get_repo()
            body["github"] = {"reachable": True, "repo": settings.repo_slug}
        except AppError as exc:
            body["status"] = "not_ready"
            body["github"] = {"reachable": False, "error": exc.code, "message": exc.message}
            return JSONResponse(body, status_code=503)
        body["rate_limit"] = {
            "limit": github.rate_limit.limit,
            "remaining": github.rate_limit.remaining,
            "reset": github.rate_limit.reset,
        }

    return JSONResponse(body, status_code=200)

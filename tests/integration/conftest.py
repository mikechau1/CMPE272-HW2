"""Integration fixtures.

These talk to the real GitHub API using the same environment variables the
service reads, so they double as a smoke test of an operator's configuration.
Everything here skips cleanly when credentials are absent, which is what lets
CI run the unit suite on a fork without secrets.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

# Titles are prefixed so anything this suite leaves behind is obvious in the UI.
TITLE_PREFIX = "[integration]"


def _live_settings() -> Settings | None:
    settings = Settings(_env_file=".env")
    if not (settings.has_token and settings.has_repo):
        return None
    return settings.model_copy(
        update={
            "event_store_path": ":memory:",
            "log_level": "WARNING",
            "log_format": "text",
            # Real network: allow a couple of retries for a transient 5xx.
            "github_max_retries": 2,
            "webhook_secret": settings.webhook_secret or "integration-placeholder-secret",
        }
    )


@pytest.fixture(scope="session")
def live_settings() -> Settings:
    settings = _live_settings()
    if settings is None:
        pytest.skip(
            "live GitHub credentials are not configured; set GITHUB_TOKEN, "
            "GITHUB_OWNER and GITHUB_REPO (see .env.example)"
        )
    return settings


@pytest.fixture(scope="session")
def live(live_settings: Settings) -> Iterator[TestClient]:
    """The real app, wired to the real GitHub API."""
    with TestClient(create_app(live_settings), raise_server_exceptions=False) as client:
        yield client


@pytest.fixture(scope="session")
def created_issues(live: TestClient) -> Iterator[list[int]]:
    """Track issues this suite creates and close them on the way out.

    GitHub has no delete-issue endpoint, so "cleanup" means closing -- the same
    constraint that shapes the gateway's own API.
    """
    numbers: list[int] = []
    yield numbers
    for number in numbers:
        # Best-effort: a teardown failure must not mask a real test failure.
        with contextlib.suppress(Exception):
            live.patch(f"/issues/{number}", json={"state": "closed", "state_reason": "completed"})


@pytest.fixture
def new_issue(live: TestClient, created_issues: list[int]):
    """Create a tracked issue and return its body."""

    def _create(title: str, **fields: object) -> dict:
        response = live.post("/issues", json={"title": f"{TITLE_PREFIX} {title}", **fields})
        assert response.status_code == 201, response.text
        issue = response.json()
        created_issues.append(issue["number"])
        return issue

    return _create


@pytest.fixture(scope="session")
def service_url() -> str:
    """Base URL of a *running* gateway process, for the tunnel tests."""
    url = os.environ.get("SERVICE_URL")
    if not url:
        port = os.environ.get("PORT", "8000")
        url = f"http://localhost:{port}"
    return url.rstrip("/")

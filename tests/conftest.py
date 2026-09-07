"""Shared fixtures.

Unit tests never touch the network: `respx` intercepts the app's outbound
httpx client while Starlette's TestClient (a separate, non-httpx transport)
drives the app itself.  The event store runs in-memory per test, so
idempotency assertions start from a clean slate every time.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security import compute_signature

FIXTURES = Path(__file__).parent / "fixtures"

TEST_SECRET = "test-webhook-secret-not-a-real-one"  # noqa: S105
TEST_TOKEN = "github_pat_TESTTOKEN"  # noqa: S105
TEST_OWNER = "mikechau1"
TEST_REPO = "cmpe272-issues-gw"
REPO_PATH = f"/repos/{TEST_OWNER}/{TEST_REPO}"


def load_fixture(name: str) -> Any:
    """Load a captured GitHub payload from tests/fixtures."""
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(autouse=True)
def _quiet_http_logs() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@pytest.fixture
def settings() -> Settings:
    # `_env_file=None` matters: without it these settings would inherit values
    # from a developer's real .env, and the suite would pass locally while
    # failing in CI (or vice versa). Unit tests must be hermetic.
    return Settings(
        _env_file=None,
        github_token=TEST_TOKEN,
        github_owner=TEST_OWNER,
        github_repo=TEST_REPO,
        webhook_secret=TEST_SECRET,
        port=8000,
        github_api_url="https://api.github.com",
        github_max_retries=0,  # retry behaviour is exercised directly, not per-route
        event_store_path=":memory:",
        log_format="text",
        log_level="WARNING",
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # raise_server_exceptions=False so the catch-all handler's 500 body is
    # observable, exactly as a real client would see it.
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def gh(settings: Settings) -> Iterator[respx.MockRouter]:
    """Intercept the app's outbound GitHub calls.

    ``assert_all_mocked`` stays on: an unexpected upstream call is a test
    failure, not a silent passthrough.
    """
    with respx.mock(
        base_url=settings.github_api_url, assert_all_called=False, assert_all_mocked=True
    ) as router:
        yield router


@pytest.fixture
def sign() -> Callable[[bytes], str]:
    """Produce the ``X-Hub-Signature-256`` GitHub would send for a body."""

    def _sign(body: bytes) -> str:
        return compute_signature(TEST_SECRET, body)

    return _sign


@pytest.fixture
def deliver(client: TestClient, sign: Callable[[bytes], str]) -> Callable[..., Any]:
    """POST a correctly signed webhook delivery, the way GitHub would."""

    def _deliver(
        payload: dict[str, Any],
        *,
        event: str = "issues",
        delivery_id: str = "11111111-2222-3333-4444-555555555555",
        signature: str | None = None,
        raw: bytes | None = None,
    ) -> Any:
        body = raw if raw is not None else json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": signature if signature is not None else sign(body),
            "User-Agent": "GitHub-Hookshot/abc1234",
        }
        return client.post("/webhook", content=body, headers=headers)

    return _deliver

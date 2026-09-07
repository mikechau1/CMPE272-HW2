"""Negative paths that cannot be provoked on demand against real GitHub.

Exhausting a 5000/hour rate limit or waiting for a GitHub outage is not a
test strategy, so these drive the *whole* app -- routers, client, retry
policy, error mapping -- against a mocked upstream.  No credentials needed;
they run in CI on every push.
"""

from __future__ import annotations

import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import REPO_PATH, load_fixture


@pytest.fixture
def retrying_client(settings: Settings):
    """The app with retries enabled and backoff neutered, so tests stay fast."""
    resilient = settings.model_copy(update={"github_max_retries": 2})
    app = create_app(resilient)
    with TestClient(app, raise_server_exceptions=False) as client:

        async def _no_sleep(_: float) -> None:
            return None

        app.state.github._sleep = _no_sleep
        yield client


# --- rate limiting ---------------------------------------------------------


def test_primary_rate_limit_becomes_429_with_retry_after(
    client: TestClient, gh: respx.MockRouter
) -> None:
    reset = int(time.time()) + 300
    gh.get(f"{REPO_PATH}/issues").mock(
        return_value=httpx.Response(
            403,
            json={
                "message": "API rate limit exceeded for user ID 583920.",
                "documentation_url": "https://docs.github.com/rest/overview/rate-limits-for-the-rest-api",
            },
            headers={
                "x-ratelimit-limit": "5000",
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": str(reset),
            },
        )
    )
    response = client.get("/issues")

    assert response.status_code == 429
    assert 290 <= int(response.headers["Retry-After"]) <= 300

    error = response.json()["error"]
    assert error["code"] == "rate_limited"
    assert "5000/hour" in error["message"]
    assert error["upstream_status"] == 403, "the caller can still see what GitHub actually said"


def test_secondary_rate_limit_is_also_429(client: TestClient, gh: respx.MockRouter) -> None:
    gh.post(f"{REPO_PATH}/issues").mock(
        return_value=httpx.Response(
            403,
            json={"message": "You have exceeded a secondary rate limit."},
            headers={"retry-after": "30", "x-ratelimit-remaining": "4000"},
        )
    )
    response = client.post("/issues", json={"title": "too fast"})

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "30"
    assert "secondary rate limit" in response.json()["error"]["message"]


def test_a_429_is_retried_and_can_succeed(
    retrying_client: TestClient, gh: respx.MockRouter
) -> None:
    route = gh.get(f"{REPO_PATH}/issues").mock(
        side_effect=[
            httpx.Response(429, json={"message": "slow down"}, headers={"retry-after": "1"}),
            httpx.Response(200, json=load_fixture("github_issue_list.json")),
        ]
    )
    response = retrying_client.get("/issues")

    assert response.status_code == 200
    assert route.call_count == 2


def test_a_read_survives_one_bad_gateway(retrying_client: TestClient, gh: respx.MockRouter) -> None:
    route = gh.get(f"{REPO_PATH}/issues/12").mock(
        side_effect=[
            httpx.Response(502, json={"message": "Bad gateway"}),
            httpx.Response(200, json=load_fixture("github_issue.json")),
        ]
    )
    response = retrying_client.get("/issues/12")

    assert response.status_code == 200
    assert response.json()["number"] == 12
    assert route.call_count == 2


def test_sustained_5xx_gives_up_with_502(retrying_client: TestClient, gh: respx.MockRouter) -> None:
    route = gh.get(f"{REPO_PATH}/issues").mock(
        return_value=httpx.Response(503, json={"message": "Service unavailable"})
    )
    response = retrying_client.get("/issues")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "github_unavailable"
    assert route.call_count == 3, "one attempt plus two retries"


def test_a_failed_write_is_not_retried(retrying_client: TestClient, gh: respx.MockRouter) -> None:
    """The safety property: a 502 on create must never produce two issues."""
    route = gh.post(f"{REPO_PATH}/issues").mock(
        return_value=httpx.Response(500, json={"message": "boom"})
    )
    response = retrying_client.post("/issues", json={"title": "only once"})

    assert response.status_code == 502
    assert route.call_count == 1


def test_upstream_timeout_is_504(client: TestClient, gh: respx.MockRouter) -> None:
    gh.get(f"{REPO_PATH}/issues").mock(side_effect=httpx.ReadTimeout("too slow"))
    response = client.get("/issues")

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "github_timeout"


def test_unreachable_github_is_502(client: TestClient, gh: respx.MockRouter) -> None:
    gh.get(f"{REPO_PATH}/issues").mock(side_effect=httpx.ConnectError("no route to host"))
    response = client.get("/issues")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "github_unreachable"
    assert "GITHUB_API_URL" in response.json()["error"]["message"]


# --- the gateway stays serviceable while GitHub is down --------------------


def test_healthz_stays_green_during_an_upstream_outage(
    client: TestClient, gh: respx.MockRouter
) -> None:
    gh.get(f"{REPO_PATH}/issues").mock(return_value=httpx.Response(503, json={"message": "down"}))
    assert client.get("/issues").status_code == 502
    assert client.get("/healthz").status_code == 200


def test_webhooks_are_still_accepted_during_an_upstream_outage(
    client: TestClient, deliver, gh: respx.MockRouter
) -> None:
    """Ingestion has no GitHub dependency, so an outage must not drop deliveries."""
    gh.get(f"{REPO_PATH}/issues").mock(return_value=httpx.Response(500, json={"message": "down"}))
    client.get("/issues")

    assert deliver(load_fixture("webhook_issues_opened.json")).status_code == 204
    assert client.get("/events").json()[0]["status"] == "processed"


# --- poison deliveries -----------------------------------------------------


def test_a_failing_background_task_parks_the_delivery(
    client: TestClient, deliver, monkeypatch
) -> None:
    """Post-ack failure must be recorded, not retried in-band or swallowed."""

    def explode(*_args, **_kwargs):
        raise RuntimeError("summariser blew up")

    monkeypatch.setattr("app.routers.webhook._summarize", explode)

    assert deliver(load_fixture("webhook_issues_opened.json")).status_code == 204

    stored = client.get("/events").json()[0]
    assert stored["status"] == "failed"
    assert "summariser blew up" in stored["error"]
    assert stored["attempts"] == 1


def test_a_parked_delivery_is_not_reprocessed_on_redelivery(
    client: TestClient, deliver, monkeypatch
) -> None:
    def explode(*_args, **_kwargs):
        raise RuntimeError("still broken")

    monkeypatch.setattr("app.routers.webhook._summarize", explode)
    payload = load_fixture("webhook_issues_opened.json")

    deliver(payload, delivery_id="poison-1")
    deliver(payload, delivery_id="poison-1")

    events = client.get("/events").json()
    assert len(events) == 1
    assert events[0]["attempts"] == 1, "a duplicate must not re-run the failing task"

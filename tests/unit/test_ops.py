"""Health, readiness, /events, and configuration handling."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import REPO_PATH, load_fixture

# --- /healthz --------------------------------------------------------------


def test_healthz_is_200_and_never_calls_github(client: TestClient, gh: respx.MockRouter) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["repo"] == "mikechau1/cmpe272-issues-gw"
    assert body["config"]["complete"] is True
    assert body["config"]["missing_env"] == []
    assert body["event_store"]["ok"] is True
    assert not gh.calls, "liveness must not depend on an upstream that can be down"


def test_healthz_reports_missing_configuration(gh: respx.MockRouter) -> None:
    with TestClient(
        create_app(Settings(event_store_path=":memory:", log_format="text", _env_file=None))
    ) as bare:
        body = bare.get("/healthz").json()
    assert body["status"] == "ok", "an unconfigured process is still alive"
    assert body["config"]["complete"] is False
    assert set(body["config"]["missing_env"]) == {
        "GITHUB_TOKEN",
        "GITHUB_OWNER",
        "GITHUB_REPO",
        "WEBHOOK_SECRET",
    }


def test_healthz_counts_stored_events(client: TestClient, deliver: Callable[..., Any]) -> None:
    deliver(load_fixture("webhook_issues_opened.json"))
    assert client.get("/healthz").json()["event_store"]["events"] == 1


# --- /readyz ---------------------------------------------------------------


def test_readyz_is_shallow_by_default(client: TestClient, gh: respx.MockRouter) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "missing_env": []}
    assert not gh.calls


def test_readyz_deep_checks_github(client: TestClient, gh: respx.MockRouter) -> None:
    gh.get(REPO_PATH).mock(
        return_value=httpx.Response(
            200,
            json={"full_name": "mikechau1/cmpe272-issues-gw"},
            headers={"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "4999"},
        )
    )
    body = client.get("/readyz", params={"deep": True}).json()
    assert body["status"] == "ok"
    assert body["github"]["reachable"] is True
    assert body["rate_limit"]["remaining"] == 4999


def test_readyz_deep_reports_a_bad_token(client: TestClient, gh: respx.MockRouter) -> None:
    gh.get(REPO_PATH).mock(return_value=httpx.Response(401, json={"message": "Bad credentials"}))
    response = client.get("/readyz", params={"deep": True})
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["github"]["error"] == "github_unauthorized"


def test_readyz_is_503_when_configuration_is_incomplete(gh: respx.MockRouter) -> None:
    with TestClient(
        create_app(Settings(event_store_path=":memory:", log_format="text", _env_file=None))
    ) as bare:
        response = bare.get("/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert not gh.calls


# --- /events ---------------------------------------------------------------


def test_events_starts_empty(client: TestClient) -> None:
    assert client.get("/events").json() == []


def test_events_returns_the_documented_fields(
    client: TestClient, deliver: Callable[..., Any]
) -> None:
    deliver(load_fixture("webhook_issues_opened.json"), delivery_id="abc")
    event = client.get("/events").json()[0]
    assert {"id", "event", "action", "issue_number", "timestamp"} <= set(event)
    assert event["event"] == "issues"
    assert event["action"] == "opened"
    assert event["issue_number"] == 12


def test_events_are_newest_first(client: TestClient, deliver: Callable[..., Any]) -> None:
    payload = load_fixture("webhook_issues_opened.json")
    deliver(payload, delivery_id="first")
    payload["action"] = "closed"
    deliver(payload, delivery_id="second")

    assert [event["delivery_id"] for event in client.get("/events").json()] == ["second", "first"]


def test_events_honours_the_limit(client: TestClient, deliver: Callable[..., Any]) -> None:
    payload = load_fixture("webhook_issues_opened.json")
    for index in range(4):
        deliver(payload, delivery_id=f"d-{index}")
    assert len(client.get("/events", params={"limit": 2}).json()) == 2


def test_events_filters_by_event_type(client: TestClient, deliver: Callable[..., Any]) -> None:
    deliver(load_fixture("webhook_issues_opened.json"), delivery_id="i")
    deliver(
        load_fixture("webhook_issue_comment_created.json"),
        event="issue_comment",
        delivery_id="c",
    )
    filtered = client.get("/events", params={"event": "issue_comment"}).json()
    assert [event["delivery_id"] for event in filtered] == ["c"]


# --- discovery -------------------------------------------------------------


def test_root_redirects_to_the_docs(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/docs"


def test_openapi_json_serves_the_hand_written_contract(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    assert spec["openapi"] == "3.1.0"
    assert spec["info"]["title"] == "GitHub Issues Gateway"
    assert "webhookSignature" in spec["components"]["securitySchemes"]


def test_openapi_yaml_is_downloadable(client: TestClient) -> None:
    response = client.get("/openapi.yaml")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/yaml")
    assert response.text.startswith("openapi: 3.1.0")


def test_docs_render(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200


# --- unexpected faults -----------------------------------------------------


def test_an_unhandled_exception_becomes_a_clean_500(
    client: TestClient, gh: respx.MockRouter, monkeypatch
) -> None:
    def explode(_: dict) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr("app.routers.issues.project_issue", explode)
    gh.get(f"{REPO_PATH}/issues/12").mock(
        return_value=httpx.Response(200, json=load_fixture("github_issue.json"))
    )

    response = client.get("/issues/12")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert "kaboom" not in response.text, "internal detail must not leak to the client"
    assert body["error"]["request_id"]

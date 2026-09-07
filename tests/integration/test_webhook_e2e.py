"""Real webhook deliveries, end to end through a public tunnel.

This is the only test that exercises the path GitHub actually uses: GitHub
signs a delivery, ships it over the internet to a tunnel, the tunnel forwards
it to a *running* gateway process, and the event shows up on `GET /events`.

It needs setup a test runner cannot do for itself (a tunnel, a webhook
configured on the repo), so it is opt-in:

    RUN_TUNNEL_TESTS=1 SERVICE_URL=http://localhost:8000 pytest -m tunnel

See the "Webhook setup" section of README.md.  `scripts/webhook_replay.sh`
covers the same assertions without a tunnel by replaying a signed payload.
"""

from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest

pytestmark = pytest.mark.tunnel

POLL_TIMEOUT_S = 45.0
POLL_INTERVAL_S = 1.5


@pytest.fixture(scope="module")
def tunnel_enabled() -> None:
    if os.environ.get("RUN_TUNNEL_TESTS") != "1":
        pytest.skip("set RUN_TUNNEL_TESTS=1 with a tunnel and webhook configured")


@pytest.fixture(scope="module")
def gateway(tunnel_enabled: None, service_url: str) -> httpx.Client:
    client = httpx.Client(base_url=service_url, timeout=10.0)
    try:
        health = client.get("/healthz")
    except httpx.HTTPError as exc:
        pytest.skip(f"no gateway reachable at {service_url}: {exc}")
    if health.status_code != 200:
        pytest.skip(f"gateway at {service_url} is unhealthy: {health.text}")
    return client


def _await_event(
    gateway: httpx.Client, *, event: str, action: str, issue_number: int
) -> dict | None:
    """Poll /events until the delivery lands, or the deadline passes."""
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        for stored in gateway.get("/events", params={"limit": 100}).json():
            if (
                stored["event"] == event
                and stored["action"] == action
                and stored["issue_number"] == issue_number
            ):
                return stored
        time.sleep(POLL_INTERVAL_S)
    return None


def test_creating_an_issue_delivers_an_issues_opened_event(
    gateway: httpx.Client, live, created_issues: list[int]
) -> None:
    response = live.post("/issues", json={"title": f"[tunnel] opened {uuid.uuid4().hex[:8]}"})
    assert response.status_code == 201
    number = response.json()["number"]
    created_issues.append(number)

    stored = _await_event(gateway, event="issues", action="opened", issue_number=number)
    assert stored is not None, (
        f"no issues/opened delivery for #{number} within {POLL_TIMEOUT_S}s -- "
        "check the tunnel, the repository webhook, and that WEBHOOK_SECRET matches"
    )
    assert stored["status"] == "processed"
    assert stored["delivery_id"]
    assert stored["sender"]


def test_commenting_delivers_an_issue_comment_event(
    gateway: httpx.Client, live, created_issues: list[int]
) -> None:
    created = live.post("/issues", json={"title": f"[tunnel] comment {uuid.uuid4().hex[:8]}"})
    number = created.json()["number"]
    created_issues.append(number)

    assert live.post(f"/issues/{number}/comments", json={"body": "hello"}).status_code == 201

    stored = _await_event(gateway, event="issue_comment", action="created", issue_number=number)
    assert stored is not None, f"no issue_comment/created delivery for #{number}"
    assert stored["status"] == "processed"


def test_closing_delivers_an_issues_closed_event(
    gateway: httpx.Client, live, created_issues: list[int]
) -> None:
    created = live.post("/issues", json={"title": f"[tunnel] closed {uuid.uuid4().hex[:8]}"})
    number = created.json()["number"]
    created_issues.append(number)

    assert live.patch(f"/issues/{number}", json={"state": "closed"}).status_code == 200

    stored = _await_event(gateway, event="issues", action="closed", issue_number=number)
    assert stored is not None, f"no issues/closed delivery for #{number}"


def test_redelivery_from_the_github_ui_is_idempotent(
    gateway: httpx.Client, live, created_issues: list[int]
) -> None:
    """Redeliver the same event from Settings -> Webhooks -> Recent Deliveries.

    The assertion is the invariant, not the click: whatever arrives twice with
    one delivery GUID must occupy exactly one row.
    """
    created = live.post("/issues", json={"title": f"[tunnel] dedupe {uuid.uuid4().hex[:8]}"})
    number = created.json()["number"]
    created_issues.append(number)

    assert _await_event(gateway, event="issues", action="opened", issue_number=number) is not None

    events = gateway.get("/events", params={"limit": 200}).json()
    keys = [(e["delivery_id"], e["event"], e["action"]) for e in events]
    assert len(keys) == len(set(keys)), "the store must hold one row per delivery+action"

"""Webhook signature verification and delivery handling.

Signature checks come first here because they gate everything else: an
unverified body is attacker-controlled input, so a failure at this layer is
the difference between a webhook receiver and an open write endpoint.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.security import compute_signature, verify_signature
from tests.conftest import TEST_SECRET, load_fixture


@pytest.fixture
def issue_payload() -> dict[str, Any]:
    return load_fixture("webhook_issues_opened.json")


@pytest.fixture
def comment_payload() -> dict[str, Any]:
    return load_fixture("webhook_issue_comment_created.json")


# ---------------------------------------------------------------------------
# The primitive
# ---------------------------------------------------------------------------


def test_compute_signature_matches_the_reference_construction() -> None:
    body = b'{"action":"opened"}'
    expected = hmac.new(TEST_SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert compute_signature(TEST_SECRET, body) == f"sha256={expected}"


def test_verify_accepts_a_correct_signature() -> None:
    body = b'{"action":"opened","issue":{"number":12}}'
    assert verify_signature(TEST_SECRET, body, compute_signature(TEST_SECRET, body))


def test_verify_accepts_bytes_and_str_secrets_alike() -> None:
    body = b"payload"
    assert verify_signature(TEST_SECRET.encode(), body, compute_signature(TEST_SECRET, body))


def test_verify_rejects_a_tampered_body() -> None:
    original = b'{"action":"opened","issue":{"number":12}}'
    signature = compute_signature(TEST_SECRET, original)
    tampered = original.replace(b'"number":12', b'"number":99')
    assert not verify_signature(TEST_SECRET, tampered, signature)


def test_verify_rejects_a_signature_from_a_different_secret() -> None:
    body = b"payload"
    assert not verify_signature(TEST_SECRET, body, compute_signature("other-secret", body))


def test_verify_rejects_a_single_flipped_hex_digit() -> None:
    body = b"payload"
    good = compute_signature(TEST_SECRET, body)
    flipped = good[:-1] + ("0" if good[-1] != "0" else "1")
    assert not verify_signature(TEST_SECRET, body, flipped)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "   ",
        "deadbeef",  # no scheme
        "sha1=" + "a" * 40,  # wrong algorithm
        "sha256=",  # empty digest
        "sha256=" + "a" * 63,  # too short
        "sha256=" + "a" * 65,  # too long
        "sha256=" + "z" * 64,  # not hex
    ],
)
def test_verify_rejects_malformed_headers(header: str | None) -> None:
    assert not verify_signature(TEST_SECRET, b"payload", header)


def test_verify_is_case_insensitive_about_the_digest() -> None:
    body = b"payload"
    upper = compute_signature(TEST_SECRET, body).upper().replace("SHA256=", "sha256=")
    assert verify_signature(TEST_SECRET, body, upper)


def test_verify_rejects_everything_when_the_secret_is_empty() -> None:
    body = b"payload"
    assert not verify_signature("", body, compute_signature("", body))


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


def test_valid_delivery_is_acked_with_204(deliver: Callable[..., Any], issue_payload: dict) -> None:
    response = deliver(issue_payload)
    assert response.status_code == 204
    assert not response.content


def test_invalid_signature_is_401(deliver: Callable[..., Any], issue_payload: dict) -> None:
    response = deliver(issue_payload, signature="sha256=" + "0" * 64)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_signature"


def test_missing_signature_is_401(deliver: Callable[..., Any], issue_payload: dict) -> None:
    response = deliver(issue_payload, signature="")
    assert response.status_code == 401


def test_tampered_body_is_401(
    client: TestClient, sign: Callable[[bytes], str], issue_payload: dict
) -> None:
    """Sign one body, send another -- the classic replay-with-edits attack."""
    signed = json.dumps(issue_payload).encode()
    issue_payload["issue"]["number"] = 9999
    tampered = json.dumps(issue_payload).encode()

    response = client.post(
        "/webhook",
        content=tampered,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "tampered-1",
            "X-Hub-Signature-256": sign(signed),
        },
    )
    assert response.status_code == 401
    assert client.get("/events").json() == []


def test_rejected_delivery_is_not_stored(
    client: TestClient, deliver: Callable[..., Any], issue_payload: dict
) -> None:
    deliver(issue_payload, signature="sha256=" + "1" * 64)
    assert client.get("/events").json() == []


def test_signature_is_verified_before_the_event_header(
    client: TestClient, issue_payload: dict
) -> None:
    """An unsigned request with a bogus event must be 401, never 400.

    Answering 400 first would confirm to an unauthenticated caller which
    events the service handles.
    """
    response = client.post(
        "/webhook",
        content=json.dumps(issue_payload).encode(),
        headers={"X-GitHub-Event": "not-a-real-event", "X-Hub-Signature-256": "sha256=" + "0" * 64},
    )
    assert response.status_code == 401


def test_secret_and_signature_never_appear_in_the_error_body(
    deliver: Callable[..., Any], issue_payload: dict
) -> None:
    bad = "sha256=" + "ab" * 32
    text = deliver(issue_payload, signature=bad).text
    assert TEST_SECRET not in text
    assert "ab" * 32 not in text


# --- events and actions ----------------------------------------------------


def test_ping_is_accepted(deliver: Callable[..., Any], client: TestClient) -> None:
    response = deliver(load_fixture("webhook_ping.json"), event="ping", delivery_id="ping-1")
    assert response.status_code == 204
    events = client.get("/events").json()
    assert events[0]["event"] == "ping"
    assert events[0]["action"] is None


def test_issue_comment_is_accepted(
    deliver: Callable[..., Any], client: TestClient, comment_payload: dict
) -> None:
    assert deliver(comment_payload, event="issue_comment", delivery_id="c-1").status_code == 204
    stored = client.get("/events").json()[0]
    assert (stored["event"], stored["action"], stored["issue_number"]) == (
        "issue_comment",
        "created",
        12,
    )


def test_unknown_event_is_400(deliver: Callable[..., Any], issue_payload: dict) -> None:
    response = deliver(issue_payload, event="push")
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "unsupported_event"
    assert body["error"]["details"]["event"] == "push"


def test_missing_event_header_is_400(
    client: TestClient, sign: Callable[[bytes], str], issue_payload: dict
) -> None:
    body = json.dumps(issue_payload).encode()
    response = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body)})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_event"


def test_unknown_action_is_400(deliver: Callable[..., Any], issue_payload: dict) -> None:
    issue_payload["action"] = "exploded"
    response = deliver(issue_payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_action"


def test_missing_action_is_400(deliver: Callable[..., Any], issue_payload: dict) -> None:
    del issue_payload["action"]
    response = deliver(issue_payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_action"


@pytest.mark.parametrize("action", ["opened", "closed", "reopened", "edited", "labeled", "deleted"])
def test_all_documented_issue_actions_are_accepted(
    deliver: Callable[..., Any], issue_payload: dict, action: str
) -> None:
    issue_payload["action"] = action
    assert deliver(issue_payload, delivery_id=f"d-{action}").status_code == 204


def test_malformed_json_with_a_valid_signature_is_400(
    client: TestClient, sign: Callable[[bytes], str]
) -> None:
    body = b"{definitely not json"
    response = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": sign(body)},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json"


def test_non_object_json_is_400(client: TestClient, sign: Callable[[bytes], str]) -> None:
    body = b"[1, 2, 3]"
    response = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": sign(body)},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json"


def test_oversized_body_is_413(client: TestClient, sign: Callable[[bytes], str]) -> None:
    body = b'{"action":"opened","pad":"' + b"x" * (5 * 1024 * 1024) + b'"}'
    response = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": sign(body)},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_webhook_is_503_without_a_configured_secret(issue_payload: dict, settings) -> None:
    """Unsigned traffic is refused outright rather than trusted."""
    from app.main import create_app

    settings.webhook_secret = ""
    with TestClient(create_app(settings)) as unconfigured:
        response = unconfigured.post(
            "/webhook",
            content=json.dumps(issue_payload).encode(),
            headers={"X-GitHub-Event": "issues"},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "not_configured"


# --- idempotency -----------------------------------------------------------


def test_redelivery_is_idempotent(
    deliver: Callable[..., Any], client: TestClient, issue_payload: dict
) -> None:
    """GitHub retries and the "Redeliver" button both reuse the delivery GUID."""
    for _ in range(3):
        assert deliver(issue_payload, delivery_id="same-guid").status_code == 204

    events = client.get("/events").json()
    assert len(events) == 1
    assert events[0]["delivery_id"] == "same-guid"


def test_same_delivery_id_with_a_different_action_is_a_distinct_event(
    deliver: Callable[..., Any], client: TestClient, issue_payload: dict
) -> None:
    """The dedupe key is (delivery_id, event, action), not delivery_id alone."""
    deliver(issue_payload, delivery_id="guid-x")
    issue_payload["action"] = "closed"
    deliver(issue_payload, delivery_id="guid-x")

    actions = {event["action"] for event in client.get("/events").json()}
    assert actions == {"opened", "closed"}


def test_missing_delivery_header_falls_back_to_a_content_hash(
    client: TestClient, sign: Callable[[bytes], str], issue_payload: dict
) -> None:
    body = json.dumps(issue_payload).encode()
    headers = {"X-GitHub-Event": "issues", "X-Hub-Signature-256": sign(body)}

    assert client.post("/webhook", content=body, headers=headers).status_code == 204
    assert client.post("/webhook", content=body, headers=headers).status_code == 204

    events = client.get("/events").json()
    assert len(events) == 1, "identical bodies must dedupe even without a delivery GUID"
    assert events[0]["delivery_id"].startswith("sha256:")


def test_delivery_is_processed_in_the_background(
    deliver: Callable[..., Any], client: TestClient, issue_payload: dict
) -> None:
    deliver(issue_payload)
    stored = client.get("/events").json()[0]
    assert stored["status"] == "processed"
    assert stored["processed_at"] is not None
    assert stored["attempts"] == 1


def test_stored_event_captures_the_summary_fields(
    deliver: Callable[..., Any], client: TestClient, issue_payload: dict
) -> None:
    deliver(issue_payload, delivery_id="abc-123")
    stored = client.get("/events").json()[0]
    assert stored["delivery_id"] == "abc-123"
    assert stored["event"] == "issues"
    assert stored["action"] == "opened"
    assert stored["issue_number"] == 12
    assert stored["repository"] == "mikechau1/cmpe272-issues-gw"
    assert stored["sender"] == "mikechau1"
    assert stored["timestamp"].endswith("Z")

"""Route validation: bad input must be rejected before any GitHub call.

Every case here asserts two things -- the status is 400 (not FastAPI's default
422, which the contract does not describe) and ``respx`` saw no upstream
traffic, i.e. we did not spend rate-limit budget rejecting garbage.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.conftest import REPO_PATH, load_fixture


def _assert_rejected(response: httpx.Response, field: str | None = None) -> dict:
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["status"] == 400
    assert body["error"]["request_id"]
    if field is not None:
        fields = [detail["field"] for detail in body["error"]["details"]]
        assert field in fields, fields
    return body


# --- POST /issues ----------------------------------------------------------


def test_create_issue_missing_title(client: TestClient, gh: respx.MockRouter) -> None:
    body = _assert_rejected(client.post("/issues", json={"body": "no title"}), "body.title")
    assert "title" in body["error"]["message"]
    assert not gh.calls, "rejected requests must not reach GitHub"


@pytest.mark.parametrize("title", ["", "   ", "\n\t "])
def test_create_issue_blank_title(client: TestClient, gh: respx.MockRouter, title: str) -> None:
    _assert_rejected(client.post("/issues", json={"title": title}), "body.title")
    assert not gh.calls


def test_create_issue_title_too_long(client: TestClient, gh: respx.MockRouter) -> None:
    _assert_rejected(client.post("/issues", json={"title": "x" * 257}), "body.title")
    assert not gh.calls


def test_create_issue_rejects_unknown_field(client: TestClient, gh: respx.MockRouter) -> None:
    response = client.post("/issues", json={"title": "ok", "assignee": "somebody"})
    _assert_rejected(response, "body.assignee")
    assert not gh.calls


def test_create_issue_rejects_blank_label(client: TestClient, gh: respx.MockRouter) -> None:
    _assert_rejected(client.post("/issues", json={"title": "ok", "labels": ["bug", " "]}))
    assert not gh.calls


def test_create_issue_rejects_wrong_types(client: TestClient, gh: respx.MockRouter) -> None:
    _assert_rejected(client.post("/issues", json={"title": 12, "labels": "bug"}))
    assert not gh.calls


def test_create_issue_rejects_malformed_json(client: TestClient, gh: respx.MockRouter) -> None:
    response = client.post(
        "/issues", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
    assert not gh.calls


# --- GET /issues -----------------------------------------------------------


def test_list_rejects_invalid_state(client: TestClient, gh: respx.MockRouter) -> None:
    _assert_rejected(client.get("/issues", params={"state": "sideways"}), "query.state")
    assert not gh.calls


@pytest.mark.parametrize("per_page", [0, -1, 101, 1000])
def test_list_rejects_out_of_range_per_page(
    client: TestClient, gh: respx.MockRouter, per_page: int
) -> None:
    _assert_rejected(client.get("/issues", params={"per_page": per_page}), "query.per_page")
    assert not gh.calls


def test_list_rejects_page_zero(client: TestClient, gh: respx.MockRouter) -> None:
    _assert_rejected(client.get("/issues", params={"page": 0}), "query.page")
    assert not gh.calls


def test_list_rejects_unknown_sort(client: TestClient, gh: respx.MockRouter) -> None:
    _assert_rejected(client.get("/issues", params={"sort": "priority"}), "query.sort")
    assert not gh.calls


def test_list_accepts_documented_defaults(client: TestClient, gh: respx.MockRouter) -> None:
    route = gh.get(f"{REPO_PATH}/issues").mock(
        return_value=httpx.Response(200, json=load_fixture("github_issue_list.json"))
    )
    assert client.get("/issues").status_code == 200
    sent = route.calls.last.request.url
    assert sent.params["state"] == "open"
    assert sent.params["per_page"] == "30"
    assert sent.params["page"] == "1"


# --- GET/PATCH /issues/{number} -------------------------------------------


@pytest.mark.parametrize("number", ["abc", "0", "-3", "1.5"])
def test_issue_number_must_be_a_positive_integer(
    client: TestClient, gh: respx.MockRouter, number: str
) -> None:
    _assert_rejected(client.get(f"/issues/{number}"), "path.number")
    assert not gh.calls


def test_patch_rejects_invalid_state(client: TestClient, gh: respx.MockRouter) -> None:
    _assert_rejected(client.patch("/issues/12", json={"state": "deleted"}), "body.state")
    assert not gh.calls


def test_patch_rejects_empty_body(client: TestClient, gh: respx.MockRouter) -> None:
    body = _assert_rejected(client.patch("/issues/12", json={}))
    assert "at least one of" in body["error"]["message"]
    assert not gh.calls


def test_patch_rejects_unknown_field(client: TestClient, gh: respx.MockRouter) -> None:
    _assert_rejected(client.patch("/issues/12", json={"milestone": 3}), "body.milestone")
    assert not gh.calls


def test_patch_rejects_blank_title(client: TestClient, gh: respx.MockRouter) -> None:
    _assert_rejected(client.patch("/issues/12", json={"title": "  "}), "body.title")
    assert not gh.calls


def test_patch_forwards_only_supplied_fields(client: TestClient, gh: respx.MockRouter) -> None:
    """`exclude_unset`: an absent key is untouched, an explicit null is a clear."""
    route = gh.patch(f"{REPO_PATH}/issues/12").mock(
        return_value=httpx.Response(200, json=load_fixture("github_issue.json"))
    )
    assert client.patch("/issues/12", json={"body": None}).status_code == 200
    import json as _json

    assert _json.loads(route.calls.last.request.content) == {"body": None}


# --- POST /issues/{number}/comments ---------------------------------------


@pytest.mark.parametrize("body", ["", "   "])
def test_comment_rejects_blank_body(client: TestClient, gh: respx.MockRouter, body: str) -> None:
    _assert_rejected(client.post("/issues/12/comments", json={"body": body}), "body.body")
    assert not gh.calls


def test_comment_requires_body_field(client: TestClient, gh: respx.MockRouter) -> None:
    _assert_rejected(client.post("/issues/12/comments", json={}), "body.body")
    assert not gh.calls


# --- GET /events -----------------------------------------------------------


def test_events_rejects_unknown_event_filter(client: TestClient) -> None:
    response = client.get("/events", params={"event": "push"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_event"


def test_events_rejects_out_of_range_limit(client: TestClient) -> None:
    _assert_rejected(client.get("/events", params={"limit": 9999}), "query.limit")


# --- method / route semantics ---------------------------------------------


def test_unknown_route_uses_the_error_envelope(client: TestClient) -> None:
    response = client.get("/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_wrong_method_uses_the_error_envelope(client: TestClient) -> None:
    response = client.delete("/issues/12")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"

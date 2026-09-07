"""End-to-end against the real GitHub Issues API.

Run with credentials present:

    make test-integration          # or: pytest -m integration

Each test creates real issues in `GITHUB_OWNER/GITHUB_REPO` and closes them on
teardown.  Point these at a throwaway repository, never a real one.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from tests.integration.conftest import TITLE_PREFIX

pytestmark = pytest.mark.integration

# GitHub's *list* endpoints are eventually consistent: an issue is readable at
# /issues/{number} immediately, but can take a beat to appear in a filtered
# listing. Conditional GET widens that window, because a 304 means "GitHub's
# cached view is unchanged", not "the repository is unchanged". Assertions
# about listings therefore poll instead of demanding read-your-writes.
CONSISTENCY_TIMEOUT_S = 30.0
CONSISTENCY_INTERVAL_S = 2.0


def _unique(label: str) -> str:
    return f"{label} {uuid.uuid4().hex[:8]}"


def _eventually(predicate: Callable[[], bool], *, what: str) -> None:
    deadline = time.monotonic() + CONSISTENCY_TIMEOUT_S
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(CONSISTENCY_INTERVAL_S)
    raise AssertionError(f"{what} did not become true within {CONSISTENCY_TIMEOUT_S}s")


# --- required scenario 1: create -> read -----------------------------------


def test_create_then_get_round_trips(live: TestClient, new_issue) -> None:
    created = new_issue(_unique("create-then-get"), body="Created by the integration suite.")

    assert created["state"] == "open"
    assert created["html_url"].startswith("https://github.com/")

    fetched = live.get(f"/issues/{created['number']}")
    assert fetched.status_code == 200
    assert fetched.json()["number"] == created["number"]
    assert fetched.json()["title"] == created["title"]
    assert fetched.json()["body"] == "Created by the integration suite."


def test_create_returns_a_usable_location_header(live: TestClient, created_issues) -> None:
    response = live.post("/issues", json={"title": f"{TITLE_PREFIX} {_unique('location')}"})
    assert response.status_code == 201
    created_issues.append(response.json()["number"])

    location = response.headers["Location"]
    assert location == f"/issues/{response.json()['number']}"
    assert live.get(location).status_code == 200, "the Location header must be followable"


def test_create_with_labels(live: TestClient, new_issue) -> None:
    issue = new_issue(_unique("labelled"), labels=["bug"])
    assert "bug" in [label["name"] for label in issue["labels"]]


# --- required scenario 2: update, close, reopen ----------------------------


def test_update_title_and_body(live: TestClient, new_issue) -> None:
    issue = new_issue(_unique("editable"), body="first draft")
    number = issue["number"]

    renamed = f"{TITLE_PREFIX} {_unique('renamed')}"
    response = live.patch(f"/issues/{number}", json={"title": renamed, "body": "second draft"})

    assert response.status_code == 200
    assert response.json()["title"] == renamed
    assert response.json()["body"] == "second draft"
    assert live.get(f"/issues/{number}").json()["title"] == renamed


def test_close_then_reopen(live: TestClient, new_issue) -> None:
    """Closing is this API's "delete"; it must be reversible."""
    number = new_issue(_unique("close-reopen"))["number"]

    closed = live.patch(f"/issues/{number}", json={"state": "closed", "state_reason": "completed"})
    assert closed.status_code == 200
    assert closed.json()["state"] == "closed"
    assert closed.json()["closed_at"] is not None

    reopened = live.patch(f"/issues/{number}", json={"state": "open"})
    assert reopened.status_code == 200
    assert reopened.json()["state"] == "open"
    assert reopened.json()["closed_at"] is None


def test_closed_issue_appears_under_the_right_filter(live: TestClient, new_issue) -> None:
    number = new_issue(_unique("filtered"))["number"]
    live.patch(f"/issues/{number}", json={"state": "closed"})

    def _numbers(state: str) -> list[int]:
        response = live.get("/issues", params={"state": state, "per_page": 100})
        assert response.status_code == 200
        return [issue["number"] for issue in response.json()]

    _eventually(lambda: number in _numbers("closed"), what=f"#{number} listed as closed")
    assert number not in _numbers("open")


# --- required scenario 3: comment -> list comments -------------------------


def test_comment_then_list_comments(live: TestClient, new_issue) -> None:
    number = new_issue(_unique("commentable"))["number"]
    text = f"Integration comment {uuid.uuid4().hex[:8]}"

    created = live.post(f"/issues/{number}/comments", json={"body": text})
    assert created.status_code == 201
    assert created.headers["Location"] == f"/issues/{number}/comments"

    comment = created.json()
    assert comment["body"] == text
    assert comment["user"]["login"]
    assert comment["html_url"].startswith("https://github.com/")

    listed = live.get(f"/issues/{number}/comments")
    assert listed.status_code == 200
    assert comment["id"] in [item["id"] for item in listed.json()]


def test_comment_count_reflects_on_the_issue(live: TestClient, new_issue) -> None:
    number = new_issue(_unique("counted"))["number"]
    live.post(f"/issues/{number}/comments", json={"body": "one"})
    assert live.get(f"/issues/{number}").json()["comments"] >= 1


# --- listing, pagination, conditional GET ----------------------------------


def test_list_returns_open_issues_by_default(live: TestClient, new_issue) -> None:
    number = new_issue(_unique("listed"))["number"]

    def _listed() -> bool:
        response = live.get("/issues", params={"per_page": 100})
        assert response.status_code == 200
        assert response.headers["X-Per-Page"] == "100"
        return number in [issue["number"] for issue in response.json()]

    _eventually(_listed, what=f"#{number} listed among open issues")


def test_pagination_produces_a_followable_link(live: TestClient, new_issue) -> None:
    """With per_page=1 any repo holding two open issues must paginate."""
    for index in range(2):
        new_issue(_unique(f"paged-{index}"))

    _eventually(
        lambda: len(live.get("/issues", params={"per_page": 100}).json()) >= 2,
        what="at least two open issues listed",
    )

    first = live.get("/issues", params={"per_page": 1, "page": 1})
    assert first.status_code == 200
    assert len(first.json()) == 1

    link = first.headers.get("Link")
    assert link and 'rel="next"' in link
    assert "api.github.com" not in link, "clients must be handed our URLs, not GitHub's"

    second = live.get("/issues", params={"per_page": 1, "page": 2})
    assert second.status_code == 200
    assert second.json()[0]["number"] != first.json()[0]["number"]


def test_rate_limit_headers_are_forwarded(live: TestClient) -> None:
    response = live.get("/issues", params={"per_page": 1})
    assert int(response.headers["X-RateLimit-Remaining"]) >= 0
    assert int(response.headers["X-RateLimit-Limit"]) > 0


def test_conditional_get_saves_rate_limit_budget(live: TestClient) -> None:
    """Extra credit: a matching If-None-Match must cost no quota."""
    first = live.get("/issues", params={"per_page": 5})
    assert first.status_code == 200
    etag = first.headers["ETag"]
    remaining_before = int(first.headers["X-RateLimit-Remaining"])

    second = live.get("/issues", params={"per_page": 5}, headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert not second.content

    remaining_after = int(second.headers["X-RateLimit-Remaining"])
    assert remaining_after == remaining_before, "a 304 must not consume rate-limit budget"
    assert second.headers["X-Cache"] == "HIT"


# --- error paths against the real API --------------------------------------


def test_unknown_issue_is_404(live: TestClient) -> None:
    response = live.get("/issues/99999999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert response.json()["error"]["upstream_status"] == 404


def test_github_validation_error_maps_to_400(live: TestClient, new_issue) -> None:
    """GitHub rejects an unknown state_reason with 422; we answer 400."""
    number = new_issue(_unique("bad-reason"))["number"]
    response = live.patch(f"/issues/{number}", json={"state": "closed", "state_reason": "reopened"})

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["error"]["upstream_status"] in (400, 422)


def test_a_bad_token_is_reported_as_401(live_settings) -> None:
    from app.main import create_app

    broken = live_settings.model_copy(update={"github_token": "github_pat_definitely_invalid"})
    with TestClient(create_app(broken), raise_server_exceptions=False) as client:
        response = client.get("/issues")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "github_unauthorized"
    assert "GITHUB_TOKEN" in response.json()["error"]["message"]


def test_readyz_deep_confirms_the_configuration(live: TestClient) -> None:
    response = live.get("/readyz", params={"deep": True})
    assert response.status_code == 200
    body = response.json()
    assert body["github"]["reachable"] is True
    assert body["rate_limit"]["remaining"] > 0

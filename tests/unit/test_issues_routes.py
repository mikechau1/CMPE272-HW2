"""Route behaviour on the happy paths: status codes, headers, projections."""

from __future__ import annotations

import json

import httpx
import respx
from fastapi.testclient import TestClient

from app.pagination import parse_link_header
from tests.conftest import REPO_PATH, load_fixture

LINK_HEADER = (
    '<https://api.github.com/repositories/889012345/issues?state=open&page=2>; rel="next", '
    '<https://api.github.com/repositories/889012345/issues?state=open&page=5>; rel="last"'
)


# --- POST /issues ----------------------------------------------------------


def test_create_returns_201_with_a_location_header(
    client: TestClient, gh: respx.MockRouter
) -> None:
    route = gh.post(f"{REPO_PATH}/issues").mock(
        return_value=httpx.Response(201, json=load_fixture("github_issue.json"))
    )
    response = client.post(
        "/issues", json={"title": "Broken thing", "body": "details", "labels": ["bug"]}
    )

    assert response.status_code == 201
    assert response.headers["Location"] == "/issues/12"

    sent = json.loads(route.calls.last.request.content)
    assert sent == {"title": "Broken thing", "body": "details", "labels": ["bug"]}


def test_create_projects_the_issue_onto_the_documented_shape(
    client: TestClient, gh: respx.MockRouter
) -> None:
    gh.post(f"{REPO_PATH}/issues").mock(
        return_value=httpx.Response(201, json=load_fixture("github_issue.json"))
    )
    body = client.post("/issues", json={"title": "x"}).json()

    assert set(body) == {
        "number", "id", "title", "body", "state", "state_reason", "labels", "user",
        "assignees", "comments", "locked", "html_url", "created_at", "updated_at", "closed_at",
    }  # fmt: skip
    assert body["labels"] == [
        {"name": "bug", "color": "d73a4a", "description": "Something isn't working"},
        {"name": "gateway", "color": "0e8a16", "description": None},
    ]
    assert body["user"]["login"] == "mikechau1"
    assert "node_id" not in body, "upstream churn must not leak through the projection"


def test_create_omits_absent_optional_fields_upstream(
    client: TestClient, gh: respx.MockRouter
) -> None:
    route = gh.post(f"{REPO_PATH}/issues").mock(
        return_value=httpx.Response(201, json=load_fixture("github_issue.json"))
    )
    client.post("/issues", json={"title": "just a title"})
    assert json.loads(route.calls.last.request.content) == {"title": "just a title"}


def test_create_trims_the_title(client: TestClient, gh: respx.MockRouter) -> None:
    route = gh.post(f"{REPO_PATH}/issues").mock(
        return_value=httpx.Response(201, json=load_fixture("github_issue.json"))
    )
    client.post("/issues", json={"title": "  padded  "})
    assert json.loads(route.calls.last.request.content)["title"] == "padded"


# --- GET /issues -----------------------------------------------------------


def test_list_returns_200_and_filters_out_pull_requests(
    client: TestClient, gh: respx.MockRouter
) -> None:
    gh.get(f"{REPO_PATH}/issues").mock(
        return_value=httpx.Response(200, json=load_fixture("github_issue_list.json"))
    )
    response = client.get("/issues")

    assert response.status_code == 200
    numbers = [issue["number"] for issue in response.json()]
    assert numbers == [12, 11], "#10 is a pull request and must not appear"


def test_list_forwards_filters_upstream(client: TestClient, gh: respx.MockRouter) -> None:
    route = gh.get(f"{REPO_PATH}/issues").mock(return_value=httpx.Response(200, json=[]))
    client.get(
        "/issues",
        params={
            "state": "all",
            "labels": "bug,gateway",
            "page": 3,
            "per_page": 5,
            "sort": "updated",
        },
    )
    params = dict(route.calls.last.request.url.params)
    assert params == {
        "state": "all",
        "labels": "bug,gateway",
        "page": "3",
        "per_page": "5",
        "sort": "updated",
    }


def test_list_rewrites_the_link_header_to_point_at_us(
    client: TestClient, gh: respx.MockRouter
) -> None:
    gh.get(f"{REPO_PATH}/issues").mock(
        return_value=httpx.Response(200, json=[], headers={"link": LINK_HEADER})
    )
    response = client.get("/issues", params={"state": "open", "labels": "bug"})

    links = parse_link_header(response.headers["Link"])
    assert set(links) == {"next", "last"}
    for url in links.values():
        assert "api.github.com" not in url
        assert "/issues?" in url
    assert "labels=bug" in links["next"]
    assert "page=2" in links["next"]


def test_list_exposes_page_headers(client: TestClient, gh: respx.MockRouter) -> None:
    gh.get(f"{REPO_PATH}/issues").mock(return_value=httpx.Response(200, json=[]))
    response = client.get("/issues", params={"page": 2, "per_page": 7})
    assert response.headers["X-Page"] == "2"
    assert response.headers["X-Per-Page"] == "7"


def test_list_forwards_rate_limit_headers(client: TestClient, gh: respx.MockRouter) -> None:
    gh.get(f"{REPO_PATH}/issues").mock(
        return_value=httpx.Response(
            200,
            json=[],
            headers={"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "4321"},
        )
    )
    response = client.get("/issues")
    assert response.headers["X-RateLimit-Limit"] == "5000"
    assert response.headers["X-RateLimit-Remaining"] == "4321"


def test_list_omits_the_link_header_when_there_is_one_page(
    client: TestClient, gh: respx.MockRouter
) -> None:
    gh.get(f"{REPO_PATH}/issues").mock(return_value=httpx.Response(200, json=[]))
    assert "Link" not in client.get("/issues").headers


# --- conditional GET -------------------------------------------------------


def test_list_returns_an_etag_a_client_can_replay(client: TestClient, gh: respx.MockRouter) -> None:
    gh.get(f"{REPO_PATH}/issues").mock(
        return_value=httpx.Response(
            200, json=load_fixture("github_issue_list.json"), headers={"etag": 'W/"abc123"'}
        )
    )
    first = client.get("/issues")
    etag = first.headers["ETag"]

    assert etag.startswith('W/"gw1-'), (
        "our ETag is versioned by the projection, not GitHub's raw tag"
    )
    assert first.headers["X-Cache"] == "MISS"

    second = client.get("/issues", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert not second.content
    assert second.headers["ETag"] == etag


def test_client_etag_matches_weakly(client: TestClient, gh: respx.MockRouter) -> None:
    gh.get(f"{REPO_PATH}/issues").mock(
        return_value=httpx.Response(200, json=[], headers={"etag": 'W/"abc123"'})
    )
    etag = client.get("/issues").headers["ETag"]
    strong = etag.removeprefix("W/")
    assert client.get("/issues", headers={"If-None-Match": strong}).status_code == 304


def test_star_if_none_match_is_honoured(client: TestClient, gh: respx.MockRouter) -> None:
    gh.get(f"{REPO_PATH}/issues").mock(
        return_value=httpx.Response(200, json=[], headers={"etag": 'W/"abc"'})
    )
    assert client.get("/issues", headers={"If-None-Match": "*"}).status_code == 304


def test_stale_client_etag_gets_a_fresh_200(client: TestClient, gh: respx.MockRouter) -> None:
    gh.get(f"{REPO_PATH}/issues").mock(
        return_value=httpx.Response(200, json=[{"number": 1}], headers={"etag": 'W/"new"'})
    )
    response = client.get("/issues", headers={"If-None-Match": 'W/"gw1-old"'})
    assert response.status_code == 200
    assert [issue["number"] for issue in response.json()] == [1]


def test_x_cache_reports_a_conditional_hit(client: TestClient, gh: respx.MockRouter) -> None:
    """Second call is a 304 upstream, served from the client's own ETag cache."""
    gh.get(f"{REPO_PATH}/issues").mock(
        side_effect=[
            httpx.Response(200, json=[], headers={"etag": 'W/"v1"'}),
            httpx.Response(304, headers={"etag": 'W/"v1"'}),
        ]
    )
    client.get("/issues")
    assert client.get("/issues").headers["X-Cache"] == "HIT"


# --- GET /issues/{number} --------------------------------------------------


def test_get_issue_returns_200(client: TestClient, gh: respx.MockRouter) -> None:
    gh.get(f"{REPO_PATH}/issues/12").mock(
        return_value=httpx.Response(200, json=load_fixture("github_issue.json"))
    )
    body = client.get("/issues/12").json()
    assert body["number"] == 12
    assert body["title"] == "Rate limiter drops the Retry-After header"


def test_get_issue_404s_for_a_pull_request(client: TestClient, gh: respx.MockRouter) -> None:
    pull_request = load_fixture("github_issue_list.json")[2]
    gh.get(f"{REPO_PATH}/issues/10").mock(return_value=httpx.Response(200, json=pull_request))

    response = client.get("/issues/10")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_an_issue"


# --- PATCH /issues/{number} ------------------------------------------------


def test_close_issue_is_the_delete_operation(client: TestClient, gh: respx.MockRouter) -> None:
    closed = load_fixture("github_issue.json") | {
        "state": "closed",
        "state_reason": "completed",
        "closed_at": "2026-09-06T20:15:03Z",
    }
    route = gh.patch(f"{REPO_PATH}/issues/12").mock(return_value=httpx.Response(200, json=closed))

    response = client.patch("/issues/12", json={"state": "closed", "state_reason": "completed"})
    assert response.status_code == 200
    assert response.json()["state"] == "closed"
    assert json.loads(route.calls.last.request.content) == {
        "state": "closed",
        "state_reason": "completed",
    }


def test_reopen_issue(client: TestClient, gh: respx.MockRouter) -> None:
    route = gh.patch(f"{REPO_PATH}/issues/12").mock(
        return_value=httpx.Response(200, json=load_fixture("github_issue.json"))
    )
    assert client.patch("/issues/12", json={"state": "open"}).status_code == 200
    assert json.loads(route.calls.last.request.content) == {"state": "open"}


def test_rename_issue(client: TestClient, gh: respx.MockRouter) -> None:
    route = gh.patch(f"{REPO_PATH}/issues/12").mock(
        return_value=httpx.Response(200, json=load_fixture("github_issue.json"))
    )
    client.patch("/issues/12", json={"title": "  New title  "})
    assert json.loads(route.calls.last.request.content) == {"title": "New title"}


# --- comments --------------------------------------------------------------


def test_create_comment_returns_201(client: TestClient, gh: respx.MockRouter) -> None:
    route = gh.post(f"{REPO_PATH}/issues/12/comments").mock(
        return_value=httpx.Response(201, json=load_fixture("github_comment.json"))
    )
    response = client.post("/issues/12/comments", json={"body": "Confirmed."})

    assert response.status_code == 201
    assert response.headers["Location"] == "/issues/12/comments"

    body = response.json()
    assert set(body) == {"id", "body", "user", "html_url", "issue_url", "created_at", "updated_at"}
    assert body["id"] == 3310028841
    assert body["user"]["login"] == "mikechau1"
    assert json.loads(route.calls.last.request.content) == {"body": "Confirmed."}


def test_list_comments_returns_200_with_pagination(
    client: TestClient, gh: respx.MockRouter
) -> None:
    gh.get(f"{REPO_PATH}/issues/12/comments").mock(
        return_value=httpx.Response(
            200,
            json=[load_fixture("github_comment.json")],
            headers={
                "link": '<https://api.github.com/repositories/1/issues/12/comments?page=2>; rel="next"'
            },
        )
    )
    response = client.get("/issues/12/comments", params={"per_page": 1})

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.headers["X-Per-Page"] == "1"
    assert "/issues/12/comments?" in parse_link_header(response.headers["Link"])["next"]


# --- cross-cutting ---------------------------------------------------------


def test_every_response_carries_a_request_id(client: TestClient, gh: respx.MockRouter) -> None:
    gh.get(f"{REPO_PATH}/issues").mock(return_value=httpx.Response(200, json=[]))
    assert client.get("/issues").headers["X-Request-ID"]


def test_an_inbound_request_id_is_adopted(client: TestClient, gh: respx.MockRouter) -> None:
    gh.get(f"{REPO_PATH}/issues").mock(return_value=httpx.Response(200, json=[]))
    response = client.get("/issues", headers={"X-Request-ID": "trace-me-42"})
    assert response.headers["X-Request-ID"] == "trace-me-42"


def test_an_oversized_request_id_is_truncated(client: TestClient, gh: respx.MockRouter) -> None:
    gh.get(f"{REPO_PATH}/issues").mock(return_value=httpx.Response(200, json=[]))
    response = client.get("/issues", headers={"X-Request-ID": "x" * 500})
    assert len(response.headers["X-Request-ID"]) == 128

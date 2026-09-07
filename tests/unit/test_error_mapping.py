"""GitHub failures -> gateway error objects.

GitHub overloads 403 for three unrelated conditions (primary rate limit,
secondary rate limit, genuine permission denial) and answers 404 for private
resources a token cannot see.  Passing those through verbatim would leave the
caller unable to tell "wait and retry" from "fix your token", so each maps to
a distinct status and code here.
"""

from __future__ import annotations

import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.errors import (
    Forbidden,
    NotFound,
    RateLimited,
    Unauthorized,
    UpstreamError,
    UpstreamTimeout,
    ValidationFailed,
    from_github_response,
    from_transport_error,
    is_rate_limited,
)
from tests.conftest import REPO_PATH


def _response(status: int, *, json: object = None, headers: dict[str, str] | None = None):
    return httpx.Response(
        status,
        json=json if json is not None else {"message": "boom"},
        headers=headers or {},
        request=httpx.Request("GET", "https://api.github.com/x"),
    )


# ---------------------------------------------------------------------------
# The mapping function
# ---------------------------------------------------------------------------


def test_401_maps_to_unauthorized_and_names_the_env_var() -> None:
    error = from_github_response(_response(401, json={"message": "Bad credentials"}))
    assert isinstance(error, Unauthorized)
    assert (error.status_code, error.code) == (401, "github_unauthorized")
    assert "GITHUB_TOKEN" in error.message
    assert "Bad credentials" in error.message
    assert error.upstream_status == 401


def test_plain_403_maps_to_forbidden_not_rate_limited() -> None:
    error = from_github_response(
        _response(
            403,
            json={"message": "Resource not accessible by personal access token"},
            headers={"x-ratelimit-remaining": "4321"},
        )
    )
    assert isinstance(error, Forbidden)
    assert error.code == "github_forbidden"
    assert "Issues: Read and write" in error.message


def test_403_with_exhausted_budget_maps_to_429_with_retry_after() -> None:
    reset = int(time.time()) + 900
    error = from_github_response(
        _response(
            403,
            json={"message": "API rate limit exceeded"},
            headers={
                "x-ratelimit-remaining": "0",
                "x-ratelimit-limit": "5000",
                "x-ratelimit-reset": str(reset),
            },
        ),
        context="listing issues",
    )
    assert isinstance(error, RateLimited)
    assert error.status_code == 429
    assert 880 <= int(error.headers["Retry-After"]) <= 900
    assert "primary rate limit" in error.message
    assert "5000/hour" in error.message
    assert "listing issues" in error.message


def test_secondary_rate_limit_uses_retry_after_header() -> None:
    error = from_github_response(
        _response(
            403,
            json={"message": "You have exceeded a secondary rate limit"},
            headers={"retry-after": "42", "x-ratelimit-remaining": "4000"},
        )
    )
    assert isinstance(error, RateLimited)
    assert error.headers["Retry-After"] == "42"
    assert "secondary rate limit" in error.message


def test_429_is_always_a_rate_limit() -> None:
    error = from_github_response(_response(429, headers={"retry-after": "7"}))
    assert isinstance(error, RateLimited)
    assert error.headers["Retry-After"] == "7"


def test_rate_limit_falls_back_to_a_default_wait() -> None:
    error = from_github_response(_response(429))
    assert isinstance(error, RateLimited)
    assert error.headers["Retry-After"] == "60"


@pytest.mark.parametrize(
    ("status", "headers", "expected"),
    [
        (403, {"x-ratelimit-remaining": "0"}, True),
        (403, {"retry-after": "60"}, True),
        (403, {"x-ratelimit-remaining": "10"}, False),
        (429, {}, True),
        (404, {"x-ratelimit-remaining": "0"}, False),
    ],
)
def test_is_rate_limited_discriminates(status: int, headers: dict, expected: bool) -> None:
    assert is_rate_limited(_response(status, headers=headers)) is expected


def test_is_rate_limited_reads_the_message_as_a_last_resort() -> None:
    assert is_rate_limited(_response(403, json={"message": "API rate limit exceeded for user"}))


def test_404_explains_the_private_resource_ambiguity() -> None:
    error = from_github_response(
        _response(404, json={"message": "Not Found"}), context="fetching issue #999"
    )
    assert isinstance(error, NotFound)
    assert "fetching issue #999" in error.message
    assert "404 rather than 403" in error.message


def test_410_maps_to_not_found_with_a_distinct_code() -> None:
    error = from_github_response(_response(410, json={"message": "Issues are disabled"}))
    assert error.status_code == 404
    assert error.code == "gone"


def test_422_maps_to_400_and_surfaces_githubs_field_errors() -> None:
    error = from_github_response(
        _response(
            422,
            json={
                "message": "Validation Failed",
                "errors": [
                    {
                        "resource": "Issue",
                        "field": "title",
                        "code": "missing_field",
                        "extraneous": "dropped",
                    }
                ],
            },
        ),
        context="creating an issue",
    )
    assert isinstance(error, ValidationFailed)
    assert error.status_code == 400
    assert error.details == [{"resource": "Issue", "field": "title", "code": "missing_field"}]


def test_string_errors_in_the_upstream_array_are_normalised() -> None:
    error = from_github_response(_response(422, json={"message": "Nope", "errors": ["too long"]}))
    assert error.details == [{"message": "too long"}]


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_upstream_5xx_maps_to_502_not_500(status: int) -> None:
    """Our 5xx is reserved for *our* faults; GitHub's becomes a 502."""
    error = from_github_response(_response(status), context="listing issues")
    assert isinstance(error, UpstreamError)
    assert error.status_code == 502
    assert error.code == "github_unavailable"
    assert error.upstream_status == status


def test_unexpected_status_still_produces_an_error_object() -> None:
    error = from_github_response(_response(418, json={"message": "teapot"}))
    assert error.status_code == 502
    assert "teapot" in error.message


def test_non_json_error_body_does_not_crash_the_mapper() -> None:
    response = httpx.Response(
        500, content=b"<html>gateway error</html>", request=httpx.Request("GET", "https://x/y")
    )
    assert from_github_response(response).status_code == 502


def test_timeout_maps_to_504() -> None:
    error = from_transport_error(httpx.ReadTimeout("slow"), context="creating an issue")
    assert isinstance(error, UpstreamTimeout)
    assert error.status_code == 504
    assert "creating an issue" in error.message


def test_connection_failure_maps_to_502() -> None:
    error = from_transport_error(httpx.ConnectError("dns"), context="listing issues")
    assert error.status_code == 502
    assert error.code == "github_unreachable"
    assert "GITHUB_API_URL" in error.message


def test_error_serialisation_shape() -> None:
    error = NotFound("gone", details=[{"field": "x"}], upstream_status=404)
    payload = error.to_dict()["error"]
    assert payload["code"] == "not_found"
    assert payload["message"] == "gone"
    assert payload["status"] == 404
    assert payload["details"] == [{"field": "x"}]
    assert payload["upstream_status"] == 404


# ---------------------------------------------------------------------------
# End to end through a route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("upstream_status", "upstream_headers", "expected_status", "expected_code"),
    [
        (401, {}, 401, "github_unauthorized"),
        (403, {"x-ratelimit-remaining": "9"}, 403, "github_forbidden"),
        (403, {"x-ratelimit-remaining": "0"}, 429, "rate_limited"),
        (404, {}, 404, "not_found"),
        (422, {}, 400, "validation_error"),
        (500, {}, 502, "github_unavailable"),
        (503, {}, 502, "github_unavailable"),
    ],
)
def test_route_translates_upstream_failures(
    client: TestClient,
    gh: respx.MockRouter,
    upstream_status: int,
    upstream_headers: dict,
    expected_status: int,
    expected_code: str,
) -> None:
    gh.get(f"{REPO_PATH}/issues/12").mock(
        return_value=httpx.Response(
            upstream_status, json={"message": "upstream said no"}, headers=upstream_headers
        )
    )
    response = client.get("/issues/12")
    assert response.status_code == expected_status
    body = response.json()
    assert body["error"]["code"] == expected_code
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]


def test_rate_limited_route_sets_retry_after(client: TestClient, gh: respx.MockRouter) -> None:
    gh.get(f"{REPO_PATH}/issues").mock(
        return_value=httpx.Response(
            403,
            json={"message": "API rate limit exceeded"},
            headers={"x-ratelimit-remaining": "0", "retry-after": "120"},
        )
    )
    response = client.get("/issues")
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "120"


def test_timeout_through_a_route_is_504(client: TestClient, gh: respx.MockRouter) -> None:
    gh.post(f"{REPO_PATH}/issues").mock(side_effect=httpx.ReadTimeout("slow"))
    response = client.post("/issues", json={"title": "hello"})
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "github_timeout"


def test_missing_token_is_401_before_any_upstream_call(settings, gh: respx.MockRouter) -> None:
    from app.main import create_app

    settings.github_token = ""
    with TestClient(create_app(settings)) as unconfigured:
        response = unconfigured.post("/issues", json={"title": "hello"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_credentials"
    assert not gh.calls


def test_missing_repo_config_is_503(settings, gh: respx.MockRouter) -> None:
    from app.main import create_app

    settings.github_repo = ""
    with TestClient(create_app(settings)) as unconfigured:
        response = unconfigured.get("/issues")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "not_configured"
    assert not gh.calls


def test_unexpected_upstream_shape_is_502(client: TestClient, gh: respx.MockRouter) -> None:
    """A 200 whose body is not an object should not blow up as a 500."""
    gh.get(f"{REPO_PATH}/issues/12").mock(return_value=httpx.Response(200, json="not-an-object"))
    response = client.get("/issues/12")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"

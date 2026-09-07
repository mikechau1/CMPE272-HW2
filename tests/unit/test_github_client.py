"""GitHub client: headers, retry policy, rate-limit tracking, conditional GET."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from app.config import Settings
from app.errors import NotFound, RateLimited, Unauthorized, UpstreamError, UpstreamTimeout
from app.github_client import (
    GITHUB_ACCEPT,
    GITHUB_API_VERSION,
    ETagCache,
    GitHubClient,
)
from tests.conftest import REPO_PATH, load_fixture


class FakeSleep:
    """Records backoff delays instead of spending them."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.fixture
def sleeper() -> FakeSleep:
    return FakeSleep()


@pytest.fixture
async def make_client(settings: Settings, sleeper: FakeSleep) -> AsyncIterator[object]:
    created: list[GitHubClient] = []

    def _make(**overrides: object) -> GitHubClient:
        local = settings.model_copy(update=overrides)
        client = GitHubClient(local, httpx.AsyncClient(timeout=1.0), sleep=sleeper)
        created.append(client)
        return client

    yield _make
    for client in created:
        await client._client.aclose()


@pytest.fixture
def github(make_client) -> GitHubClient:
    return make_client()


# --- request construction --------------------------------------------------


async def test_sends_the_headers_github_asks_for(
    github: GitHubClient, gh: respx.MockRouter
) -> None:
    route = gh.get(f"{REPO_PATH}/issues").mock(return_value=httpx.Response(200, json=[]))
    await github.list_issues()

    headers = route.calls.last.request.headers
    assert headers["accept"] == GITHUB_ACCEPT
    assert headers["x-github-api-version"] == GITHUB_API_VERSION
    assert headers["authorization"] == "Bearer github_pat_TESTTOKEN"
    assert "cmpe272-issues-gateway" in headers["user-agent"]


async def test_omits_empty_query_parameters(github: GitHubClient, gh: respx.MockRouter) -> None:
    route = gh.get(f"{REPO_PATH}/issues").mock(return_value=httpx.Response(200, json=[]))
    await github.list_issues(state="all", labels=None, sort=None, page=2, per_page=50)

    params = route.calls.last.request.url.params
    assert dict(params) == {"state": "all", "page": "2", "per_page": "50"}


async def test_missing_token_raises_before_any_network_call(
    make_client, gh: respx.MockRouter
) -> None:
    client = make_client(github_token="")
    with pytest.raises(Unauthorized) as caught:
        await client.list_issues()
    assert caught.value.code == "missing_credentials"
    assert not gh.calls


async def test_204_decodes_to_none(github: GitHubClient, gh: respx.MockRouter) -> None:
    gh.get(f"{REPO_PATH}/issues").mock(return_value=httpx.Response(204))
    assert (await github.list_issues()).data is None


async def test_non_json_success_body_is_returned_as_text(
    github: GitHubClient, gh: respx.MockRouter
) -> None:
    gh.get(f"{REPO_PATH}/issues").mock(return_value=httpx.Response(200, content=b"plain"))
    assert (await github.list_issues()).data == "plain"


# --- rate limit tracking ---------------------------------------------------


async def test_rate_limit_headers_are_absorbed(github: GitHubClient, gh: respx.MockRouter) -> None:
    gh.get(f"{REPO_PATH}/issues").mock(
        return_value=httpx.Response(
            200,
            json=[],
            headers={
                "x-ratelimit-limit": "5000",
                "x-ratelimit-remaining": "4987",
                "x-ratelimit-reset": "1757203200",
                "x-ratelimit-used": "13",
                "x-ratelimit-resource": "core",
            },
        )
    )
    await github.list_issues()

    assert github.rate_limit.limit == 5000
    assert github.rate_limit.remaining == 4987
    assert github.rate_limit.used == 13
    assert github.rate_limit.resource == "core"
    assert github.rate_limit.as_headers()["X-RateLimit-Remaining"] == "4987"


async def test_garbage_rate_limit_headers_are_ignored(
    github: GitHubClient, gh: respx.MockRouter
) -> None:
    gh.get(f"{REPO_PATH}/issues").mock(
        return_value=httpx.Response(200, json=[], headers={"x-ratelimit-remaining": "lots"})
    )
    await github.list_issues()
    assert github.rate_limit.remaining is None


async def test_exhausted_budget_raises_rate_limited(
    github: GitHubClient, gh: respx.MockRouter
) -> None:
    gh.get(f"{REPO_PATH}/issues").mock(
        return_value=httpx.Response(
            403,
            json={"message": "API rate limit exceeded"},
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(time.time()) + 60)},
        )
    )
    with pytest.raises(RateLimited) as caught:
        await github.list_issues()
    assert 50 <= caught.value.retry_after <= 60


# --- retry policy ----------------------------------------------------------


async def test_safe_methods_retry_on_5xx(make_client, gh: respx.MockRouter, sleeper) -> None:
    client = make_client(github_max_retries=2)
    route = gh.get(f"{REPO_PATH}/issues").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(502, json={"message": "boom"}),
            httpx.Response(200, json=[{"number": 1}]),
        ]
    )
    result = await client.list_issues()

    assert result.data == [{"number": 1}]
    assert route.call_count == 3
    assert sleeper.delays == [0.25, 0.5], "exponential backoff"


async def test_retries_are_bounded(make_client, gh: respx.MockRouter) -> None:
    client = make_client(github_max_retries=2)
    route = gh.get(f"{REPO_PATH}/issues").mock(
        return_value=httpx.Response(500, json={"message": "down"})
    )
    with pytest.raises(UpstreamError):
        await client.list_issues()
    assert route.call_count == 3, "initial attempt plus two retries, then give up"


async def test_post_is_never_retried(make_client, gh: respx.MockRouter) -> None:
    """Retrying a create could produce two issues; correctness beats resilience."""
    client = make_client(github_max_retries=3)
    route = gh.post(f"{REPO_PATH}/issues").mock(
        side_effect=[
            httpx.Response(502, json={"message": "boom"}),
            httpx.Response(201, json=load_fixture("github_issue.json")),
        ]
    )
    with pytest.raises(UpstreamError):
        await client.create_issue(title="hello")
    assert route.call_count == 1


async def test_patch_is_retried_because_it_is_idempotent(make_client, gh: respx.MockRouter) -> None:
    client = make_client(github_max_retries=1)
    route = gh.patch(f"{REPO_PATH}/issues/12").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json=load_fixture("github_issue.json")),
        ]
    )
    assert (await client.update_issue(12, {"state": "closed"})).status_code == 200
    assert route.call_count == 2


async def test_transport_errors_are_retried_for_safe_methods(
    make_client, gh: respx.MockRouter
) -> None:
    client = make_client(github_max_retries=1)
    route = gh.get(f"{REPO_PATH}/issues").mock(
        side_effect=[httpx.ConnectError("reset"), httpx.Response(200, json=[])]
    )
    assert (await client.list_issues()).status_code == 200
    assert route.call_count == 2


async def test_persistent_timeout_becomes_504(make_client, gh: respx.MockRouter) -> None:
    client = make_client(github_max_retries=1)
    gh.get(f"{REPO_PATH}/issues").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(UpstreamTimeout):
        await client.list_issues()


async def test_client_errors_are_not_retried(make_client, gh: respx.MockRouter) -> None:
    client = make_client(github_max_retries=3)
    route = gh.get(f"{REPO_PATH}/issues/999").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    with pytest.raises(NotFound):
        await client.get_issue(999)
    assert route.call_count == 1


async def test_retry_after_extends_the_backoff(make_client, gh: respx.MockRouter, sleeper) -> None:
    client = make_client(github_max_retries=1)
    gh.get(f"{REPO_PATH}/issues").mock(
        side_effect=[
            httpx.Response(429, json={"message": "slow down"}, headers={"retry-after": "5"}),
            httpx.Response(200, json=[]),
        ]
    )
    await client.list_issues()
    assert sleeper.delays == [5.0]


async def test_retry_after_is_capped(make_client, gh: respx.MockRouter, sleeper) -> None:
    """A hostile or mistaken Retry-After must not stall the worker forever."""
    client = make_client(github_max_retries=1)
    gh.get(f"{REPO_PATH}/issues").mock(
        side_effect=[
            httpx.Response(429, json={"message": "slow"}, headers={"retry-after": "99999"}),
            httpx.Response(200, json=[]),
        ]
    )
    await client.list_issues()
    assert sleeper.delays == [30.0]


# --- conditional GET (ETag) ------------------------------------------------


async def test_etag_is_replayed_as_if_none_match(
    github: GitHubClient, gh: respx.MockRouter
) -> None:
    payload = load_fixture("github_issue_list.json")
    route = gh.get(f"{REPO_PATH}/issues").mock(
        side_effect=[
            httpx.Response(200, json=payload, headers={"etag": 'W/"abc123"'}),
            httpx.Response(304, headers={"etag": 'W/"abc123"'}),
        ]
    )

    first = await github.list_issues()
    assert first.from_cache is False
    assert "if-none-match" not in route.calls[0].request.headers

    second = await github.list_issues()
    assert route.calls[1].request.headers["if-none-match"] == 'W/"abc123"'
    assert second.from_cache is True
    assert second.status_code == 200
    assert second.data == payload, "a 304 must serve the cached body, not an empty one"


async def test_304_refreshes_the_rate_limit_snapshot(
    github: GitHubClient, gh: respx.MockRouter
) -> None:
    """A 304 costs no quota, and the response still carries the live counters."""
    gh.get(f"{REPO_PATH}/issues").mock(
        side_effect=[
            httpx.Response(200, json=[], headers={"etag": 'W/"e1"', "x-ratelimit-remaining": "10"}),
            httpx.Response(304, headers={"etag": 'W/"e1"', "x-ratelimit-remaining": "10"}),
        ]
    )
    await github.list_issues()
    result = await github.list_issues()
    assert result.headers["x-ratelimit-remaining"] == "10"
    assert github.rate_limit.remaining == 10


async def test_a_changed_etag_replaces_the_cached_entry(
    github: GitHubClient, gh: respx.MockRouter
) -> None:
    gh.get(f"{REPO_PATH}/issues").mock(
        side_effect=[
            httpx.Response(200, json=[{"number": 1}], headers={"etag": 'W/"v1"'}),
            httpx.Response(200, json=[{"number": 2}], headers={"etag": 'W/"v2"'}),
            httpx.Response(304, headers={"etag": 'W/"v2"'}),
        ]
    )
    await github.list_issues()
    await github.list_issues()
    assert (await github.list_issues()).data == [{"number": 2}]


async def test_cache_is_keyed_by_query(github: GitHubClient, gh: respx.MockRouter) -> None:
    route = gh.get(f"{REPO_PATH}/issues").mock(
        return_value=httpx.Response(200, json=[], headers={"etag": 'W/"x"'})
    )
    await github.list_issues(state="open")
    await github.list_issues(state="closed")
    assert "if-none-match" not in route.calls[1].request.headers
    assert len(github.cache) == 2


async def test_cache_can_be_disabled(make_client, gh: respx.MockRouter) -> None:
    client = make_client(enable_etag_cache=False)
    route = gh.get(f"{REPO_PATH}/issues").mock(
        return_value=httpx.Response(200, json=[], headers={"etag": 'W/"x"'})
    )
    await client.list_issues()
    await client.list_issues()
    assert "if-none-match" not in route.calls[1].request.headers
    assert len(client.cache) == 0


async def test_write_responses_are_never_cached(github: GitHubClient, gh: respx.MockRouter) -> None:
    gh.post(f"{REPO_PATH}/issues").mock(
        return_value=httpx.Response(
            201, json=load_fixture("github_issue.json"), headers={"etag": 'W/"created"'}
        )
    )
    await github.create_issue(title="hello")
    assert len(github.cache) == 0


async def test_304_without_a_cached_entry_is_not_treated_as_success(
    github: GitHubClient, gh: respx.MockRouter
) -> None:
    """Defensive: a bare 304 we cannot satisfy must surface, not return None."""
    gh.get(f"{REPO_PATH}/issues").mock(return_value=httpx.Response(304))
    with pytest.raises(UpstreamError):
        await github.list_issues()


# --- the cache itself ------------------------------------------------------


def test_cache_key_is_order_insensitive() -> None:
    assert ETagCache.key("GET", "/x", {"b": 2, "a": 1}) == ETagCache.key(
        "GET", "/x", {"a": 1, "b": 2}
    )


def test_cache_key_ignores_none_values() -> None:
    assert ETagCache.key("GET", "/x", {"a": 1, "b": None}) == ETagCache.key("GET", "/x", {"a": 1})


def test_cache_evicts_least_recently_used() -> None:
    from app.github_client import _CacheEntry

    cache = ETagCache(max_entries=2)
    cache.put("a", _CacheEntry(etag="1", data=None))
    cache.put("b", _CacheEntry(etag="2", data=None))
    cache.get("a")  # 'a' is now the most recent
    cache.put("c", _CacheEntry(etag="3", data=None))

    assert len(cache) == 2
    assert cache.get("b") is None
    assert cache.get("a") is not None


def test_cache_clear_resets_counters() -> None:
    from app.github_client import _CacheEntry

    cache = ETagCache()
    cache.put("a", _CacheEntry(etag="1", data=None))
    cache.hits = 3
    cache.clear()
    assert len(cache) == 0
    assert cache.hits == 0

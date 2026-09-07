"""Issue and comment routes.

CRUD, with the C-R-U-D caveat the assignment calls out: GitHub has no
"delete issue" REST endpoint, so deletion is modelled as
``PATCH /issues/{number} {"state": "closed"}``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.responses import JSONResponse

from ..deps import get_github
from ..errors import NotFound, UpstreamError
from ..github_client import GitHubClient, GitHubResult
from ..logging_config import get_logger
from ..pagination import normalize_per_page, rewrite_link_header
from ..schemas import (
    CreateCommentRequest,
    CreateIssueRequest,
    IssueStateFilter,
    UpdateIssueRequest,
    is_pull_request,
    project_comment,
    project_issue,
)

logger = get_logger(__name__)
router = APIRouter(tags=["issues"])

# Bumped whenever project_issue() changes shape, so a stale client ETag from an
# older deploy can never produce a 304 carrying the wrong projection.
_PROJECTION_VERSION = "1"


def gateway_etag(upstream: str | None) -> str | None:
    """Derive our ETag from GitHub's, tagged with our projection version."""
    if not upstream:
        return None
    core = upstream.strip()
    if core.startswith("W/"):
        core = core[2:]
    core = core.strip('"')
    if not core:
        return None
    return f'W/"gw{_PROJECTION_VERSION}-{core}"'


def _if_none_match_matches(header: str | None, etag: str | None) -> bool:
    if not header or not etag:
        return False
    candidates = {token.strip() for token in header.split(",") if token.strip()}
    if "*" in candidates:
        return True

    # Weak comparison: W/"x" and "x" are the same entity for a conditional GET.
    def core(value: str) -> str:
        return value[2:].strip() if value.startswith("W/") else value.strip()

    return any(core(candidate) == core(etag) for candidate in candidates)


def _response_headers(github: GitHubClient, result: GitHubResult | None = None) -> dict[str, str]:
    """Surface upstream rate-limit budget and cache outcome to our callers."""
    headers = github.rate_limit.as_headers()
    if result is not None:
        headers["X-Cache"] = "HIT" if result.from_cache else "MISS"
    return headers


def _expect_object(result: GitHubResult, what: str) -> dict[str, Any]:
    if not isinstance(result.data, dict):
        raise UpstreamError(f"GitHub returned an unexpected body for {what}.")
    return result.data


@router.post("/issues", status_code=201)
async def create_issue(
    payload: CreateIssueRequest,
    github: Annotated[GitHubClient, Depends(get_github)],
) -> Response:
    result = await github.create_issue(
        title=payload.title, body=payload.body, labels=payload.labels
    )
    issue = project_issue(_expect_object(result, "the created issue"))

    logger.info(
        "issue created",
        extra={"issue_number": issue["number"], "labels": [x["name"] for x in issue["labels"]]},
    )
    headers = _response_headers(github)
    headers["Location"] = f"/issues/{issue['number']}"
    return JSONResponse(issue, status_code=201, headers=headers)


@router.get("/issues")
async def list_issues(
    request: Request,
    github: Annotated[GitHubClient, Depends(get_github)],
    state: Annotated[IssueStateFilter, Query(description="Filter by issue state.")] = "open",
    labels: Annotated[
        str | None,
        Query(description="Comma-separated label names; a match carries all of them."),
    ] = None,
    page: Annotated[int, Query(ge=1, description="1-based page number.")] = 1,
    per_page: Annotated[int, Query(ge=1, le=100, description="Results per page (max 100).")] = 30,
    sort: Annotated[str | None, Query(pattern="^(created|updated|comments)$")] = None,
    direction: Annotated[str | None, Query(pattern="^(asc|desc)$")] = None,
) -> Response:
    per_page = normalize_per_page(per_page)
    result = await github.list_issues(
        state=state, labels=labels, page=page, per_page=per_page, sort=sort, direction=direction
    )

    raw_items = result.data if isinstance(result.data, list) else []
    # GitHub's "list issues" also returns pull requests; this is an Issues API.
    issues = [project_issue(item) for item in raw_items if not is_pull_request(item)]

    etag = gateway_etag(result.etag)
    headers = _response_headers(github, result)
    headers["X-Page"] = str(page)
    headers["X-Per-Page"] = str(per_page)
    if etag:
        headers["ETag"] = etag
        headers["Cache-Control"] = "no-cache"

    if _if_none_match_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)

    link = rewrite_link_header(
        result.link,
        base_url=str(request.url_for("list_issues")),
        passthrough={"state": state, "labels": labels, "sort": sort, "direction": direction},
    )
    if link:
        headers["Link"] = link

    return JSONResponse(issues, status_code=200, headers=headers)


@router.get("/issues/{number}")
async def get_issue(
    github: Annotated[GitHubClient, Depends(get_github)],
    number: Annotated[int, Path(ge=1, description="Issue number within the repository.")],
) -> Response:
    result = await github.get_issue(number)
    raw = _expect_object(result, f"issue #{number}")

    if is_pull_request(raw):
        # GitHub serves PRs from the issues endpoint; this API does not.
        raise NotFound(
            f"#{number} is a pull request, not an issue. This gateway exposes issues only.",
            code="not_an_issue",
        )

    return JSONResponse(
        project_issue(raw), status_code=200, headers=_response_headers(github, result)
    )


@router.patch("/issues/{number}")
async def update_issue(
    payload: UpdateIssueRequest,
    github: Annotated[GitHubClient, Depends(get_github)],
    number: Annotated[int, Path(ge=1, description="Issue number within the repository.")],
) -> Response:
    changes = payload.to_github_payload()
    result = await github.update_issue(number, changes)
    issue = project_issue(_expect_object(result, f"issue #{number}"))

    logger.info(
        "issue updated",
        extra={"issue_number": number, "fields": sorted(changes), "state": issue["state"]},
    )
    return JSONResponse(issue, status_code=200, headers=_response_headers(github, result))


@router.post("/issues/{number}/comments", status_code=201)
async def create_comment(
    payload: CreateCommentRequest,
    github: Annotated[GitHubClient, Depends(get_github)],
    number: Annotated[int, Path(ge=1, description="Issue number within the repository.")],
) -> Response:
    result = await github.create_comment(number, payload.body)
    comment = project_comment(_expect_object(result, "the created comment"))

    logger.info("comment created", extra={"issue_number": number, "comment_id": comment["id"]})
    headers = _response_headers(github)
    headers["Location"] = f"/issues/{number}/comments"
    return JSONResponse(comment, status_code=201, headers=headers)


@router.get("/issues/{number}/comments")
async def list_comments(
    request: Request,
    github: Annotated[GitHubClient, Depends(get_github)],
    number: Annotated[int, Path(ge=1, description="Issue number within the repository.")],
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 30,
) -> Response:
    per_page = normalize_per_page(per_page)
    result = await github.list_comments(number, page=page, per_page=per_page)

    raw_items = result.data if isinstance(result.data, list) else []
    comments = [project_comment(item) for item in raw_items]

    headers = _response_headers(github, result)
    headers["X-Page"] = str(page)
    headers["X-Per-Page"] = str(per_page)
    link = rewrite_link_header(
        result.link,
        base_url=str(request.url_for("list_comments", number=number)),
    )
    if link:
        headers["Link"] = link

    return JSONResponse(comments, status_code=200, headers=headers)

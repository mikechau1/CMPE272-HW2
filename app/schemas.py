"""Request/response models, and the GitHub -> gateway projections.

The gateway does not echo GitHub payloads wholesale.  It projects them onto a
small documented surface (:func:`project_issue`, :func:`project_comment`) so
the OpenAPI contract stays honest and upstream field churn cannot leak into
our clients.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IssueState = Literal["open", "closed"]
IssueStateFilter = Literal["open", "closed", "all"]
StateReason = Literal["completed", "not_planned", "reopened"]


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class CreateIssueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, Field(min_length=1, max_length=256)]
    body: Annotated[str | None, Field(max_length=65536)] = None
    labels: Annotated[list[str] | None, Field(max_length=100)] = None

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must contain at least one non-whitespace character")
        return value

    @field_validator("labels")
    @classmethod
    def _labels_not_blank(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [label.strip() for label in value]
        if any(not label for label in cleaned):
            raise ValueError("labels must not contain empty strings")
        return cleaned


class UpdateIssueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[str | None, Field(min_length=1, max_length=256)] = None
    body: Annotated[str | None, Field(max_length=65536)] = None
    state: IssueState | None = None
    state_reason: StateReason | None = None
    labels: Annotated[list[str] | None, Field(max_length=100)] = None

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("title must contain at least one non-whitespace character")
        return value

    @model_validator(mode="after")
    def _at_least_one_field(self) -> UpdateIssueRequest:
        # `exclude_unset` semantics: an explicit `"body": null` counts as a
        # change (clear the body); an absent key does not.
        if not self.model_fields_set:
            raise ValueError("provide at least one of: title, body, state, state_reason, labels")
        return self

    def to_github_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class CreateCommentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: Annotated[str, Field(min_length=1, max_length=65536)]

    @field_validator("body")
    @classmethod
    def _body_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("body must contain at least one non-whitespace character")
        return value


# ---------------------------------------------------------------------------
# GitHub -> gateway projections
# ---------------------------------------------------------------------------


def project_user(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    return {
        "login": raw.get("login"),
        "id": raw.get("id"),
        "type": raw.get("type"),
        "html_url": raw.get("html_url"),
        "avatar_url": raw.get("avatar_url"),
    }


def project_label(raw: Any) -> dict[str, Any]:
    """GitHub returns label objects, but accepts plain strings on write."""
    if isinstance(raw, str):
        return {"name": raw, "color": None, "description": None}
    if isinstance(raw, dict):
        return {
            "name": raw.get("name"),
            "color": raw.get("color"),
            "description": raw.get("description"),
        }
    return {"name": str(raw), "color": None, "description": None}


def project_issue(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": raw.get("number"),
        "id": raw.get("id"),
        "title": raw.get("title"),
        "body": raw.get("body"),
        "state": raw.get("state"),
        "state_reason": raw.get("state_reason"),
        "labels": [project_label(label) for label in raw.get("labels") or []],
        "user": project_user(raw.get("user")),
        "assignees": [
            login
            for login in (
                (a or {}).get("login") for a in raw.get("assignees") or [] if isinstance(a, dict)
            )
            if login
        ],
        "comments": raw.get("comments"),
        "locked": raw.get("locked"),
        "html_url": raw.get("html_url"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "closed_at": raw.get("closed_at"),
    }


def project_comment(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": raw.get("id"),
        "body": raw.get("body"),
        "user": project_user(raw.get("user")),
        "html_url": raw.get("html_url"),
        "issue_url": raw.get("issue_url"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
    }


def is_pull_request(raw: dict[str, Any]) -> bool:
    """GitHub's issues list includes pull requests; they carry this key.

    The gateway is an *issues* API, so PRs are filtered out of list results.
    """
    return isinstance(raw, dict) and raw.get("pull_request") is not None

"""The contract is the source of truth -- these tests stop it drifting.

`openapi.yaml` is hand-written and served as the app's schema, so nothing
forces it to match the code.  These checks supply that force: every route the
app exposes must be documented, every documented route must exist, and the
examples must satisfy the schemas they are attached to.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from openapi_spec_validator import validate

CONTRACT = Path(__file__).resolve().parents[2] / "openapi.yaml"

# Documented but not implemented as app routes, and vice versa.
UNDOCUMENTED_ROUTES = {
    "/",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/openapi.yaml",
    "/docs/oauth2-redirect",
}


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    with CONTRACT.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


# --- the document itself ---------------------------------------------------


def test_contract_exists_at_the_repository_root() -> None:
    assert CONTRACT.exists(), "openapi.yaml is a required submission artifact"


def test_contract_is_valid_openapi_31(spec: dict) -> None:
    assert spec["openapi"] == "3.1.0"
    validate(spec)


def test_contract_declares_a_server_driven_by_the_port_variable(spec: dict) -> None:
    server = spec["servers"][0]
    assert "{port}" in server["url"]
    assert server["variables"]["port"]["default"] == "8000"


# --- required surface ------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/issues", "post"),
        ("/issues", "get"),
        ("/issues/{number}", "get"),
        ("/issues/{number}", "patch"),
        ("/issues/{number}/comments", "post"),
        ("/webhook", "post"),
        ("/events", "get"),
        ("/healthz", "get"),
    ],
)
def test_every_assigned_route_is_documented(spec: dict, path: str, method: str) -> None:
    assert method in spec["paths"][path]


@pytest.mark.parametrize("name", ["Issue", "Comment", "Error"])
def test_required_reusable_schemas_exist(spec: dict, name: str) -> None:
    assert name in spec["components"]["schemas"]


def test_security_schemes_cover_both_credentials(spec: dict) -> None:
    schemes = spec["components"]["securitySchemes"]
    assert schemes["githubToken"]["type"] == "http"
    assert schemes["githubToken"]["scheme"] == "bearer"
    assert schemes["webhookSignature"]["name"] == "X-Hub-Signature-256"


def test_github_backed_routes_declare_the_bearer_scheme(spec: dict) -> None:
    for path in ("/issues", "/issues/{number}", "/issues/{number}/comments"):
        for method, operation in spec["paths"][path].items():
            if method == "parameters":
                continue
            assert operation.get("security") == [{"githubToken": []}], f"{method.upper()} {path}"


def test_webhook_declares_the_signature_scheme(spec: dict) -> None:
    assert spec["paths"]["/webhook"]["post"]["security"] == [{"webhookSignature": []}]


# --- documented behaviour --------------------------------------------------


def test_create_documents_201_with_a_location_header(spec: dict) -> None:
    created = spec["paths"]["/issues"]["post"]["responses"]["201"]
    assert "Location" in created["headers"]


def test_webhook_documents_a_bodyless_204(spec: dict) -> None:
    responses = spec["paths"]["/webhook"]["post"]["responses"]
    assert "content" not in responses["204"]
    assert set(responses) >= {"204", "400", "401"}


def test_list_documents_pagination_and_conditional_get(spec: dict) -> None:
    operation = spec["paths"]["/issues"]["get"]
    ok = operation["responses"]["200"]
    assert {"Link", "ETag", "X-Page", "X-Per-Page"} <= set(ok["headers"])
    assert "304" in operation["responses"]

    names = {param.get("$ref", "").rsplit("/", 1)[-1] for param in operation["parameters"]}
    assert {"Page", "PerPage", "StateFilter", "IfNoneMatch"} <= names


def test_per_page_is_capped_at_100(spec: dict) -> None:
    assert spec["components"]["parameters"]["PerPage"]["schema"]["maximum"] == 100


def test_rate_limited_response_documents_retry_after(spec: dict) -> None:
    assert "Retry-After" in spec["components"]["responses"]["RateLimited"]["headers"]


def test_error_schema_pins_the_envelope(spec: dict) -> None:
    error = spec["components"]["schemas"]["Error"]["properties"]["error"]
    assert set(error["required"]) == {"code", "message", "status"}
    assert "request_id" in error["properties"]


def test_delete_is_documented_as_closing_an_issue(spec: dict) -> None:
    """The assignment's "D": there is no DELETE, so PATCH must explain itself."""
    assert "delete" not in spec["paths"]["/issues/{number}"]
    patch = spec["paths"]["/issues/{number}"]["patch"]
    assert "close" in patch["summary"].lower()
    assert (
        "closed"
        in patch["requestBody"]["content"]["application/json"]["examples"]["close"]["value"][
            "state"
        ]
    )


# --- examples --------------------------------------------------------------


def _iter_examples(node: Any, trail: str = "") -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "examples" and isinstance(value, dict):
                for name, example in value.items():
                    if isinstance(example, dict) and "value" in example:
                        found.append((f"{trail}.{name}", example["value"]))
            else:
                found.extend(_iter_examples(value, f"{trail}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_iter_examples(value, f"{trail}[{index}]"))
    return found


def test_the_contract_carries_examples_for_success_and_failure(spec: dict) -> None:
    examples = _iter_examples(spec["paths"])
    assert len(examples) >= 20

    covered = {
        status
        for path in spec["paths"].values()
        for method, operation in path.items()
        if method != "parameters"
        for status, response in operation.get("responses", {}).items()
        if _iter_examples(response)
    }
    assert {"200", "201", "204"} & covered
    assert {"400", "401"} <= covered


def test_error_examples_match_the_error_envelope(spec: dict) -> None:
    required = {"code", "message", "status"}
    for name, value in _iter_examples(spec):
        if isinstance(value, dict) and "error" in value:
            assert required <= set(value["error"]), name
            assert 400 <= value["error"]["status"] <= 599, name


def test_issue_examples_match_the_issue_schema(spec: dict) -> None:
    required = set(spec["components"]["schemas"]["Issue"]["required"])
    for name, value in _iter_examples(spec["paths"]):
        candidate = value[0] if isinstance(value, list) and value else value
        if isinstance(candidate, dict) and "number" in candidate and "state" in candidate:
            assert required <= set(candidate), name


def test_no_real_credentials_appear_in_the_contract() -> None:
    text = CONTRACT.read_text(encoding="utf-8")
    assert not re.search(r"gh[pousr]_[A-Za-z0-9]{16,}", text)
    assert not re.search(r"github_pat_[A-Za-z0-9_]{20,}", text)


# --- code <-> contract -----------------------------------------------------


def _walk(routes: Any) -> list[Any]:
    """Flatten FastAPI's route tree.

    `include_router` wraps each router in a container whose own `path` is None
    and whose children hang off `original_router`, so a flat scan of
    `app.routes` finds none of the real endpoints.
    """
    flat = []
    for route in routes:
        nested = getattr(route, "original_router", None) or (
            route if hasattr(route, "routes") and not hasattr(route, "methods") else None
        )
        if nested is not None:
            flat.extend(_walk(nested.routes))
        else:
            flat.append(route)
    return flat


def _app_routes(app: FastAPI) -> set[tuple[str, str]]:
    routes = set()
    for route in _walk(app.routes):
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods or path in UNDOCUMENTED_ROUTES:
            continue
        for method in methods:
            if method not in {"HEAD", "OPTIONS"}:
                routes.add((path, method.lower()))
    return routes


def _spec_routes(spec: dict) -> set[tuple[str, str]]:
    return {
        (path, method)
        for path, operations in spec["paths"].items()
        for method in operations
        if method != "parameters"
    }


def test_every_implemented_route_is_in_the_contract(app: FastAPI, spec: dict) -> None:
    undocumented = _app_routes(app) - _spec_routes(spec)
    assert not undocumented, f"implemented but undocumented: {sorted(undocumented)}"


def test_every_documented_route_is_implemented(app: FastAPI, spec: dict) -> None:
    unimplemented = _spec_routes(spec) - _app_routes(app)
    assert not unimplemented, f"documented but missing: {sorted(unimplemented)}"


def test_operation_ids_are_unique(spec: dict) -> None:
    ids = [
        operation["operationId"]
        for path in spec["paths"].values()
        for method, operation in path.items()
        if method != "parameters"
    ]
    assert len(ids) == len(set(ids))


def test_every_internal_ref_resolves(spec: dict) -> None:
    def refs(node: Any) -> list[str]:
        if isinstance(node, dict):
            found = [node["$ref"]] if isinstance(node.get("$ref"), str) else []
            for key, value in node.items():
                if key != "$ref":
                    found.extend(refs(value))
            return found
        if isinstance(node, list):
            return [ref for item in node for ref in refs(item)]
        return []

    for ref in refs(spec):
        assert ref.startswith("#/"), f"external refs are not bundled: {ref}"
        target: Any = spec
        for segment in ref[2:].split("/"):
            assert segment in target, f"unresolved $ref: {ref}"
            target = target[segment]

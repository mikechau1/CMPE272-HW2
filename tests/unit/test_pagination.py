"""Link header parsing and rewriting."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from app.pagination import (
    build_link_header,
    normalize_per_page,
    page_params,
    parse_link_header,
    rewrite_link_header,
)

GITHUB_LINK = (
    '<https://api.github.com/repositories/889012345/issues?state=open&page=2>; rel="next", '
    '<https://api.github.com/repositories/889012345/issues?state=open&page=9>; rel="last"'
)


# --- parsing ---------------------------------------------------------------


def test_parses_githubs_two_rel_header() -> None:
    links = parse_link_header(GITHUB_LINK)
    assert set(links) == {"next", "last"}
    assert links["next"].endswith("page=2")


def test_parses_all_four_rels() -> None:
    header = ", ".join(
        f'<https://api.github.com/x?page={page}>; rel="{rel}"'
        for rel, page in [("prev", 1), ("next", 3), ("first", 1), ("last", 9)]
    )
    assert set(parse_link_header(header)) == {"prev", "next", "first", "last"}


@pytest.mark.parametrize("value", [None, "", "   ", "garbage", '<>; rel="next"'])
def test_unparseable_input_yields_no_links(value: str | None) -> None:
    assert parse_link_header(value) == {}


def test_tolerates_unquoted_rel_and_odd_spacing() -> None:
    links = parse_link_header("<https://api.github.com/x?page=2>  ;   rel=next")
    assert links["next"].endswith("page=2")


def test_ignores_non_rel_parameters() -> None:
    header = '<https://api.github.com/x?page=2>; type="application/json"; rel="next"'
    assert parse_link_header(header) == {"next": "https://api.github.com/x?page=2"}


def test_multi_token_rel_registers_each_token() -> None:
    links = parse_link_header('<https://api.github.com/x?page=2>; rel="next alternate"')
    assert links["next"] == links["alternate"]


def test_first_occurrence_of_a_rel_wins() -> None:
    header = '<https://a/1>; rel="next", <https://b/2>; rel="next"'
    assert parse_link_header(header)["next"] == "https://a/1"


def test_urls_containing_commas_and_semicolons_survive() -> None:
    header = '<https://api.github.com/x?labels=bug,gateway&page=2>; rel="next"'
    assert parse_link_header(header)["next"].endswith("labels=bug,gateway&page=2")


# --- building --------------------------------------------------------------


def test_build_orders_rels_predictably() -> None:
    header = build_link_header(
        {"last": "https://x/9", "next": "https://x/2", "first": "https://x/1"}
    )
    assert header.index('rel="first"') < header.index('rel="next"') < header.index('rel="last"')


def test_build_then_parse_round_trips() -> None:
    links = {"next": "https://x/2?a=1", "last": "https://x/9?a=1"}
    assert parse_link_header(build_link_header(links)) == links


def test_build_appends_unknown_rels_after_the_known_ones() -> None:
    header = build_link_header({"weird": "https://x/w", "next": "https://x/2"})
    assert header.index('rel="next"') < header.index('rel="weird"')


# --- cursors ---------------------------------------------------------------


def test_page_params_keeps_only_cursors() -> None:
    url = "https://api.github.com/x?state=open&page=2&per_page=50&labels=bug"
    assert page_params(url) == {"page": "2", "per_page": "50"}


def test_page_params_on_a_bare_url() -> None:
    assert page_params("https://api.github.com/x") == {}


def test_page_params_drops_opaque_github_cursors() -> None:
    """GitHub emits `after`/`before` cursors our routes do not accept."""
    url = "https://api.github.com/x?page=2&per_page=2&after=Y3Vyc29yOnYyOpLPAAA%3D"
    assert page_params(url) == {"page": "2", "per_page": "2"}


def test_rewritten_links_only_contain_parameters_we_accept() -> None:
    upstream = '<https://api.github.com/x?page=2&per_page=2&after=Y3Vyc29y>; rel="next"'
    header = rewrite_link_header(
        upstream, base_url="http://localhost:8000/issues", passthrough={"state": "all"}
    )
    query = parse_qs(urlsplit(parse_link_header(header)["next"]).query)
    assert set(query) == {"state", "page", "per_page"}


# --- rewriting -------------------------------------------------------------


def test_rewrite_points_at_our_service_and_keeps_filters() -> None:
    header = rewrite_link_header(
        GITHUB_LINK,
        base_url="http://localhost:8000/issues",
        passthrough={"state": "closed", "labels": "bug,gateway"},
    )
    links = parse_link_header(header)

    assert set(links) == {"next", "last"}
    for url in links.values():
        assert url.startswith("http://localhost:8000/issues?")
        assert "api.github.com" not in url

    query = parse_qs(urlsplit(links["next"]).query)
    assert query["page"] == ["2"]
    assert query["state"] == ["closed"], "the caller's filter wins over GitHub's echo"
    assert query["labels"] == ["bug,gateway"]


def test_rewrite_drops_empty_passthrough_values() -> None:
    header = rewrite_link_header(
        GITHUB_LINK,
        base_url="http://localhost:8000/issues",
        passthrough={"state": "open", "labels": None, "sort": ""},
    )
    query = parse_qs(urlsplit(parse_link_header(header)["next"]).query)
    assert set(query) == {"state", "page"}


def test_rewrite_preserves_per_page() -> None:
    upstream = '<https://api.github.com/x?page=3&per_page=100>; rel="next"'
    header = rewrite_link_header(upstream, base_url="http://localhost:8000/issues")
    query = parse_qs(urlsplit(parse_link_header(header)["next"]).query)
    assert query == {"page": ["3"], "per_page": ["100"]}


@pytest.mark.parametrize("upstream", [None, "", "not a link header"])
def test_rewrite_returns_none_when_there_is_nothing_to_rewrite(upstream: str | None) -> None:
    assert rewrite_link_header(upstream, base_url="http://localhost:8000/issues") is None


def test_rewrite_handles_a_cursorless_link() -> None:
    header = rewrite_link_header(
        '<https://api.github.com/x>; rel="next"', base_url="http://localhost:8000/issues"
    )
    assert header == '<http://localhost:8000/issues>; rel="next"'


# --- clamping --------------------------------------------------------------


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [(None, 30), (1, 1), (30, 30), (100, 100), (101, 100), (0, 1), (-5, 1)],
)
def test_normalize_per_page_clamps_to_githubs_range(supplied: int | None, expected: int) -> None:
    assert normalize_per_page(supplied) == expected

"""RFC 8288 ``Link`` header parsing, and rewriting GitHub's links as ours.

GitHub paginates with a ``Link`` header whose URLs point at api.github.com.
Handing those to our clients would leak the upstream and hand out URLs they
cannot call, so every rel is rebuilt against this service's own base URL while
the ``page``/``per_page`` cursors GitHub chose are preserved verbatim.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit

DEFAULT_PER_PAGE = 30
MAX_PER_PAGE = 100

# <url>; rel="next", <url>; rel="last"   (params may appear in any order)
_LINK_SEGMENT = re.compile(r"<(?P<url>[^>]*)>\s*(?P<params>(?:;[^,]*)*)")
_PARAM = re.compile(r';\s*(?P<key>[^=;\s]+)\s*=\s*(?:"(?P<quoted>[^"]*)"|(?P<bare>[^;,\s]*))')

# The rels GitHub emits, in the order clients expect to see them.
REL_ORDER = ("first", "prev", "next", "last")


def parse_link_header(value: str | None) -> dict[str, str]:
    """Parse a ``Link`` header into ``{rel: url}``.

    Unparseable segments are skipped rather than raising -- a malformed
    upstream header should degrade pagination, not fail the request.
    """
    if not value:
        return {}

    links: dict[str, str] = {}
    for match in _LINK_SEGMENT.finditer(value):
        url = match.group("url").strip()
        if not url:
            continue
        for param in _PARAM.finditer(match.group("params") or ""):
            if param.group("key").strip().lower() != "rel":
                continue
            rel = (param.group("quoted") or param.group("bare") or "").strip()
            # rel="next prev" is legal RFC 8288; register each token.
            for token in rel.split():
                if token and token not in links:
                    links[token] = url
    return links


def build_link_header(links: dict[str, str]) -> str:
    """Serialise ``{rel: url}`` back into a ``Link`` header value."""
    ordered = [rel for rel in REL_ORDER if rel in links]
    ordered += [rel for rel in links if rel not in REL_ORDER]
    return ", ".join(f'<{links[rel]}>; rel="{rel}"' for rel in ordered)


# Only the cursors this API itself accepts. GitHub has begun adding opaque
# `after`/`before` cursors to its own Link headers; forwarding those would put
# parameters in our response that our routes ignore, which is a link that lies
# about what it does. Page numbers work on the same endpoint and are what the
# contract documents.
FORWARDED_CURSORS = ("page", "per_page")


def page_params(url: str) -> dict[str, str]:
    """Extract the pagination cursors this API honours from a URL's query."""
    query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    return {key: query[key] for key in FORWARDED_CURSORS if key in query}


def rewrite_link_header(
    upstream_link: str | None,
    *,
    base_url: str,
    passthrough: dict[str, object] | None = None,
) -> str | None:
    """Rebuild GitHub's ``Link`` header against *base_url*.

    ``passthrough`` carries the caller's own filters (state, labels, ...) so a
    client can follow ``rel="next"`` and keep the same query it started with.
    """
    upstream = parse_link_header(upstream_link)
    if not upstream:
        return None

    base = base_url.rstrip("?&")
    rewritten: dict[str, str] = {}
    for rel, url in upstream.items():
        query: dict[str, object] = {
            k: v for k, v in (passthrough or {}).items() if v is not None and v != ""
        }
        query.update(page_params(url))
        rewritten[rel] = f"{base}?{urlencode(query, doseq=True)}" if query else base
    return build_link_header(rewritten)


def normalize_per_page(value: int | None) -> int:
    """Clamp ``per_page`` into GitHub's accepted 1..100 range."""
    if value is None:
        return DEFAULT_PER_PAGE
    return max(1, min(int(value), MAX_PER_PAGE))

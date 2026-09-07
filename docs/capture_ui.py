"""Drive the gateway's Swagger UI in a real browser and capture screenshots.

Produces the images `docs/build_walkthrough.py` assembles into the Word
document. Every shot is a genuine interaction against a running service and a
real GitHub repository -- nothing is mocked or staged.

    python docs/capture_ui.py            # service must be running on $PORT

Requires `pip install -r docs/requirements.txt`. Uses the system Chrome via
channel="chrome", so there is no browser download.

Two things about Swagger UI that shape this file:

* A whole `.opblock` is 5,000-10,000 px tall once expanded, almost all of it
  the documented-responses reference. Dropped into a Word page that is a
  two-foot-tall strip of unreadable grey. So each step is stitched from the
  few parts that carry the story -- the operation header, the inputs, the curl
  that was actually sent, and the live response -- into one page-shaped image.
* "Try it out" enforces the contract client-side. A parameter typed
  `format: uuid` will not send anything else, and it fails silently.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path

from PIL import Image
from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "docs" / "screenshots"
TMP = SHOTS / ".parts"

# `localhost`, not `127.0.0.1`: "Try it out" sends requests to the server URL
# declared in the contract, so loading /docs under a different spelling of the
# same host makes every call cross-origin and the browser blocks it.
BASE = os.environ.get("SERVICE_URL", f"http://localhost:{os.environ.get('PORT', '8000')}")
VIEWPORT = {"width": 1500, "height": 1050}
SCALE = 2

# The parts of an operation block worth showing, in reading order. Missing ones
# are skipped: a GET has no request body, a pre-execute shot has no response.
PARTS = (
    ".opblock-summary",
    "table.parameters",
    ".opblock-section-request-body",
    ".curl-command",
    ".live-responses-table",
)

manifest: list[dict] = []


def _env_secret() -> str:
    if value := os.environ.get("WEBHOOK_SECRET"):
        return value
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("WEBHOOK_SECRET="):
                return line.split("=", 1)[1].strip()
    return ""


def _record(name: str, caption: str, status: str = "") -> None:
    path = SHOTS / f"{name}.png"
    with Image.open(path) as im:
        size = (im.width, im.height)
    manifest.append(
        {
            "name": name,
            "file": path.name,
            "caption": caption,
            "status": status,
            "width": size[0],
            "height": size[1],
        }
    )
    print(f"  {name:<26} {size[0]:>5}x{size[1]:<6} {path.stat().st_size / 1024:7.1f} KB")


def shot_page(page: Page, name: str, caption: str, *, full: bool = False, status: str = "") -> None:
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=full)
    _record(name, caption, status)


def shot_element(page: Page, name: str, caption: str, selector: str, status: str = "") -> None:
    element = page.locator(selector).first
    element.scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    element.screenshot(path=str(SHOTS / f"{name}.png"))
    _record(name, caption, status)


def _join(name: str, caption: str, tiles: list[Path], status: str = "") -> None:
    """Join captured tiles into one page-shaped image."""
    if not tiles:
        raise RuntimeError(f"no parts captured for {name}")

    images = [Image.open(t).convert("RGB") for t in tiles]
    gap = 10 * SCALE
    width = max(i.width for i in images)
    height = sum(i.height for i in images) + gap * (len(images) - 1)

    canvas = Image.new("RGB", (width, height), "white")
    y = 0
    for image in images:
        canvas.paste(image, (0, y))
        y += image.height + gap
        image.close()
    canvas.save(SHOTS / f"{name}.png")
    canvas.close()

    for tile in tiles:
        tile.unlink()
    _record(name, caption, status)


def _capture_tiles(page: Page, name: str, locators) -> list[Path]:
    TMP.mkdir(parents=True, exist_ok=True)
    tiles: list[Path] = []
    for index, locator in enumerate(locators):
        if not locator.count():
            continue
        box = locator.bounding_box()
        if not box or box["height"] < 4:
            continue
        locator.scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        tile = TMP / f"{name}-{index}.png"
        locator.screenshot(path=str(tile))
        tiles.append(tile)
    return tiles


def stack(page: Page, name: str, caption: str, op_name: str, parts=PARTS) -> None:
    """Screenshot each present part of an operation and join them vertically.

    The observed HTTP status is read off the page and stored in the manifest,
    so the document's summary table reports what actually happened rather than
    what the author expected.
    """
    locators = [page.locator(f"{op(op_name)} {part}").first for part in parts]
    _join(name, caption, _capture_tiles(page, name, locators), response_status(page, op_name))


def stack_all(page: Page, name: str, caption: str, selector: str) -> None:
    """Join every element matching *selector*, in document order."""
    _join(name, caption, _capture_tiles(page, name, page.locator(selector).all()))


def op(name: str) -> str:
    return f"#operations-{name}"


def expand(page: Page, name: str) -> None:
    block = page.locator(op(name))
    if "is-open" not in (block.get_attribute("class") or ""):
        block.locator(".opblock-summary").click()
    page.wait_for_timeout(400)


def collapse_all(page: Page) -> None:
    for block in page.locator(".opblock.is-open").all():
        block.locator(".opblock-summary").click()
        page.wait_for_timeout(120)


def try_it_out(page: Page, name: str) -> None:
    """Enter try-out mode if not already in it.

    After a call has run, Swagger UI renders both "Cancel" and "Reset" with the
    same `try-out__btn` class, so a bare class selector matches two elements.
    The presence of `.cancel` is the reliable signal that try-out is active.
    """
    block = page.locator(op(name))
    if block.locator("button.try-out__btn.cancel").count():
        return
    button = block.locator("button.try-out__btn").first
    if button.count():
        button.click()
        page.wait_for_timeout(300)


def set_body(page: Page, name: str, payload: str) -> None:
    page.locator(f"{op(name)} textarea.body-param__text").fill(payload)
    page.wait_for_timeout(200)


def set_param(page: Page, name: str, param: str, value: str) -> None:
    """Set one parameter, whichever control the contract made Swagger UI render.

    A parameter with an `enum` (state, sort, X-GitHub-Event, ...) becomes a
    <select>; everything else is an <input>. Assuming one or the other is how
    this quietly times out.
    """
    row = page.locator(f'{op(name)} tr[data-param-name="{param}"]')
    select = row.locator("select")
    if select.count():
        select.first.select_option(value)
    else:
        row.locator("input").first.fill(value)
    page.wait_for_timeout(120)


def execute(page: Page, name: str) -> None:
    page.locator(f"{op(name)} button.execute").click()
    page.wait_for_selector(f"{op(name)} .live-responses-table", timeout=30000)
    page.wait_for_timeout(800)


def response_body(page: Page, name: str) -> str:
    pre = page.locator(f"{op(name)} .live-responses-table .microlight").first
    return pre.inner_text() if pre.count() else ""


def response_status(page: Page, name: str) -> str:
    # `tbody` matters: the thead has a cell of the same class reading "Code".
    cell = page.locator(f"{op(name)} .live-responses-table tbody td.response-col_status").first
    text = cell.inner_text() if cell.count() else ""
    match = re.search(r"\d{3}", text)
    return match.group(0) if match else text.strip()


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(TMP, ignore_errors=True)
    for stale in SHOTS.glob("*.png"):
        stale.unlink()

    secret = _env_secret()
    owner = os.environ.get("GITHUB_OWNER", "owner")
    repo = os.environ.get("GITHUB_REPO", "repo")
    print(f"Capturing {BASE}/docs -> {SHOTS}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=SCALE)
        page.goto(f"{BASE}/docs", wait_until="networkidle")
        page.wait_for_selector(".opblock", timeout=20000)
        page.wait_for_timeout(900)

        # -- 01 the contract, rendered ------------------------------------
        # Split in two: the whole page at full width would be an eleven-inch
        # strip, unreadable on any page it lands on.
        shot_element(
            page,
            "01-overview",
            "The service serves its hand-written openapi.yaml at /docs, so this "
            "page is the contract itself, not a description generated from the "
            "code. The server dropdown lists both spellings of localhost, "
            "because Try it out sends requests to the selected server URL.",
            ".information-container",
        )
        stack_all(
            page,
            "01b-operations",
            "Ten operations in three groups. There is deliberately no DELETE: "
            "GitHub's REST API has no delete-issue endpoint, so removal is "
            "PATCH /issues/{number} with state=closed.",
            ".opblock-tag-section",
        )

        # -- 02 security schemes ------------------------------------------
        page.locator("button.btn.authorize").first.click()
        page.wait_for_timeout(700)
        shot_element(
            page,
            "02-security-schemes",
            "The two credentials the contract declares. githubToken is the "
            "server-side PAT the gateway uses to call GitHub; webhookSignature "
            "is the HMAC header GitHub sends. Neither is ever accepted from a "
            "client of this API.",
            ".dialog-ux .modal-ux",
        )
        page.locator(
            ".dialog-ux .modal-ux .btn-done, .dialog-ux button:has-text('Close')"
        ).first.click()
        page.wait_for_timeout(400)

        # -- 03 create an issue: the request -------------------------------
        expand(page, "issues-createIssue")
        try_it_out(page, "issues-createIssue")
        set_body(
            page,
            "issues-createIssue",
            json.dumps(
                {
                    "title": "Rate limiter drops the Retry-After header",
                    "body": "Filed from the Swagger UI while capturing this walkthrough.",
                    "labels": ["bug", "gateway"],
                },
                indent=2,
            ),
        )
        stack(
            page,
            "03-create-request",
            "POST /issues, filled in through Try it out. The editor is "
            "pre-populated from the contract's CreateIssueRequest schema, which "
            "is why the field names and types are already correct.",
            "issues-createIssue",
        )

        execute(page, "issues-createIssue")
        status = response_status(page, "issues-createIssue")
        print(f"    create -> {status}")
        try:
            number = json.loads(response_body(page, "issues-createIssue"))["number"]
        except Exception:
            print("    could not read the issue number from the response", file=sys.stderr)
            return 1
        stack(
            page,
            "04-create-response",
            f"201 Created. The Location header points at /issues/{number} on this "
            "service, and X-RateLimit-Remaining is GitHub's own budget passed "
            "through so a caller can pace itself.",
            "issues-createIssue",
        )
        collapse_all(page)

        # -- 05 the same call, rejected ------------------------------------
        expand(page, "issues-createIssue")
        try_it_out(page, "issues-createIssue")
        set_body(page, "issues-createIssue", json.dumps({"body": "no title supplied"}, indent=2))
        execute(page, "issues-createIssue")
        print(f"    create without title -> {response_status(page, 'issues-createIssue')}")
        stack(
            page,
            "05-validation-400",
            "The same endpoint with the title omitted: 400, not FastAPI's default "
            "422, because the contract specifies 400. The details array names the "
            "offending field, and no GitHub call was made -- invalid input costs "
            "no rate-limit budget.",
            "issues-createIssue",
        )
        collapse_all(page)

        # -- 06 list, with the pagination headers --------------------------
        expand(page, "issues-listIssues")
        try_it_out(page, "issues-listIssues")
        set_param(page, "issues-listIssues", "state", "open")
        set_param(page, "issues-listIssues", "per_page", "3")
        execute(page, "issues-listIssues")
        print(f"    list -> {response_status(page, 'issues-listIssues')}")
        stack(
            page,
            "06-list-issues",
            "GET /issues with per_page=3. In the response headers: Link, rewritten "
            "to point back at this service instead of api.github.com; ETag, for "
            "conditional GET; and X-Page / X-Per-Page echoing what was applied.",
            "issues-listIssues",
        )
        collapse_all(page)

        # -- 07 read one ----------------------------------------------------
        expand(page, "issues-getIssue")
        try_it_out(page, "issues-getIssue")
        set_param(page, "issues-getIssue", "number", str(number))
        execute(page, "issues-getIssue")
        print(f"    get #{number} -> {response_status(page, 'issues-getIssue')}")
        stack(
            page,
            "07-get-issue",
            f"GET /issues/{number} returns the issue just created, projected onto "
            "the fields the contract documents rather than GitHub's raw payload.",
            "issues-getIssue",
        )
        collapse_all(page)

        # -- 08 close: this API's delete -------------------------------------
        expand(page, "issues-updateIssue")
        try_it_out(page, "issues-updateIssue")
        set_param(page, "issues-updateIssue", "number", str(number))
        set_body(
            page,
            "issues-updateIssue",
            json.dumps({"state": "closed", "state_reason": "completed"}, indent=2),
        )
        execute(page, "issues-updateIssue")
        print(f"    close #{number} -> {response_status(page, 'issues-updateIssue')}")
        stack(
            page,
            "08-close-issue",
            'PATCH with {"state": "closed"} -- the D in CRUD. GitHub has no '
            "delete-issue endpoint, so closing is how this API deletes. Note "
            "closed_at is now set.",
            "issues-updateIssue",
        )
        collapse_all(page)

        # -- 09 reopen --------------------------------------------------------
        expand(page, "issues-updateIssue")
        try_it_out(page, "issues-updateIssue")
        set_param(page, "issues-updateIssue", "number", str(number))
        set_body(page, "issues-updateIssue", json.dumps({"state": "open"}, indent=2))
        execute(page, "issues-updateIssue")
        print(f"    reopen #{number} -> {response_status(page, 'issues-updateIssue')}")
        stack(
            page,
            "09-reopen-issue",
            "Reopening the same issue. closed_at is back to null, which is how you "
            "can tell the close really was reversible -- the reason modelling "
            "delete as close is honest rather than a workaround.",
            "issues-updateIssue",
        )
        collapse_all(page)

        # -- 10 comment --------------------------------------------------------
        expand(page, "issues-createComment")
        try_it_out(page, "issues-createComment")
        set_param(page, "issues-createComment", "number", str(number))
        set_body(
            page,
            "issues-createComment",
            json.dumps({"body": "Confirmed on staging -- see the attached trace."}, indent=2),
        )
        execute(page, "issues-createComment")
        print(f"    comment on #{number} -> {response_status(page, 'issues-createComment')}")
        stack(
            page,
            "10-create-comment",
            "201 Created for a comment, with a Location header pointing at the "
            "issue's comment collection.",
            "issues-createComment",
        )
        collapse_all(page)

        # -- 11 read the comment back ------------------------------------------
        expand(page, "issues-listComments")
        try_it_out(page, "issues-listComments")
        set_param(page, "issues-listComments", "number", str(number))
        execute(page, "issues-listComments")
        print(f"    list comments -> {response_status(page, 'issues-listComments')}")
        stack(
            page,
            "11-list-comments",
            "The comment read back from GitHub, confirming the write landed and "
            "is visible through the same gateway that created it.",
            "issues-listComments",
        )
        collapse_all(page)

        # -- 12 / 13 webhook: signed, then tampered ----------------------------
        payload = json.dumps(
            {
                "action": "opened",
                "issue": {"number": number, "title": "Walkthrough delivery", "state": "open"},
                "repository": {"full_name": f"{owner}/{repo}"},
                "sender": {"login": owner},
            }
        )
        # Must be a real UUID: the contract types this header `format: uuid`, and
        # Swagger UI validates it client-side and silently refuses to send
        # anything else. GitHub's delivery ids are UUIDs, so the contract is right.
        delivery = str(uuid.uuid4())
        tampered_delivery = str(uuid.uuid4())
        signature = (
            "sha256=" + hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        )

        expand(page, "webhooks-receiveWebhook")
        try_it_out(page, "webhooks-receiveWebhook")
        set_param(page, "webhooks-receiveWebhook", "X-GitHub-Event", "issues")
        set_param(page, "webhooks-receiveWebhook", "X-GitHub-Delivery", delivery)
        set_param(page, "webhooks-receiveWebhook", "X-Hub-Signature-256", signature)
        set_body(page, "webhooks-receiveWebhook", payload)
        execute(page, "webhooks-receiveWebhook")
        print(f"    webhook (signed) -> {response_status(page, 'webhooks-receiveWebhook')}")
        stack(
            page,
            "12-webhook-valid",
            "A correctly signed delivery: 204 No Content, no body. The gateway "
            "verified the HMAC, stored the delivery, and acked before doing any "
            "interpretation -- GitHub gives a receiver ten seconds.",
            "webhooks-receiveWebhook",
        )

        # Same signature, one word changed in the body.
        set_param(page, "webhooks-receiveWebhook", "X-GitHub-Delivery", tampered_delivery)
        set_body(page, "webhooks-receiveWebhook", payload.replace('"opened"', '"closed"'))
        execute(page, "webhooks-receiveWebhook")
        print(f"    webhook (tampered) -> {response_status(page, 'webhooks-receiveWebhook')}")
        stack(
            page,
            "13-webhook-tampered",
            "The same signature against a body with one word changed: 401. This "
            "is the check that separates a webhook receiver from an open write "
            "endpoint. The comparison is constant-time, and the signature is "
            "never echoed back or logged.",
            "webhooks-receiveWebhook",
        )
        collapse_all(page)

        # -- 14 the delivery log ------------------------------------------------
        expand(page, "webhooks-listEvents")
        try_it_out(page, "webhooks-listEvents")
        set_param(page, "webhooks-listEvents", "limit", "5")
        execute(page, "webhooks-listEvents")
        print(f"    events -> {response_status(page, 'webhooks-listEvents')}")
        stack(
            page,
            "14-events",
            "GET /events shows the accepted delivery with status 'processed'. The "
            "tampered one is absent: it never got past the signature check, so it "
            "was never stored.",
            "webhooks-listEvents",
        )
        collapse_all(page)

        # -- 15 health -----------------------------------------------------------
        expand(page, "ops-healthz")
        try_it_out(page, "ops-healthz")
        execute(page, "ops-healthz")
        print(f"    healthz -> {response_status(page, 'ops-healthz')}")
        stack(
            page,
            "15-healthz",
            "/healthz reports process health and configuration completeness "
            "without calling GitHub, so an upstream outage or an exhausted rate "
            "limit cannot make an orchestrator kill a healthy container.",
            "ops-healthz",
        )
        collapse_all(page)

        # -- 16 schemas ------------------------------------------------------------
        # The models section is open by default; clicking it would close it.
        shot_element(
            page,
            "16-schemas",
            "The reusable components/schemas the contract defines: Issue, "
            "Comment, Error and the request models. Every response in the "
            "document above is an instance of one of these.",
            "section.models",
        )

        # -- 17 redoc ---------------------------------------------------------------
        page.goto(f"{BASE}/redoc", wait_until="networkidle")
        page.wait_for_timeout(3000)
        shot_page(
            page,
            "17-redoc",
            "The same openapi.yaml rendered by ReDoc at /redoc. Two renderers, "
            "one contract, no second source of truth to drift.",
        )

        browser.close()

    # Manifest first: cleanup must never be able to lose the run's results.
    (SHOTS / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(manifest)} screenshots -> {SHOTS}")
    print(f"issue used: #{number}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

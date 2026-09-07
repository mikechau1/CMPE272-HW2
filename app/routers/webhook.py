"""GitHub webhook receiver.

The contract with GitHub is: **ack fast, never lose a delivery, tolerate
replays.**  This handler is written in that order.

1. Verify the HMAC before touching the body (:mod:`app.security`).  An
   unsigned or mis-signed delivery is 401 and nothing else happens.
2. Validate the event/action against a known set -- 400 otherwise.
3. Persist the raw delivery.  This is the durable step and it is idempotent:
   the store's ``(delivery_id, event, action)`` unique index turns GitHub's
   retry, or a manual "Redeliver", into a no-op that still acks 2xx.
4. Ack 204 and do the interpretation work in a background task, so a slow
   consumer can never push us past GitHub's 10s delivery timeout.

Poison handling: a background task that raises marks its row ``failed`` with
the error text and stops.  It is never retried in-band -- the delivery is
already durable and is visible via ``GET /events`` for inspection or manual
replay.  Nothing about a failure is signalled back to GitHub, because the
delivery *was* accepted.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..config import Settings
from ..deps import get_settings_dep, get_store
from ..errors import BadRequest, NotConfigured, PayloadTooLarge, Unauthorized
from ..logging_config import get_logger
from ..security import SIGNATURE_HEADER, verify_signature
from ..store import EventStore, StoredEvent

logger = get_logger(__name__)
router = APIRouter(tags=["webhooks"])

EVENT_HEADER = "X-GitHub-Event"
DELIVERY_HEADER = "X-GitHub-Delivery"

# https://docs.github.com/webhooks/webhook-events-and-payloads
ISSUE_ACTIONS = frozenset(
    {
        "opened", "edited", "deleted", "transferred", "pinned", "unpinned",
        "closed", "reopened", "assigned", "unassigned", "labeled", "unlabeled",
        "locked", "unlocked", "milestoned", "demilestoned", "typed", "untyped",
        "parent_issue_added", "parent_issue_removed",
        "sub_issue_added", "sub_issue_removed",
    }
)  # fmt: skip
COMMENT_ACTIONS = frozenset({"created", "edited", "deleted"})

SUPPORTED_EVENTS: dict[str, frozenset[str]] = {
    "issues": ISSUE_ACTIONS,
    "issue_comment": COMMENT_ACTIONS,
    "ping": frozenset(),
}


def _summarize(event: str, action: str | None, payload: dict[str, Any]) -> str:
    """One human-readable line for the log; the payload stays in the store."""
    issue = payload.get("issue") or {}
    number = issue.get("number")
    title = (issue.get("title") or "")[:80]
    sender = (payload.get("sender") or {}).get("login", "unknown")

    if event == "ping":
        return f"ping from {payload.get('hook', {}).get('type', 'repository')} hook"
    if event == "issues":
        return f"{sender} {action} issue #{number}: {title!r}"
    if event == "issue_comment":
        comment_id = (payload.get("comment") or {}).get("id")
        return f"{sender} {action} comment {comment_id} on issue #{number}"
    return f"{event}/{action}"  # pragma: no cover - guarded by SUPPORTED_EVENTS


async def _process(store: EventStore, record: StoredEvent, payload: dict[str, Any]) -> None:
    """Post-ack work.  Must never raise: the delivery is already acknowledged."""
    try:
        summary = _summarize(record.event, record.action, payload)
        logger.info(
            "webhook processed",
            extra={
                "delivery_id": record.delivery_id,
                "event": record.event,
                "action": record.action,
                "issue_number": record.issue_number,
                "summary": summary,
            },
        )
        await run_in_threadpool(store.mark_processed, record.id)
    except Exception as exc:  # noqa: BLE001 - poison delivery, park it
        logger.exception(
            "webhook processing failed",
            extra={"delivery_id": record.delivery_id, "event_row_id": record.id},
        )
        try:
            await run_in_threadpool(store.mark_failed, record.id, f"{type(exc).__name__}: {exc}")
        except Exception:  # pragma: no cover - store itself is broken
            logger.exception("could not mark delivery failed")


def _extract(payload: dict[str, Any]) -> dict[str, Any]:
    issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
    repo = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    number = issue.get("number")
    return {
        "issue_number": number if isinstance(number, int) else None,
        "repository": repo.get("full_name"),
        "sender": sender.get("login"),
    }


@router.post("/webhook", status_code=204)
async def receive_webhook(
    request: Request,
    background: BackgroundTasks,
    settings: Annotated[Settings, Depends(get_settings_dep)],
    store: Annotated[EventStore, Depends(get_store)],
) -> Response:
    if not settings.has_webhook_secret:
        # Refuse rather than accept unsigned traffic.
        raise NotConfigured(
            "WEBHOOK_SECRET is not set, so delivery signatures cannot be verified. "
            "The gateway refuses unsigned webhook traffic."
        )

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > settings.max_webhook_body_bytes:
        raise PayloadTooLarge(
            f"Webhook body exceeds the {settings.max_webhook_body_bytes} byte limit."
        )

    body = await request.body()
    if len(body) > settings.max_webhook_body_bytes:
        raise PayloadTooLarge(
            f"Webhook body exceeds the {settings.max_webhook_body_bytes} byte limit."
        )

    # --- 1. signature, before anything reads the body -----------------------
    if not verify_signature(settings.webhook_secret, body, request.headers.get(SIGNATURE_HEADER)):
        # Deliberately vague, and the offending signature is never logged.
        logger.warning(
            "webhook signature rejected",
            extra={
                "event": request.headers.get(EVENT_HEADER),
                "signature_present": SIGNATURE_HEADER in request.headers,
                "body_bytes": len(body),
            },
        )
        raise Unauthorized(
            f"Invalid or missing {SIGNATURE_HEADER}. The delivery body did not match "
            "an HMAC-SHA256 computed with WEBHOOK_SECRET.",
            code="invalid_signature",
        )

    # --- 2. event / action must be ones we understand -----------------------
    event = (request.headers.get(EVENT_HEADER) or "").strip()
    if not event:
        raise BadRequest(
            f"Missing {EVENT_HEADER} header.",
            code="missing_event",
        )
    if event not in SUPPORTED_EVENTS:
        raise BadRequest(
            f"Unsupported webhook event {event!r}. This gateway handles: "
            f"{', '.join(sorted(SUPPORTED_EVENTS))}.",
            code="unsupported_event",
            details={"event": event, "supported": sorted(SUPPORTED_EVENTS)},
        )

    try:
        payload = json.loads(body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BadRequest("Webhook body is not valid JSON.", code="invalid_json") from exc
    if not isinstance(payload, dict):
        raise BadRequest("Webhook body must be a JSON object.", code="invalid_json")

    action = payload.get("action")
    allowed = SUPPORTED_EVENTS[event]
    if allowed:
        if not isinstance(action, str) or not action:
            raise BadRequest(
                f"Webhook event {event!r} requires an 'action' field.",
                code="missing_action",
            )
        if action not in allowed:
            raise BadRequest(
                f"Unsupported action {action!r} for event {event!r}.",
                code="unsupported_action",
                details={"event": event, "action": action, "supported": sorted(allowed)},
            )
    else:
        action = None  # `ping` carries no action

    # GitHub always sends a delivery GUID; fall back to a content hash so a
    # hand-rolled curl replay still dedupes deterministically.
    delivery_id = (request.headers.get(DELIVERY_HEADER) or "").strip()[:128]
    if not delivery_id:
        delivery_id = f"sha256:{hashlib.sha256(body).hexdigest()[:32]}"

    # --- 3. persist (durable + idempotent) ----------------------------------
    fields = _extract(payload)
    record, duplicate = await run_in_threadpool(
        store.record,
        delivery_id=delivery_id,
        event=event,
        action=action,
        payload=payload,
        **fields,
    )

    if duplicate:
        logger.info(
            "webhook duplicate ignored",
            extra={
                "delivery_id": delivery_id,
                "event": event,
                "action": action,
                "event_row_id": record.id,
                "original_status": record.status,
            },
        )
        # Same ack as the first time: a redelivery is a success, not a conflict.
        return Response(status_code=204)

    logger.info(
        "webhook accepted",
        extra={
            "delivery_id": delivery_id,
            "event": event,
            "action": action,
            "issue_number": record.issue_number,
            "event_row_id": record.id,
        },
    )

    # --- 4. ack now, interpret afterwards -----------------------------------
    background.add_task(_process, store, record, payload)
    return Response(status_code=204)


@router.get("/events", tags=["webhooks"])
async def list_events(
    store: Annotated[EventStore, Depends(get_store)],
    limit: Annotated[int, Query(ge=1, le=500, description="How many deliveries to return.")] = 50,
    event: Annotated[str | None, Query(description="Filter by event type.")] = None,
) -> Response:
    if event is not None and event not in SUPPORTED_EVENTS:
        raise BadRequest(
            f"Unknown event filter {event!r}. Known events: {', '.join(sorted(SUPPORTED_EVENTS))}.",
            code="unsupported_event",
        )
    events = await run_in_threadpool(store.list_recent, limit, event=event)
    return JSONResponse([item.to_dict() for item in events], status_code=200)

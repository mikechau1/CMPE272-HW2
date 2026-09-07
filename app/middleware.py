"""Request-scoped observability.

Assigns (or adopts) a request id, binds it plus the GitHub delivery id into
the logging context, emits one structured access-log line per request, and
echoes ``X-Request-ID`` back so a client can quote it in a bug report.
"""

from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from .logging_config import delivery_id_ctx, get_logger, request_id_ctx

logger = get_logger("app.access")

REQUEST_ID_HEADER = "X-Request-ID"
DELIVERY_ID_HEADER = "X-GitHub-Delivery"


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "").strip()
        # Bound the length so a hostile client cannot bloat every log line.
        request_id = incoming[:128] if incoming else uuid.uuid4().hex
        delivery_id = (request.headers.get(DELIVERY_ID_HEADER, "") or "").strip()[:128] or None

        request_token = request_id_ctx.set(request_id)
        delivery_token = delivery_id_ctx.set(delivery_id)
        request.state.request_id = request_id
        request.state.delivery_id = delivery_id

        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "query": str(request.url.query) or None,
                    "status": status,
                    "duration_ms": duration_ms,
                    "client": request.client.host if request.client else None,
                },
            )
            request_id_ctx.reset(request_token)
            delivery_id_ctx.reset(delivery_token)

"""SQLite-backed store for webhook deliveries.

Why persist at all: GitHub retries a delivery it could not confirm, and the
grader will redeliver from the UI.  Both mean the *same* delivery can arrive
more than once, so the store -- not the handler -- is where idempotency lives.

Dedupe key: ``(delivery_id, event, action)``.  A redelivery reuses GitHub's
``X-GitHub-Delivery`` GUID, so the unique index turns a replay into a no-op
insert and :meth:`record` reports it as a duplicate.  ``action`` is stored as
``''`` rather than NULL because SQLite treats NULLs as distinct inside a
unique index, which would let ``ping`` replays through.

The connection is opened with ``check_same_thread=False`` and every statement
runs under a lock, because Starlette dispatches these calls from a worker
thread pool rather than a single thread.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS webhook_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id   TEXT    NOT NULL,
    event         TEXT    NOT NULL,
    action        TEXT    NOT NULL DEFAULT '',
    issue_number  INTEGER,
    repository    TEXT,
    sender        TEXT,
    status        TEXT    NOT NULL DEFAULT 'received',
    error         TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    received_at   TEXT    NOT NULL,
    processed_at  TEXT,
    payload       TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_webhook_dedupe
    ON webhook_events (delivery_id, event, action);
CREATE INDEX IF NOT EXISTS ix_webhook_received_at
    ON webhook_events (id DESC);
"""

STATUS_RECEIVED = "received"
STATUS_PROCESSED = "processed"
STATUS_FAILED = "failed"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class StoredEvent:
    id: int
    delivery_id: str
    event: str
    action: str | None
    issue_number: int | None
    repository: str | None
    sender: str | None
    status: str
    error: str | None
    attempts: int
    timestamp: str
    processed_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "delivery_id": self.delivery_id,
            "event": self.event,
            "action": self.action,
            "issue_number": self.issue_number,
            "repository": self.repository,
            "sender": self.sender,
            "status": self.status,
            "error": self.error,
            "attempts": self.attempts,
            "timestamp": self.timestamp,
            "processed_at": self.processed_at,
        }


def _row_to_event(row: sqlite3.Row) -> StoredEvent:
    return StoredEvent(
        id=row["id"],
        delivery_id=row["delivery_id"],
        event=row["event"],
        action=row["action"] or None,
        issue_number=row["issue_number"],
        repository=row["repository"],
        sender=row["sender"],
        status=row["status"],
        error=row["error"],
        attempts=row["attempts"],
        timestamp=row["received_at"],
        processed_at=row["processed_at"],
    )


class EventStore:
    def __init__(self, path: str = ":memory:", retention: int = 500) -> None:
        self.path = path
        self.retention = max(int(retention), 1)
        self._lock = threading.Lock()

        if path not in (":memory:", ""):
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(path or ":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- writes ------------------------------------------------------------

    def record(
        self,
        *,
        delivery_id: str,
        event: str,
        action: str | None,
        issue_number: int | None = None,
        repository: str | None = None,
        sender: str | None = None,
        payload: Any = None,
    ) -> tuple[StoredEvent, bool]:
        """Insert a delivery.  Returns ``(event, is_duplicate)``.

        A duplicate is never an error: the stored row is returned untouched so
        the caller can ack with the same 2xx it gave the first time.
        """
        action_key = action or ""
        body = json.dumps(payload, default=str) if payload is not None else "{}"

        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO webhook_events
                    (delivery_id, event, action, issue_number, repository,
                     sender, status, received_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (delivery_id, event, action) DO NOTHING
                """,
                (
                    delivery_id,
                    event,
                    action_key,
                    issue_number,
                    repository,
                    sender,
                    STATUS_RECEIVED,
                    _now(),
                    body,
                ),
            )
            inserted = cursor.rowcount > 0
            self._conn.commit()

            row = self._conn.execute(
                """
                SELECT * FROM webhook_events
                WHERE delivery_id = ? AND event = ? AND action = ?
                """,
                (delivery_id, event, action_key),
            ).fetchone()

            if inserted:
                self._prune_locked()
                self._conn.commit()

        return _row_to_event(row), not inserted

    def mark_processed(self, event_id: int) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE webhook_events
                   SET status = ?, processed_at = ?, attempts = attempts + 1, error = NULL
                 WHERE id = ?
                """,
                (STATUS_PROCESSED, _now(), event_id),
            )
            self._conn.commit()

    def mark_failed(self, event_id: int, error: str) -> None:
        """Park a poison delivery: recorded, counted, and never retried inline."""
        with self._lock:
            self._conn.execute(
                """
                UPDATE webhook_events
                   SET status = ?, attempts = attempts + 1, error = ?
                 WHERE id = ?
                """,
                (STATUS_FAILED, error[:1000], event_id),
            )
            self._conn.commit()

    # -- reads -------------------------------------------------------------

    def list_recent(self, limit: int = 50, *, event: str | None = None) -> list[StoredEvent]:
        limit = max(1, min(int(limit), 500))
        sql = "SELECT * FROM webhook_events"
        params: list[Any] = []
        if event:
            sql += " WHERE event = ?"
            params.append(event)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_event(row) for row in rows]

    def get_payload(self, event_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM webhook_events WHERE id = ?", (event_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:  # pragma: no cover - payload is written by us
            return None

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM webhook_events").fetchone()[0])

    # -- housekeeping ------------------------------------------------------

    def _prune_locked(self) -> None:
        """Keep the store bounded; this is a debug log, not a system of record."""
        self._conn.execute(
            """
            DELETE FROM webhook_events
             WHERE id NOT IN (
                 SELECT id FROM webhook_events ORDER BY id DESC LIMIT ?
             )
            """,
            (self.retention,),
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

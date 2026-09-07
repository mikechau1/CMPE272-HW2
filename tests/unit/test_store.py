"""Event store: dedupe, ordering, retention, and poison parking."""

from __future__ import annotations

import pytest

from app.store import STATUS_FAILED, STATUS_PROCESSED, STATUS_RECEIVED, EventStore


@pytest.fixture
def store() -> EventStore:
    instance = EventStore(":memory:", retention=500)
    yield instance
    instance.close()


def _record(store: EventStore, **overrides):
    payload = {
        "delivery_id": "d-1",
        "event": "issues",
        "action": "opened",
        "issue_number": 12,
        "repository": "mikechau1/cmpe272-issues-gw",
        "sender": "mikechau1",
        "payload": {"action": "opened"},
    }
    payload.update(overrides)
    return store.record(**payload)


def test_first_record_is_not_a_duplicate(store: EventStore) -> None:
    event, duplicate = _record(store)
    assert duplicate is False
    assert event.id > 0
    assert event.status == STATUS_RECEIVED
    assert event.issue_number == 12
    assert event.timestamp.endswith("Z")


def test_identical_delivery_is_reported_as_a_duplicate(store: EventStore) -> None:
    first, _ = _record(store)
    second, duplicate = _record(store)
    assert duplicate is True
    assert second.id == first.id, "a replay must not create a second row"
    assert store.count() == 1


def test_duplicate_does_not_overwrite_processing_state(store: EventStore) -> None:
    event, _ = _record(store)
    store.mark_processed(event.id)
    replay, duplicate = _record(store)
    assert duplicate is True
    assert replay.status == STATUS_PROCESSED


def test_same_guid_different_action_is_a_new_row(store: EventStore) -> None:
    _record(store, delivery_id="g", action="opened")
    _, duplicate = _record(store, delivery_id="g", action="closed")
    assert duplicate is False
    assert store.count() == 2


def test_same_guid_different_event_is_a_new_row(store: EventStore) -> None:
    _record(store, delivery_id="g", event="issues", action="created")
    _, duplicate = _record(store, delivery_id="g", event="issue_comment", action="created")
    assert duplicate is False
    assert store.count() == 2


def test_actionless_events_still_dedupe(store: EventStore) -> None:
    """SQLite treats NULLs as distinct in a unique index; '' is stored instead."""
    _record(store, delivery_id="p", event="ping", action=None)
    event, duplicate = _record(store, delivery_id="p", event="ping", action=None)
    assert duplicate is True
    assert event.action is None
    assert store.count() == 1


def test_list_recent_is_newest_first(store: EventStore) -> None:
    for index in range(5):
        _record(store, delivery_id=f"d-{index}")
    ids = [event.delivery_id for event in store.list_recent()]
    assert ids == ["d-4", "d-3", "d-2", "d-1", "d-0"]


def test_list_recent_honours_the_limit(store: EventStore) -> None:
    for index in range(10):
        _record(store, delivery_id=f"d-{index}")
    assert len(store.list_recent(limit=3)) == 3


@pytest.mark.parametrize(("supplied", "expected"), [(0, 1), (-1, 1), (10_000, 500)])
def test_list_recent_clamps_the_limit(store: EventStore, supplied: int, expected: int) -> None:
    for index in range(3):
        _record(store, delivery_id=f"d-{index}")
    assert len(store.list_recent(limit=supplied)) == min(expected, 3)


def test_list_recent_filters_by_event(store: EventStore) -> None:
    _record(store, delivery_id="a", event="issues")
    _record(store, delivery_id="b", event="issue_comment")
    filtered = store.list_recent(event="issue_comment")
    assert [event.delivery_id for event in filtered] == ["b"]


def test_mark_processed_records_a_timestamp_and_attempt(store: EventStore) -> None:
    event, _ = _record(store)
    store.mark_processed(event.id)
    stored = store.list_recent()[0]
    assert stored.status == STATUS_PROCESSED
    assert stored.processed_at is not None
    assert stored.attempts == 1
    assert stored.error is None


def test_mark_failed_parks_the_delivery_with_its_error(store: EventStore) -> None:
    event, _ = _record(store)
    store.mark_failed(event.id, "ValueError: bad payload")
    stored = store.list_recent()[0]
    assert stored.status == STATUS_FAILED
    assert stored.error == "ValueError: bad payload"
    assert stored.attempts == 1


def test_long_errors_are_truncated(store: EventStore) -> None:
    event, _ = _record(store)
    store.mark_failed(event.id, "x" * 5000)
    assert len(store.list_recent()[0].error) == 1000


def test_payload_is_retrievable_for_debugging(store: EventStore) -> None:
    event, _ = _record(store, payload={"action": "opened", "issue": {"number": 12}})
    assert store.get_payload(event.id) == {"action": "opened", "issue": {"number": 12}}


def test_get_payload_of_an_unknown_row_is_none(store: EventStore) -> None:
    assert store.get_payload(99999) is None


def test_retention_prunes_the_oldest_rows() -> None:
    store = EventStore(":memory:", retention=3)
    try:
        for index in range(6):
            _record(store, delivery_id=f"d-{index}")
        assert store.count() == 3
        assert [event.delivery_id for event in store.list_recent()] == ["d-5", "d-4", "d-3"]
    finally:
        store.close()


def test_retention_of_zero_is_clamped_to_one() -> None:
    store = EventStore(":memory:", retention=0)
    try:
        assert store.retention == 1
    finally:
        store.close()


def test_file_backed_store_creates_its_directory(tmp_path) -> None:
    path = tmp_path / "nested" / "dir" / "events.db"
    store = EventStore(str(path), retention=10)
    try:
        _record(store)
        assert path.exists()
        assert store.count() == 1
    finally:
        store.close()


def test_file_backed_store_survives_reopening(tmp_path) -> None:
    path = str(tmp_path / "events.db")
    first = EventStore(path, retention=10)
    _record(first, delivery_id="persisted")
    first.close()

    second = EventStore(path, retention=10)
    try:
        assert [event.delivery_id for event in second.list_recent()] == ["persisted"]
        _, duplicate = _record(second, delivery_id="persisted")
        assert duplicate is True, "dedupe must survive a restart"
    finally:
        second.close()


def test_record_survives_a_non_serialisable_payload(store: EventStore) -> None:
    event, _ = _record(store, payload={"when": object()})
    assert store.get_payload(event.id) is not None


def test_to_dict_matches_the_documented_event_shape(store: EventStore) -> None:
    event, _ = _record(store)
    keys = set(event.to_dict())
    assert {"id", "event", "action", "issue_number", "timestamp"} <= keys

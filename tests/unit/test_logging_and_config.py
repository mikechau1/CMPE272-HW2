"""Structured logging, secret redaction, and configuration parsing."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import pytest

from app.config import Settings
from app.logging_config import (
    JsonFormatter,
    TextFormatter,
    configure_logging,
    delivery_id_ctx,
    redact,
    request_id_ctx,
)
from tests.conftest import TEST_SECRET, TEST_TOKEN, load_fixture


def _record(msg: str = "hello", **fields: Any) -> logging.LogRecord:
    record = logging.LogRecord("app.test", logging.INFO, __file__, 10, msg, (), None)
    for key, value in fields.items():
        setattr(record, key, value)
    return record


# --- redaction -------------------------------------------------------------


def test_redact_never_returns_the_secret() -> None:
    secret = "github_pat_supersecretvalue"
    rendered = redact(secret)
    assert "supersecret" not in rendered
    assert rendered == f"<redacted:{len(secret)}chars>"


def test_redact_can_keep_a_short_suffix_for_correlation() -> None:
    rendered = redact("abcdefghijklmnop", keep=4)
    assert rendered.endswith("mnop>")
    assert "abcdefghijkl" not in rendered


def test_redact_handles_unset_values() -> None:
    assert redact(None) == "<unset>"
    assert redact("") == "<unset>"


def test_redact_does_not_leak_short_secrets_via_keep() -> None:
    assert redact("abc", keep=8) == "<redacted:3chars>"


# --- formatters ------------------------------------------------------------


def test_json_formatter_emits_one_parseable_object() -> None:
    payload = json.loads(JsonFormatter().format(_record("started", port=8000)))
    assert payload["msg"] == "started"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["port"] == 8000
    assert payload["ts"].endswith("Z")


def test_json_formatter_includes_the_request_context() -> None:
    request_token = request_id_ctx.set("req-1")
    delivery_token = delivery_id_ctx.set("del-9")
    try:
        payload = json.loads(JsonFormatter().format(_record()))
    finally:
        request_id_ctx.reset(request_token)
        delivery_id_ctx.reset(delivery_token)

    assert payload["request_id"] == "req-1"
    assert payload["delivery_id"] == "del-9"


def test_json_formatter_omits_absent_context() -> None:
    payload = json.loads(JsonFormatter().format(_record()))
    assert "request_id" not in payload


def test_json_formatter_survives_unserialisable_extras() -> None:
    payload = json.loads(JsonFormatter().format(_record(thing=object())))
    assert isinstance(payload["thing"], str)


def test_json_formatter_renders_exceptions() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _record("failed")
        record.exc_info = sys.exc_info()
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exception"]


def test_text_formatter_is_readable() -> None:
    rendered = TextFormatter().format(_record("started", port=8000))
    assert "app.test" in rendered
    assert "port=8000" in rendered


def test_configure_logging_installs_exactly_one_handler() -> None:
    configure_logging("DEBUG", "json")
    configure_logging("INFO", "json")
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert root.level == logging.INFO


def test_configure_logging_rejects_an_unknown_format() -> None:
    with pytest.raises(ValueError, match="LOG_FORMAT"):
        Settings(log_format="xml")


# --- no secrets in the log stream -----------------------------------------


def test_startup_and_request_logs_never_contain_secrets(
    client, deliver: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        deliver(load_fixture("webhook_issues_opened.json"))
        client.get("/healthz")

    emitted = "\n".join(
        record.getMessage() + json.dumps(record.__dict__, default=str) for record in caplog.records
    )
    assert TEST_SECRET not in emitted
    assert TEST_TOKEN not in emitted


def test_rejected_signature_is_not_echoed_into_the_log(
    deliver: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    forged = "sha256=" + "beef" * 16
    with caplog.at_level(logging.DEBUG):
        deliver(load_fixture("webhook_issues_opened.json"), signature=forged)

    emitted = "\n".join(
        record.getMessage() + json.dumps(record.__dict__, default=str) for record in caplog.records
    )
    assert "beef" * 16 not in emitted
    assert "signature_present" in emitted, "the fact of a signature is fine; its value is not"


# --- settings --------------------------------------------------------------


def test_settings_read_the_assignment_env_var_names(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "GITHUB_TOKEN": "tok",
        "GITHUB_OWNER": "owner",
        "GITHUB_REPO": "repo",
        "WEBHOOK_SECRET": "sec",
        "PORT": "9123",
    }.items():
        monkeypatch.setenv(name, value)

    settings = Settings(_env_file=None)
    assert settings.github_token == "tok"
    assert settings.repo_slug == "owner/repo"
    assert settings.port == 9123
    assert settings.missing_vars() == []


def test_settings_strip_surrounding_whitespace() -> None:
    assert Settings(github_token="  tok\n", _env_file=None).github_token == "tok"


def test_settings_normalise_the_api_url() -> None:
    assert Settings(github_api_url="https://ghe.example.com/api/v3/").github_api_url.endswith("v3")


def test_missing_vars_lists_every_gap() -> None:
    assert set(Settings(_env_file=None).missing_vars()) == {
        "GITHUB_TOKEN",
        "GITHUB_OWNER",
        "GITHUB_REPO",
        "WEBHOOK_SECRET",
    }


def test_configuration_predicates() -> None:
    settings = Settings(
        github_token="t", github_owner="o", github_repo="r", webhook_secret="s", _env_file=None
    )
    assert settings.has_token and settings.has_repo and settings.has_webhook_secret
    assert Settings(_env_file=None).has_repo is False


@pytest.mark.parametrize("retries", [-1, 11])
def test_retry_count_is_bounded(retries: int) -> None:
    with pytest.raises(ValueError, match="github_max_retries"):
        Settings(github_max_retries=retries)

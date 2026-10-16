from __future__ import annotations

import io
import json
from email.message import Message
from typing import Any
from urllib.error import HTTPError

import main_delivery
import main_transport
import pytest


def test_provider_runtime_accepts_token_only_from_project_secret_resources() -> None:
    runtime = main_transport._provider_runtime_config(
        {
            "provider_secrets": {"bot_token": "project-token"},
            "provider_config": {"bot_token": "config-token", "token": "config-alias"},
            "binding_config": {"bot_token": "binding-token", "token": "binding-alias"},
        }
    )

    assert runtime["token"] == "project-token"


def test_project_token_lookup_never_falls_back_to_global_secret() -> None:
    calls: list[dict[str, Any]] = []

    class _Store:
        def get_provider_secret(self, **kwargs: Any) -> str:
            calls.append(kwargs)
            return "global-token" if kwargs["include_global"] else ""

    assert main_transport._telegram_token_for_project(_Store(), "project-b") == ""
    assert calls == [
        {
            "provider_key": "flowsteward.telegram",
            "secret_key": "bot_token",
            "project_id": "project-b",
            "include_global": False,
        }
    ]


def test_telegram_api_call_redacts_bot_token_from_transport_errors(monkeypatch) -> None:
    token = "123456:SUPER_SECRET"
    encoded_token = "123456%3ASUPER_SECRET"

    def fail_urlopen(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(f"urlopen failed for https://api.telegram.org/bot{encoded_token}/getMe")

    monkeypatch.setattr(main_transport.urlrequest, "urlopen", fail_urlopen)

    response = main_transport._telegram_api_call(token, "getMe")

    assert response["ok"] is False
    assert encoded_token not in response["error"]
    assert "SUPER_SECRET" not in response["error"]
    assert "/bot<redacted>/getMe" in response["error"]


def test_telegram_api_call_preserves_provider_http_error_description(monkeypatch) -> None:
    description = (
        "Conflict: terminated by other getUpdates request; "
        "make sure that only one bot instance is running"
    )

    def fail_urlopen(*_args: Any, **_kwargs: Any) -> None:
        raise _http_error(409, description)

    monkeypatch.setattr(main_transport.urlrequest, "urlopen", fail_urlopen)

    response = main_transport._telegram_api_call("real-token", "getUpdates")

    assert (
        response.items()
        >= {
            "ok": False,
            "error_code": 409,
            "description": description,
            "error": description,
            "failure_class": "provider",
            "retryable": False,
            "definitely_no_external_effect": True,
            "external_effect_status": "failed",
            "http_status": 409,
        }.items()
    )


def test_send_message_redacts_bot_token_from_retry_errors(monkeypatch) -> None:
    token = "654321:MESSAGE_SECRET"
    encoded_token = "654321%3AMESSAGE_SECRET"

    def fail_urlopen(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(f"urlopen failed for https://api.telegram.org/bot{encoded_token}/sendMessage")

    monkeypatch.setattr(main_transport.urlrequest, "urlopen", fail_urlopen)

    response = main_transport._send_message(token, "-100123", "hello")

    assert response["ok"] is False
    assert encoded_token not in response["error"]
    assert "MESSAGE_SECRET" not in response["error"]
    assert "/bot<redacted>/sendMessage" in response["error"]


def _http_error(code: int, description: str) -> HTTPError:
    body = json.dumps({"ok": False, "error_code": code, "description": description}).encode()
    return HTTPError(
        "https://api.telegram.org/botTOKEN/sendMessage",
        code,
        description,
        Message(),
        io.BytesIO(body),
    )


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("timed out after request may have been accepted"),
        ConnectionResetError("connection reset after write"),
        _http_error(500, "Internal Server Error"),
    ],
)
def test_send_message_ambiguous_failures_never_retry(monkeypatch, failure) -> None:
    attempts = []

    def fail_urlopen(*_args: Any, **_kwargs: Any) -> None:
        attempts.append(1)
        raise failure

    monkeypatch.setattr(main_transport.urlrequest, "urlopen", fail_urlopen)

    response = main_transport._send_message("real-token", "-100123", "hello")

    assert len(attempts) == 1
    assert response["ok"] is False
    assert response["external_effect_status"] == "timeout_unknown"
    assert response.get("definitely_no_external_effect") is not True


def test_send_message_invalid_success_envelope_is_ambiguous_and_not_retried(
    monkeypatch,
) -> None:
    attempts = []

    class _InvalidSuccessResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b"{}"

    def urlopen(*_args, **_kwargs):
        attempts.append(1)
        return _InvalidSuccessResponse()

    monkeypatch.setattr(main_transport.urlrequest, "urlopen", urlopen)

    response = main_transport._send_message("real-token", "-100123", "hello")

    assert len(attempts) == 1
    assert response["ok"] is False
    assert response["external_effect_status"] == "timeout_unknown"
    assert response.get("definitely_no_external_effect") is not True


@pytest.mark.parametrize(
    "failure",
    [
        _http_error(400, "Bad Request: chat not found"),
        _http_error(401, "Unauthorized"),
        _http_error(403, "Forbidden"),
        _http_error(404, "Not Found"),
        _http_error(429, "Too Many Requests"),
    ],
)
def test_send_message_explicit_provider_rejections_are_definitive(monkeypatch, failure) -> None:
    attempts = []

    def fail_urlopen(*_args: Any, **_kwargs: Any) -> None:
        attempts.append(1)
        raise failure

    monkeypatch.setattr(main_transport.urlrequest, "urlopen", fail_urlopen)

    response = main_transport._send_message("real-token", "-100123", "hello")

    assert len(attempts) == 1
    assert response["ok"] is False
    assert response["error_code"] == "telegram_send_rejected"
    assert response["provider_status"] == failure.code
    assert response["external_effect_status"] == "failed"
    assert response["definitely_no_external_effect"] is True


def test_send_message_rate_limit_exposes_bounded_retry_facts(monkeypatch) -> None:
    body = json.dumps(
        {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 9},
        }
    ).encode()
    failure = HTTPError(
        "https://api.telegram.org/botTOKEN/sendMessage",
        429,
        "Too Many Requests",
        Message(),
        io.BytesIO(body),
    )
    monkeypatch.setattr(
        main_transport.urlrequest, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(failure)
    )

    response = main_transport._send_message("real-token", "-100123", "hello")

    assert (
        response.items()
        >= {
            "failure_class": "provider",
            "retryable": True,
            "retry_after_seconds": 9.0,
            "definitely_no_external_effect": True,
            "external_effect_status": "failed",
            "provider_error_code": "telegram_send_rejected",
            "http_status": 429,
        }.items()
    )


def test_send_message_retries_only_explicit_parse_entity_rejection(monkeypatch) -> None:
    attempts = []

    class _SuccessResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"ok":true,"result":{"message_id":2}}'

    def urlopen(request, **_kwargs):
        attempts.append(json.loads(request.data.decode()))
        if len(attempts) == 1:
            raise _http_error(400, "Bad Request: can't parse entities")
        return _SuccessResponse()

    monkeypatch.setattr(main_transport.urlrequest, "urlopen", urlopen)

    response = main_transport._send_message("real-token", "-100123", "<broken", parse_mode="HTML")

    assert response["ok"] is True
    assert response["message_id"] == 2
    assert len(attempts) == 2
    assert attempts[0]["parse_mode"] == "HTML"
    assert "parse_mode" not in attempts[1]


def test_send_message_local_validation_is_definitively_pre_send(monkeypatch) -> None:
    monkeypatch.setattr(main_transport.urlrequest, "urlopen", lambda *_a, **_k: None)

    response = main_transport._send_message("", "-100123", "hello")

    assert response["ok"] is False
    assert response["external_effect_status"] == "failed"
    assert response["definitely_no_external_effect"] is True


def test_delivery_propagates_ambiguous_transport_status(monkeypatch) -> None:
    monkeypatch.setattr(
        main_delivery,
        "_send_message",
        lambda *_args, **_kwargs: {
            "ok": False,
            "error": "timed out",
            "error_code": "telegram_send_ambiguous",
            "external_effect_status": "timeout_unknown",
        },
    )

    response = main_delivery._deliver(
        {"event_type": "job.failed"},
        {"route_config": {"channel_id": "ops"}},
        {
            "provider_secrets": {"bot_token": "real-token"},
            "provider_config": {
                "channels": [
                    {
                        "channel_id": "ops",
                        "chat_id": "-100123",
                        "enabled": True,
                        "capabilities": {"notify": True},
                    }
                ],
            },
        },
    )

    assert response["ok"] is False
    assert response["external_effect_status"] == "timeout_unknown"
    assert response["error_code"] == "telegram_send_ambiguous"


@pytest.mark.parametrize("configured_mode", ["HTML", "MarkdownV2", ""])
def test_delivery_sends_html_rendering_for_the_only_supported_mode(
    monkeypatch, configured_mode
) -> None:
    sent_payloads = []

    class _SuccessResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"ok":true,"result":{"message_id":7}}'

    def urlopen(request, **_kwargs):
        sent_payloads.append(json.loads(request.data.decode()))
        return _SuccessResponse()

    monkeypatch.setattr(main_transport.urlrequest, "urlopen", urlopen)
    monkeypatch.setenv("FS_PUBLIC_BASE_URL", "https://fs.example")

    response = main_delivery._deliver(
        {
            "event_type": "job.failed",
            "severity": "error",
            "project_id": "project-1",
            "workflow_id": "workflow-1",
            "job_id": "job-1",
            "payload": {
                "workflow_label": "Workflow <>&[]()",
                "message": "Failure <>&[]()",
            },
        },
        {"route_config": {"channel_id": "ops"}},
        {
            "provider_secrets": {"bot_token": "real-token"},
            "provider_config": {
                "parse_mode": configured_mode,
                "channels": [
                    {
                        "channel_id": "ops",
                        "chat_id": "-100123",
                        "enabled": True,
                        "capabilities": {"notify": True},
                    }
                ],
            },
        },
    )

    assert response["ok"] is True
    assert len(sent_payloads) == 1
    payload = sent_payloads[0]
    assert payload["parse_mode"] == "HTML"
    assert '<a href="https://fs.example/registry/workflows/workflow-1/builder?' in payload["text"]
    assert "Workflow &lt;&gt;&amp;[]()" in payload["text"]
    assert "Failure &lt;&gt;&amp;[]()" in payload["text"]


def test_delivery_bounds_complete_escaped_html_payload(monkeypatch) -> None:
    sent_payloads = []

    class _SuccessResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"ok":true,"result":{"message_id":8}}'

    def urlopen(request, **_kwargs):
        sent_payloads.append(json.loads(request.data.decode()))
        return _SuccessResponse()

    monkeypatch.setattr(main_transport.urlrequest, "urlopen", urlopen)

    response = main_delivery._deliver(
        {
            "event_type": "job.failed",
            "payload": {"message": "dynamic <>&[]() " * 500},
        },
        {
            "route_config": {
                "channel_id": "ops",
                "message_suffix": "suffix <>&[]()",
            }
        },
        {
            "provider_secrets": {"bot_token": "real-token"},
            "provider_config": {
                "message_prefix": "prefix <>&[]()" + ("x" * 4080),
                "channels": [
                    {
                        "channel_id": "ops",
                        "chat_id": "-100123",
                        "enabled": True,
                        "capabilities": {"notify": True},
                    }
                ],
            },
        },
    )

    assert response["ok"] is True
    text = sent_payloads[0]["text"]
    assert len(text) <= 4096
    assert text.endswith("…")
    assert "<" not in text
    assert "prefix &lt;&gt;&amp;[]()" in text

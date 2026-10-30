"""Telegram workflow steps beyond Send Message, with the Telegram transport mocked.

No real bot token exists in these tests: every call that would reach
``api.telegram.org`` is replaced, or answered by a fake ``urlopen``.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from email.message import Message
from urllib import error as urlerror

import pytest
import yaml

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUNDLE_ROOT))

import main_send  # noqa: E402
import main_steps  # noqa: E402
import main_transport_steps as transport  # noqa: E402
from subprocess_contract_helpers import host_envelope  # noqa: E402

STEPS = (
    "send_file",
    "edit_message",
    "delete_message",
    "pin_message",
    "unpin_message",
    "get_file",
)


def _channel(channel_id: str = "ops", *, notify: bool = True) -> dict[str, Any]:
    return {
        "channel_id": channel_id,
        "display_name": channel_id.title(),
        "chat_id": "-1001",
        "enabled": True,
        "capabilities": {"notify": notify, "human_decision": False},
    }


def _action(
    action_id: str,
    input_payload: dict[str, Any],
    *,
    token: str = "fake-token",
    test_mode: bool = False,
    config: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action: dict[str, Any] = {
        "action_id": action_id,
        "context": {"project_id": "7", "test_mode": test_mode},
        "target": {
            "connection": {
                "connection_id": "conn-telegram",
                "config": {"channels": [_channel()], **(config or {})},
                "secrets": {"bot_token": token},
            }
        },
        "input": input_payload,
    }
    if artifacts is not None:
        action["artifacts"] = artifacts
    return action


def _invoke(action: dict[str, Any], *, top_level: dict[str, Any] | None = None):
    proc = subprocess.run(
        [sys.executable, str(BUNDLE_ROOT / "main.py")],
        input=json.dumps(host_envelope({"mode": "action", "action": action, **(top_level or {})})),
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(BUNDLE_ROOT),
    )
    return json.loads(proc.stdout.strip().splitlines()[-1]), proc.returncode


def _input_artifact(path: Path, *, size: int | None = None, name: str = "report.pdf"):
    return {
        "inputs": [
            {
                "artifact_id": "art-in",
                "artifact_handle": "artifact:art-in",
                "binding_key": "source_artifact",
                "filename": name,
                **({"size_bytes": size} if size is not None else {}),
                "access": {
                    "transport": "presigned_url",
                    "mode": "read",
                    "download_url": f"file://{path}",
                },
            }
        ]
    }


def _output_artifact(path: Path, *, max_size: int | None = None):
    return {
        "outputs": [
            {
                "artifact_id": "art-out",
                "artifact_handle": "artifact:art-out",
                "binding_key": "downloaded_file",
                "access": {
                    "transport": "presigned_url",
                    "mode": "write",
                    "upload_url": f"file://{path}",
                    **({"max_size_bytes": max_size} if max_size else {}),
                },
            }
        ]
    }


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """What the steps hand to the Telegram transport."""
    recorded: list[dict[str, Any]] = []

    def _fake(token: str, method: str, fields: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        recorded.append({"token": token, "method": method, "fields": fields, **kwargs})
        return {"ok": True, "message_id": 77, "result": {"message_id": 77}}

    monkeypatch.setattr(main_steps, "telegram_step_call", _fake)
    return recorded


# --- send_message: reply, silent, keyboard -----------------------------------


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []

    def _fake(token: str, chat_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
        recorded.append({"token": token, "chat_id": chat_id, "message": message, **kwargs})
        return {"ok": True, "message_id": 42}

    monkeypatch.setattr(main_send, "_send_message", _fake)
    return recorded


def test_send_message_without_the_new_inputs_sends_exactly_what_it_always_did(
    sent: list[dict[str, Any]],
) -> None:
    main_send.handle_send_message(_action("send_message", {"channel_id": "ops", "text": "Hi"}), {})

    assert sent == [
        {
            "token": "fake-token",
            "chat_id": "-1001",
            "message": "Hi",
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
    ]


def test_send_message_passes_reply_silent_and_keyboard(sent: list[dict[str, Any]]) -> None:
    buttons = [
        [{"text": "Open", "url": "https://example.test/1"}],
        [{"text": "Yes", "callback_data": "yes"}, {"text": "No", "callback_data": "no"}],
    ]
    result, code = main_send.handle_send_message(
        _action(
            "send_message",
            {
                "channel_id": "ops",
                "text": "Deploy?",
                "reply_to_message_id": "31",
                "disable_notification": True,
                "buttons": buttons,
            },
        ),
        {},
    )

    assert code == 0
    assert result["provider_message_id"] == "42"
    assert sent[0]["reply_to_message_id"] == 31
    assert sent[0]["disable_notification"] is True
    assert sent[0]["reply_markup"] == {"inline_keyboard": buttons}


def test_send_message_accepts_the_keyboard_as_json_text(sent: list[dict[str, Any]]) -> None:
    raw = json.dumps([[{"text": "Open", "url": "https://example.test"}]])
    main_send.handle_send_message(
        _action("send_message", {"channel_id": "ops", "text": "x", "buttons": raw}), {}
    )

    assert sent[0]["reply_markup"] == {
        "inline_keyboard": [[{"text": "Open", "url": "https://example.test"}]]
    }


@pytest.mark.parametrize(
    "buttons",
    [
        "not json",
        {"text": "x"},
        [[]],
        [["x"]],
        [[{"text": "", "url": "https://example.test"}]],
        [[{"text": "Both", "url": "https://example.test", "callback_data": "x"}]],
        [[{"text": "Neither"}]],
        [[{"text": "Long", "callback_data": "x" * 65}]],
        [[{"text": "Approval", "callback_data": "tgx:a:token"}]],
    ],
)
def test_a_malformed_keyboard_is_refused_before_anything_is_sent(
    buttons: Any, sent: list[dict[str, Any]]
) -> None:
    result, code = main_send.handle_send_message(
        _action("send_message", {"channel_id": "ops", "text": "x", "buttons": buttons}), {}
    )

    assert code == 2
    assert result["error_code"] == "keyboard_invalid"
    assert result["definitely_no_external_effect"] is True
    assert sent == []


def test_a_bad_reply_target_is_refused(sent: list[dict[str, Any]]) -> None:
    result, code = main_send.handle_send_message(
        _action("send_message", {"text": "x", "reply_to_message_id": "abc"}), {}
    )

    assert (code, result["error_code"]) == (2, "reply_to_message_id_invalid")
    assert sent == []


def test_the_transport_sends_a_silent_message_only_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main_transport

    bodies: list[dict[str, Any]] = []

    class _Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def _urlopen(request, timeout=None):
        bodies.append(json.loads(request.data))
        return _Response(json.dumps({"ok": True, "result": {"message_id": 5}}).encode())

    monkeypatch.setattr(main_transport.urlrequest, "urlopen", _urlopen)
    main_transport._send_message("real-token", "-1", "a")
    main_transport._send_message("real-token", "-1", "b", disable_notification=True)

    assert "disable_notification" not in bodies[0]
    assert bodies[1]["disable_notification"] is True


# --- send_file ----------------------------------------------------------------


def test_send_file_uploads_the_artifact_with_caption_and_options(
    tmp_path: Path, calls: list[dict[str, Any]]
) -> None:
    source = tmp_path / "in.bin"
    source.write_bytes(b"%PDF-1.4 hello")
    result, code = main_steps.handle_send_file(
        _action(
            "send_file",
            {
                "channel_id": "ops",
                "kind": "document",
                "artifact_handle": "artifact:art-in",
                "caption": "Q3 <report>",
                "reply_to_message_id": 9,
                "disable_notification": True,
            },
            artifacts=_input_artifact(source),
        ),
        {},
    )

    assert code == 0
    assert result["provider_message_id"] == "77"
    assert result["channel_id"] == "ops"
    assert result["delivered"] is True
    [call] = calls
    assert call["method"] == "sendDocument"
    assert call["upload"]["file_field"] == "document"
    assert call["upload"]["data"] == b"%PDF-1.4 hello"
    assert call["upload"]["filename"] == "report.pdf"
    assert call["fields"] == {
        "chat_id": "-1001",
        "reply_to_message_id": 9,
        "disable_notification": True,
        "caption": "Q3 &lt;report&gt;",
        "parse_mode": "HTML",
        "allow_sending_without_reply": True,
    }


@pytest.mark.parametrize(
    ("kind", "method", "field"),
    [
        ("photo", "sendPhoto", "photo"),
        ("document", "sendDocument", "document"),
        ("video", "sendVideo", "video"),
        ("audio", "sendAudio", "audio"),
        ("voice", "sendVoice", "voice"),
        ("animation", "sendAnimation", "animation"),
    ],
)
def test_send_file_picks_the_telegram_method_from_the_kind(
    kind: str, method: str, field: str, tmp_path: Path, calls: list[dict[str, Any]]
) -> None:
    source = tmp_path / "in.bin"
    source.write_bytes(b"data")
    main_steps.handle_send_file(
        _action(
            "send_file",
            {"kind": kind, "artifact_handle": "artifact:art-in"},
            artifacts=_input_artifact(source),
        ),
        {},
    )

    assert (calls[0]["method"], calls[0]["upload"]["file_field"]) == (method, field)
    assert "caption" not in calls[0]["fields"]


def test_a_photo_over_ten_megabytes_is_refused_not_cut(
    tmp_path: Path, calls: list[dict[str, Any]]
) -> None:
    source = tmp_path / "in.bin"
    source.write_bytes(b"x")
    result, code = main_steps.handle_send_file(
        _action(
            "send_file",
            {"kind": "photo", "artifact_handle": "artifact:art-in"},
            artifacts=_input_artifact(source, size=10 * 1024 * 1024 + 1),
        ),
        {},
    )

    assert (code, result["error_code"]) == (2, "file_too_large")
    assert calls == []


def test_another_kind_may_be_larger_than_a_photo_but_not_over_fifty_megabytes(
    tmp_path: Path, calls: list[dict[str, Any]]
) -> None:
    source = tmp_path / "in.bin"
    source.write_bytes(b"x")
    ok, _ = main_steps.handle_send_file(
        _action(
            "send_file",
            {"kind": "video", "artifact_handle": "artifact:art-in"},
            artifacts=_input_artifact(source, size=10 * 1024 * 1024 + 1),
        ),
        {},
    )
    refused, code = main_steps.handle_send_file(
        _action(
            "send_file",
            {"kind": "video", "artifact_handle": "artifact:art-in"},
            artifacts=_input_artifact(source, size=50 * 1024 * 1024 + 1),
        ),
        {},
    )

    assert ok["ok"] is True
    assert (code, refused["error_code"]) == (2, "file_too_large")
    assert len(calls) == 1


def test_a_file_that_grows_past_the_limit_while_streaming_is_refused(
    tmp_path: Path, calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_steps, "PHOTO_UPLOAD_LIMIT_BYTES", 8)
    source = tmp_path / "in.bin"
    source.write_bytes(b"123456789")  # no size_bytes in the descriptor
    result, code = main_steps.handle_send_file(
        _action(
            "send_file",
            {"kind": "photo", "artifact_handle": "artifact:art-in"},
            artifacts=_input_artifact(source),
        ),
        {},
    )

    assert (code, result["error_code"]) == (2, "file_too_large")
    assert calls == []


@pytest.mark.parametrize(
    ("input_payload", "error_code"),
    [
        ({"kind": "sticker", "artifact_handle": "a"}, "kind_invalid"),
        ({"artifact_handle": "a"}, "kind_invalid"),
        ({"kind": "photo"}, "artifact_required"),
        ({"kind": "photo", "artifact_handle": "a", "format": "md"}, "format_invalid"),
        ({"kind": "photo", "artifact_handle": "a", "caption": "x" * 1025}, "caption_too_long"),
        (
            {"kind": "photo", "artifact_handle": "a", "reply_to_message_id": 0},
            "reply_to_message_id_invalid",
        ),
    ],
)
def test_send_file_validates_its_inputs(
    input_payload: dict[str, Any], error_code: str, calls: list[dict[str, Any]]
) -> None:
    result, code = main_steps.handle_send_file(_action("send_file", input_payload), {})

    assert (code, result["error_code"]) == (2, error_code)
    assert calls == []


def test_send_file_reports_an_unreadable_artifact(calls: list[dict[str, Any]]) -> None:
    result, code = main_steps.handle_send_file(
        _action("send_file", {"kind": "photo", "artifact_handle": "artifact:nope"}), {}
    )

    assert (code, result["error_code"]) == (2, "artifact_read_failed")
    assert calls == []


def test_send_file_refuses_an_empty_file(tmp_path: Path, calls: list[dict[str, Any]]) -> None:
    source = tmp_path / "empty.bin"
    source.write_bytes(b"")
    result, code = main_steps.handle_send_file(
        _action(
            "send_file",
            {"kind": "document", "artifact_handle": "artifact:art-in"},
            artifacts=_input_artifact(source),
        ),
        {},
    )

    assert (code, result["error_code"]) == (2, "artifact_empty")
    assert calls == []


def test_send_file_needs_a_channel_that_may_notify(
    tmp_path: Path, calls: list[dict[str, Any]]
) -> None:
    source = tmp_path / "in.bin"
    source.write_bytes(b"x")
    action = _action(
        "send_file",
        {"channel_id": "quiet", "kind": "document", "artifact_handle": "artifact:art-in"},
        artifacts=_input_artifact(source),
    )
    action["target"]["connection"]["config"]["channels"] = [_channel("quiet", notify=False)]
    result, code = main_steps.handle_send_file(action, {})

    assert (code, result["error_code"]) == (2, "channel_missing_notify")
    assert calls == []


def test_send_file_runs_through_the_entrypoint_with_top_level_artifact_grants(
    tmp_path: Path,
) -> None:
    source = tmp_path / "in.bin"
    source.write_bytes(b"photo bytes")
    action = _action("send_file", {"kind": "photo", "artifact_handle": "artifact:art-in"})
    response, code = _invoke(action, top_level={"artifacts": _input_artifact(source)})

    assert code == 0, response
    assert response["ok"] is True
    assert response["provider_message_id"] == "1"
    assert response["channel_id"] == "ops"


# --- the multipart upload and the error mapping -------------------------------


class _Reply(io.BytesIO):
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        super().__init__(json.dumps(payload).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _http_error(status: int, description: str, **parameters: Any) -> urlerror.HTTPError:
    body = {"ok": False, "error_code": status, "description": description}
    if parameters:
        body["parameters"] = parameters
    return urlerror.HTTPError(
        "https://api.telegram.org/botX/m",
        status,
        "err",
        Message(),
        io.BytesIO(json.dumps(body).encode()),
    )


def test_the_upload_is_multipart_form_data(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _urlopen(request, timeout=None):
        seen["request"] = request
        seen["timeout"] = timeout
        return _Reply({"ok": True, "result": {"message_id": 12}})

    monkeypatch.setattr(transport.urlrequest, "urlopen", _urlopen)
    response = transport.telegram_step_call(
        "123:real-token",
        "sendDocument",
        {"chat_id": "-1001", "caption": "cap", "disable_notification": True},
        upload={
            "file_field": "document",
            "filename": 'we"ird\r\nname.pdf',
            "content_type": "application/octet-stream",
            "data": b"\x00\x01binary",
        },
        needs_message_id=True,
    )

    assert response == {"ok": True, "message_id": 12, "result": {"message_id": 12}}
    request = seen["request"]
    assert request.full_url == "https://api.telegram.org/bot123%3Areal-token/sendDocument"
    content_type = request.headers["Content-type"]
    boundary = content_type.split("boundary=")[1]
    assert content_type.startswith("multipart/form-data; boundary=")
    body = request.data
    assert b'name="chat_id"\r\n\r\n-1001\r\n' in body
    assert b'name="disable_notification"\r\n\r\ntrue\r\n' in body
    assert b'name="caption"\r\n\r\ncap\r\n' in body
    assert b'name="document"; filename="we_ird_name.pdf"' in body
    assert b"\r\n\r\n\x00\x01binary\r\n" in body
    assert body.endswith(f"--{boundary}--\r\n".encode())


def test_markup_rejected_by_telegram_is_retried_once_as_plain_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies: list[dict[str, Any]] = []

    def _urlopen(request, timeout=None):
        bodies.append(json.loads(request.data))
        if len(bodies) == 1:
            raise _http_error(400, "Bad Request: can't parse entities: Unsupported start tag")
        return _Reply({"ok": True, "result": {"message_id": 3}})

    monkeypatch.setattr(transport.urlrequest, "urlopen", _urlopen)
    response = transport.telegram_step_call(
        "123:real-token",
        "editMessageText",
        {"chat_id": "1", "message_id": 3, "text": "<x>", "parse_mode": "HTML"},
        retry_without_parse_mode=True,
    )

    assert response["ok"] is True
    assert "parse_mode" in bodies[0]
    assert "parse_mode" not in bodies[1]


@pytest.mark.parametrize(
    ("status", "description", "code", "definite"),
    [
        (
            400,
            "Bad Request: message can't be deleted for everyone",
            "telegram_delete_not_allowed",
            True,
        ),
        (400, "Bad Request: message to delete not found", "telegram_message_not_found", True),
        (
            400,
            "Bad Request: message is not modified: exactly the same",
            "telegram_message_not_modified",
            True,
        ),
        (400, "Bad Request: message can't be edited", "telegram_edit_not_allowed", True),
        (
            400,
            "Bad Request: there is no text in the message to edit",
            "telegram_edit_not_allowed",
            True,
        ),
        (
            400,
            "Bad Request: not enough rights to pin a message",
            "telegram_not_enough_rights",
            True,
        ),
        (400, "Bad Request: chat not found", "telegram_chat_not_found", True),
        (
            403,
            "Forbidden: bot was kicked from the supergroup chat",
            "telegram_bot_not_in_chat",
            True,
        ),
        (400, "Bad Request: something new", "telegram_request_rejected", True),
        (413, "Request Entity Too Large", "telegram_file_too_large", True),
        (500, "Internal error", "telegram_send_ambiguous", False),
    ],
)
def test_telegram_errors_map_to_clear_codes(
    status: int, description: str, code: str, definite: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _urlopen(request, timeout=None):
        raise _http_error(status, description)

    monkeypatch.setattr(transport.urlrequest, "urlopen", _urlopen)
    response = transport.telegram_step_call("123:real-token", "deleteMessage", {"chat_id": "1"})

    assert response["ok"] is False
    assert response["error_code"] == code
    assert response["definitely_no_external_effect"] is definite
    assert "real-token" not in json.dumps(response)


def test_a_rate_limit_carries_the_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    def _urlopen(request, timeout=None):
        raise _http_error(429, "Too Many Requests: retry after 7", retry_after=7)

    monkeypatch.setattr(transport.urlrequest, "urlopen", _urlopen)
    response = transport.telegram_step_call("123:real-token", "pinChatMessage", {"chat_id": "1"})

    assert response["error_code"] == "telegram_rate_limited"
    assert response["retryable"] is True
    assert response["retry_after_seconds"] == 7


def test_a_timeout_leaves_the_outcome_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def _urlopen(request, timeout=None):
        raise TimeoutError("timed out talking to /bot123:real-token/sendPhoto")

    monkeypatch.setattr(transport.urlrequest, "urlopen", _urlopen)
    response = transport.telegram_step_call("123:real-token", "sendPhoto", {"chat_id": "1"})

    assert response["error_code"] == "telegram_send_ambiguous"
    assert response["external_effect_status"] == "timeout_unknown"
    assert "real-token" not in json.dumps(response)


# --- edit / delete / pin / unpin ----------------------------------------------


def test_edit_message_replaces_the_text_with_the_connection_defaults(
    calls: list[dict[str, Any]],
) -> None:
    buttons = [[{"text": "Open", "url": "https://example.test"}]]
    result, code = main_steps.handle_edit_message(
        _action(
            "edit_message",
            {
                "channel_id": "ops",
                "message_id": "40",
                "text": "Done <ok>",
                "buttons": buttons,
            },
            config={"message_prefix": "[Prod]"},
        ),
        {},
    )

    assert code == 0
    assert result["provider_message_id"] == "77"
    [call] = calls
    assert call["method"] == "editMessageText"
    assert call["retry_without_parse_mode"] is True
    assert call["fields"] == {
        "chat_id": "-1001",
        "message_id": 40,
        "text": "[Prod]\nDone &lt;ok&gt;",
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": buttons},
    }


def test_edit_message_html_format_keeps_tags(calls: list[dict[str, Any]]) -> None:
    main_steps.handle_edit_message(
        _action("edit_message", {"message_id": 1, "text": "<b>Done</b>", "format": "html"}),
        {},
    )

    assert calls[0]["fields"]["text"] == "<b>Done</b>"
    assert "reply_markup" not in calls[0]["fields"]


@pytest.mark.parametrize(
    ("input_payload", "error_code"),
    [
        ({"text": "x"}, "message_id_required"),
        ({"message_id": "abc", "text": "x"}, "message_id_invalid"),
        ({"message_id": 1}, "text_required"),
        ({"message_id": 1, "text": "  "}, "text_required"),
        ({"message_id": 1, "text": "x", "format": "md"}, "format_invalid"),
        ({"message_id": 1, "text": "x" * 4097}, "message_too_long"),
        ({"message_id": 1, "text": "x", "buttons": [[{"text": "n"}]]}, "keyboard_invalid"),
    ],
)
def test_edit_message_validates_its_inputs(
    input_payload: dict[str, Any], error_code: str, calls: list[dict[str, Any]]
) -> None:
    result, code = main_steps.handle_edit_message(_action("edit_message", input_payload), {})

    assert (code, result["error_code"]) == (2, error_code)
    assert calls == []


def test_delete_message_calls_delete_message(calls: list[dict[str, Any]]) -> None:
    result, code = main_steps.handle_delete_message(
        _action("delete_message", {"channel_id": "ops", "message_id": 40}), {}
    )

    assert code == 0
    assert (result["provider_message_id"], result["channel_id"]) == ("40", "ops")
    assert calls[0]["method"] == "deleteMessage"
    assert calls[0]["fields"] == {"chat_id": "-1001", "message_id": 40}


def test_a_message_too_old_to_delete_reports_its_own_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _urlopen(request, timeout=None):
        raise _http_error(400, "Bad Request: message can't be deleted for everyone")

    monkeypatch.setattr(transport.urlrequest, "urlopen", _urlopen)
    result, code = main_steps.handle_delete_message(
        _action("delete_message", {"message_id": 5}, token="123:real-token"), {}
    )

    assert code == 2
    assert result["error_code"] == "telegram_delete_not_allowed"
    assert result["delivered"] is False
    assert result["definitely_no_external_effect"] is True


def test_pin_message_can_pin_silently(calls: list[dict[str, Any]]) -> None:
    main_steps.handle_pin_message(
        _action("pin_message", {"message_id": 8, "disable_notification": True}), {}
    )
    main_steps.handle_pin_message(_action("pin_message", {"message_id": 9}), {})

    assert calls[0]["method"] == "pinChatMessage"
    assert calls[0]["fields"] == {
        "chat_id": "-1001",
        "message_id": 8,
        "disable_notification": True,
    }
    assert calls[1]["fields"] == {"chat_id": "-1001", "message_id": 9}


def test_unpin_message_without_an_id_unpins_the_latest(calls: list[dict[str, Any]]) -> None:
    result, code = main_steps.handle_unpin_message(_action("unpin_message", {}), {})
    main_steps.handle_unpin_message(_action("unpin_message", {"message_id": "12"}), {})

    assert code == 0
    assert result["provider_message_id"] == ""
    assert calls[0]["method"] == "unpinChatMessage"
    assert calls[0]["fields"] == {"chat_id": "-1001"}
    assert calls[1]["fields"] == {"chat_id": "-1001", "message_id": 12}


@pytest.mark.parametrize("step", ["delete_message", "pin_message"])
def test_a_message_step_needs_a_message_id(step: str, calls: list[dict[str, Any]]) -> None:
    handler = getattr(main_steps, f"handle_{step}")
    result, code = handler(_action(step, {"channel_id": "ops"}), {})

    assert (code, result["error_code"]) == (2, "message_id_required")
    assert calls == []


def test_message_steps_resolve_the_channel_like_send_message(calls: list[dict[str, Any]]) -> None:
    unknown, _ = main_steps.handle_delete_message(
        _action("delete_message", {"channel_id": "nope", "message_id": 1}), {}
    )
    no_connection = _action("delete_message", {"message_id": 1})
    no_connection["target"] = {}
    missing, _ = main_steps.handle_delete_message(no_connection, {})

    assert unknown["error_code"] == "unknown_channel_id"
    assert missing["error_code"] == "project_connection_required"
    assert calls == []


@pytest.mark.parametrize(
    ("step", "input_payload"),
    [
        ("send_file", {"kind": "photo", "artifact_handle": "artifact:x"}),
        ("edit_message", {"message_id": 1, "text": "x"}),
        ("delete_message", {"message_id": 1}),
        ("pin_message", {"message_id": 1}),
        ("unpin_message", {}),
    ],
)
def test_test_run_suppresses_every_effect(
    step: str, input_payload: dict[str, Any], calls: list[dict[str, Any]]
) -> None:
    handler = getattr(main_steps, f"handle_{step}")
    for action, runtime_context in (
        (_action(step, {**input_payload, "channel_id": "ops"}, test_mode=True), {}),
        (_action(step, {**input_payload, "channel_id": "ops"}), {"test_mode": True}),
    ):
        result, code = handler(action, runtime_context)

        assert code == 0
        assert result["external_effect_status"] == "suppressed"
        assert result["delivered"] is False
        assert result["channel_id"] == "ops"
    assert calls == []


@pytest.mark.parametrize(
    ("step", "input_payload", "message_id"),
    [
        ("edit_message", {"message_id": 21, "text": "x"}, "21"),
        ("delete_message", {"message_id": 22}, "22"),
        ("pin_message", {"message_id": 23}, "23"),
        ("unpin_message", {"message_id": 24}, "24"),
    ],
)
def test_message_steps_run_through_the_entrypoint(
    step: str, input_payload: dict[str, Any], message_id: str
) -> None:
    response, code = _invoke(_action(step, {"channel_id": "ops", **input_payload}))

    assert code == 0, response
    assert response["ok"] is True
    assert (response["provider_message_id"], response["channel_id"]) == (message_id, "ops")


# --- get_file ------------------------------------------------------------------


class _Download(io.BytesIO):
    pass


def _patch_download(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    *,
    file_size: int | None,
    path: str = "photos/f1.jpg",
) -> list[str]:
    asked: list[str] = []

    def _info(token: str, file_id: str) -> dict[str, Any]:
        asked.append(file_id)
        return {"ok": True, "file_path": path, "file_size": file_size}

    monkeypatch.setattr(main_steps, "telegram_get_file_info", _info)
    monkeypatch.setattr(main_steps, "telegram_open_file", lambda *_a: _Download(body))
    return asked


def test_get_file_downloads_into_an_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _patch_download(monkeypatch, b"jpeg bytes", file_size=10)
    target = tmp_path / "out.bin"
    result, code = main_steps.handle_get_file(
        _action(
            "get_file",
            {"file_id": "AgAD-1"},
            token="123:real-token",
            artifacts=_output_artifact(target),
        ),
        {},
    )

    assert code == 0, result
    assert asked == ["AgAD-1"]
    assert target.read_bytes() == b"jpeg bytes"
    assert result["artifact_handle"] == "artifact:art-out"
    assert result["artifact_filename"] == "f1.jpg"
    assert result["size_bytes"] == len(b"jpeg bytes")
    assert len(result["sha256"]) == 64


def test_get_file_refuses_a_file_telegram_says_is_over_twenty_megabytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_download(monkeypatch, b"x", file_size=20 * 1024 * 1024 + 1)
    target = tmp_path / "out.bin"
    result, code = main_steps.handle_get_file(
        _action(
            "get_file",
            {"file_id": "big"},
            token="123:real-token",
            artifacts=_output_artifact(target),
        ),
        {},
    )

    assert (code, result["error_code"]) == (2, "file_too_large")
    assert not target.exists()


def test_get_file_stops_a_download_that_outgrows_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_steps, "FILE_DOWNLOAD_LIMIT_BYTES", 8)
    _patch_download(monkeypatch, b"123456789", file_size=None)
    target = tmp_path / "out.bin"
    result, code = main_steps.handle_get_file(
        _action(
            "get_file",
            {"file_id": "liar"},
            token="123:real-token",
            artifacts=_output_artifact(target),
        ),
        {},
    )

    assert (code, result["error_code"]) == (2, "file_too_large")
    assert not target.exists()


def test_get_file_reports_a_telegram_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        main_steps,
        "telegram_get_file_info",
        lambda *_a: {
            "ok": False,
            "error": "Bad Request: wrong file_id or the file is temporarily unavailable",
            "error_code": "telegram_request_rejected",
            "definitely_no_external_effect": True,
        },
    )
    result, code = main_steps.handle_get_file(
        _action("get_file", {"file_id": "x"}, token="123:real-token"), {}
    )

    assert (code, result["error_code"]) == (2, "telegram_request_rejected")


@pytest.mark.parametrize(
    ("input_payload", "error_code"),
    [({}, "file_id_required")],
)
def test_get_file_needs_a_file_id(input_payload: dict[str, Any], error_code: str) -> None:
    result, code = main_steps.handle_get_file(_action("get_file", input_payload), {})

    assert (code, result["error_code"]) == (2, error_code)


def test_get_file_runs_through_the_entrypoint(tmp_path: Path) -> None:
    target = tmp_path / "out.bin"
    action = _action("get_file", {"file_id": "AgAD-1"})
    response, code = _invoke(action, top_level={"artifacts": _output_artifact(target)})

    assert code == 0, response
    assert response["artifact_handle"] == "artifact:art-out"


def test_the_file_path_telegram_returns_is_checked_before_it_is_fetched() -> None:
    for bad in ("", "/etc/passwd", "../x", "a/../b", "a//b", "a\\b"):
        with pytest.raises(ValueError):
            transport.telegram_open_file("123:real-token", bad)


def test_get_file_info_reads_the_path_and_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        transport,
        "_telegram_api_call",
        lambda token, method, payload=None: {
            "ok": True,
            "result": {"file_id": "x", "file_path": "documents/a.pdf", "file_size": 12},
        },
    )

    assert transport.telegram_get_file_info("123:real-token", "x") == {
        "ok": True,
        "file_path": "documents/a.pdf",
        "file_size": 12,
    }


# --- declarations ----------------------------------------------------------------


def _load(relative: str) -> Any:
    return yaml.safe_load((BUNDLE_ROOT / relative).read_text(encoding="utf-8"))


def test_every_new_step_is_declared_consistently() -> None:
    operations = {
        row["operation_id"]: row for row in _load("contracts/operation_manifest.yaml")["operations"]
    }
    forms = {row["operation_id"]: row for row in _load("contracts/step_ui_manifest.yaml")["forms"]}
    actions = {row["action_id"]: row for row in _load("ui/actions/actions.yaml")["actions"]}
    manifest = _load("extension.yaml")
    effects = {row["operation_id"]: row for row in manifest["external_effects"]}
    english = _load("locales/en.yaml")
    lithuanian = _load("locales/lt.yaml")

    for step in ("send_message", *STEPS):
        operation = operations[step]
        assert operation["workflow_visible"] is True
        assert operation["connection_type_ids"] == ["telegram_bot"]
        input_names = {item["name"] for item in operation["inputs"]} - {"connection_ref"}
        assert {field["name"] for field in forms[step]["fields"]} == input_names
        assert {item["name"] for item in actions[step]["parameters"]} == input_names
        assert set(actions[step]["error_codes"]) <= set(operation["error_codes"])
        for bundle in (english, lithuanian):
            assert bundle[f"op.{step}.display_name"]
            assert bundle[f"op.{step}.description"]
            assert bundle[f"step.{step}.section"]
            for name in input_names:
                assert bundle[f"op.{step}.input.{name}.display_name"], (step, name)
                assert bundle[f"step.{step}.field.{name}.label"], (step, name)
            for output in operation["outputs"]:
                if output.get("display_name"):
                    assert bundle[f"op.{step}.output.{output['name']}.display_name"]
        if step != "get_file":
            effect = effects[step]
            assert effect["idempotency"] == "required"
            assert effect["test_mode_behavior"] == "suppress"
            outputs = {item["name"] for item in operation["outputs"]}
            assert {"provider_message_id", "channel_id"} <= outputs
    assert "get_file" not in effects
    # Only a placeholder (an example value, not text) may be left untranslated.
    assert set(lithuanian) <= set(english)
    assert all(key.endswith(".placeholder") for key in set(english) - set(lithuanian))


def test_the_file_steps_declare_artifact_policies_and_scopes() -> None:
    manifest = _load("extension.yaml")
    file_policies = _load("contracts/artifact_policies.yaml")["policies"]

    assert manifest["artifact_policies"] == file_policies
    assert manifest["runtime"]["extension_contract_v2"]["artifact_policies"] == (
        "contracts/artifact_policies.yaml"
    )
    by_operation = {row["operation_id"]: row for row in file_policies}
    upload = by_operation["send_file"]["inputs"][0]
    assert (upload["binding_key"], upload["field"], upload["value_type"]) == (
        "source_artifact",
        "artifact_handle",
        "artifact_handle",
    )
    assert upload["max_size_bytes"] == 50 * 1024 * 1024
    download = by_operation["get_file"]["outputs"][0]
    assert download["binding_key"] == "downloaded_file"
    assert download["max_size_bytes"] == 20 * 1024 * 1024
    assert {"artifact:read", "artifact:write"} <= set(manifest["required_scopes"])
    actions = {row["action_id"]: row for row in _load("ui/actions/actions.yaml")["actions"]}
    assert "artifact:read" in actions["send_file"]["required_scopes"]
    assert "artifact:write" in actions["get_file"]["required_scopes"]


def test_every_error_code_the_steps_return_is_declared() -> None:
    declared = {
        code
        for row in _load("contracts/operation_manifest.yaml")["operations"]
        for code in row.get("error_codes", [])
    }
    emitted: set[str] = set()
    for module in ("main_send.py", "main_steps.py", "main_transport_steps.py"):
        source = (BUNDLE_ROOT / module).read_text(encoding="utf-8")
        emitted |= set(re.findall(r'_failure\(\s*"([a-z_]+)"', source))
        emitted |= set(re.findall(r'"(telegram_[a-z_]+)"', source))
    # Not codes: a message passed to an ambiguous failure, and a fallback that
    # predates these steps.
    emitted -= {"telegram_step_call", "telegram_success_message_id_missing", "telegram_send_failed"}

    assert emitted - declared == set()

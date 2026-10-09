"""Send Message: a workflow step that posts a text to one of the bot's channels."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUNDLE_ROOT))

import main_send  # noqa: E402


def _channel(channel_id: str, *, enabled: bool = True, notify: bool = True) -> dict[str, Any]:
    return {
        "channel_id": channel_id,
        "display_name": channel_id.title(),
        "chat_id": f"-100{len(channel_id)}",
        "enabled": enabled,
        "capabilities": {"notify": notify, "human_decision": False},
    }


def _action(
    input_payload: dict[str, Any],
    *,
    channels: list[dict[str, Any]] | None = None,
    config: dict[str, Any] | None = None,
    token: str = "fake-token",
    test_mode: bool = False,
) -> dict[str, Any]:
    connection_config = {"channels": channels if channels is not None else [_channel("ops")]}
    connection_config.update(config or {})
    return {
        "action_id": "send_message",
        "context": {"project_id": "7", "test_mode": test_mode},
        "target": {
            "connection": {
                "connection_id": "conn-telegram",
                "config": connection_config,
                "secrets": {"bot_token": token},
            }
        },
        "input": input_payload,
    }


def _invoke(action: dict[str, Any]) -> tuple[dict[str, Any], int]:
    proc = subprocess.run(
        [sys.executable, str(BUNDLE_ROOT / "main.py")],
        input=json.dumps({"mode": "action", "action": action}),
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(BUNDLE_ROOT),
    )
    return json.loads(proc.stdout.strip()), proc.returncode


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """What the step hands to the Telegram transport, without a network call."""
    calls: list[dict[str, Any]] = []

    def _fake_send(token: str, chat_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"token": token, "chat_id": chat_id, "message": message, **kwargs})
        return {"ok": True, "message_id": 42}

    monkeypatch.setattr(main_send, "_send_message", _fake_send)
    return calls


def test_the_step_runs_through_the_extension_entrypoint() -> None:
    response, code = _invoke(_action({"channel_id": "ops", "text": "Build finished"}))

    assert code == 0
    assert response["ok"] is True
    assert response["delivered"] is True
    assert response["channel_id"] == "ops"
    assert response["provider_message_id"] == "1"


def test_a_plain_text_is_sent_as_written(sent: list[dict[str, Any]]) -> None:
    text = "Price < 5 € & <b>not bold</b>\n\nSecond paragraph"
    result, code = main_send.handle_send_message(_action({"channel_id": "ops", "text": text}), {})

    assert code == 0
    assert result["provider_message_id"] == "42"
    assert sent[0]["chat_id"] == "-1003"
    assert sent[0]["parse_mode"] == "HTML"
    # Escaped for Telegram's HTML mode, so the reader sees the characters typed.
    assert sent[0]["message"] == (
        "Price &lt; 5 € &amp; &lt;b&gt;not bold&lt;/b&gt;\n\nSecond paragraph"
    )


def test_an_html_text_keeps_its_tags(sent: list[dict[str, Any]]) -> None:
    text = '<b>Done</b> — <a href="https://example.test/job/1">open the job</a>'
    main_send.handle_send_message(
        _action({"channel_id": "ops", "text": text, "format": "html"}), {}
    )

    assert sent[0]["message"] == text


def test_the_connection_prefix_and_preview_setting_apply(sent: list[dict[str, Any]]) -> None:
    action = _action(
        {"text": "Deployed"},
        config={"message_prefix": "[Prod] <eu>", "disable_web_page_preview": False},
    )
    main_send.handle_send_message(action, {})

    assert sent[0]["message"] == "[Prod] &lt;eu&gt;\nDeployed"
    assert sent[0]["disable_web_page_preview"] is False

    action["input"]["disable_web_page_preview"] = True
    main_send.handle_send_message(action, {})
    assert sent[1]["disable_web_page_preview"] is True


def test_the_only_notify_channel_is_used_when_none_is_named(sent: list[dict[str, Any]]) -> None:
    channels = [_channel("ops"), _channel("approvals", notify=False), _channel("old", enabled=False)]
    result, code = main_send.handle_send_message(_action({"text": "Hi"}, channels=channels), {})

    assert code == 0
    assert result["channel_id"] == "ops"


@pytest.mark.parametrize(
    ("channels", "channel_id", "code", "mentions"),
    [
        ([_channel("ops"), _channel("sales")], "", "channel_id_required", "ops, sales"),
        ([_channel("ops", notify=False)], "", "no_notify_channel", "Chats & Channels"),
        ([_channel("ops")], "sales", "unknown_channel_id", "ops"),
        ([_channel("ops", enabled=False)], "ops", "channel_disabled", "ops"),
        ([_channel("ops", notify=False)], "ops", "channel_missing_notify", "ops"),
    ],
)
def test_a_channel_that_cannot_be_used_is_refused_before_anything_is_sent(
    sent: list[dict[str, Any]],
    channels: list[dict[str, Any]],
    channel_id: str,
    code: str,
    mentions: str,
) -> None:
    result, exit_code = main_send.handle_send_message(
        _action({"text": "Hi", "channel_id": channel_id}, channels=channels), {}
    )

    assert exit_code == 2
    assert result["error_code"] == code
    assert mentions in result["error"]
    assert result["definitely_no_external_effect"] is True
    assert sent == []


@pytest.mark.parametrize(
    ("input_payload", "code"),
    [
        ({"text": "   "}, "text_required"),
        ({}, "text_required"),
        ({"text": "Hi", "format": "markdown"}, "format_invalid"),
        ({"text": "x" * 4097}, "message_too_long"),
    ],
)
def test_a_message_that_cannot_be_sent_is_refused(
    sent: list[dict[str, Any]], input_payload: dict[str, Any], code: str
) -> None:
    result, exit_code = main_send.handle_send_message(_action(input_payload), {})

    assert exit_code == 2
    assert result["error_code"] == code
    assert sent == []


def test_a_message_of_exactly_the_limit_is_sent(sent: list[dict[str, Any]]) -> None:
    _result, code = main_send.handle_send_message(_action({"text": "x" * 4096}), {})

    assert code == 0
    assert len(sent[0]["message"]) == 4096


def test_test_run_never_sends(sent: list[dict[str, Any]]) -> None:
    result, code = main_send.handle_send_message(
        _action({"channel_id": "ops", "text": "Hi"}, test_mode=True), {}
    )

    assert code == 0
    assert result["delivered"] is False
    assert result["external_effect_status"] == "suppressed"
    assert sent == []

    result, _code = main_send.handle_send_message(
        _action({"channel_id": "ops", "text": "Hi"}), {"test_mode": True}
    )
    assert result["external_effect_status"] == "suppressed"
    assert sent == []


def test_a_rejected_send_reports_the_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        main_send,
        "_send_message",
        lambda *_args, **_kwargs: {
            "ok": False,
            "error": "Forbidden: bot was blocked by the user",
            "error_code": "telegram_send_rejected",
            "external_effect_status": "failed",
            "definitely_no_external_effect": True,
        },
    )
    result, code = main_send.handle_send_message(_action({"channel_id": "ops", "text": "Hi"}), {})

    assert code == 2
    assert result["delivered"] is False
    assert result["error_code"] == "telegram_send_rejected"
    assert result["definitely_no_external_effect"] is True


def test_the_operation_is_a_workflow_step_with_a_form_and_translations() -> None:
    operations = yaml.safe_load((BUNDLE_ROOT / "contracts/operation_manifest.yaml").read_text())
    send = next(op for op in operations["operations"] if op["operation_id"] == "send_message")
    assert send["workflow_visible"] is True
    assert send["connection_type_ids"] == ["telegram_bot"]
    inputs = {item["name"]: item for item in send["inputs"]}
    assert inputs["text"]["required"] is True
    assert inputs["channel_id"]["required"] is False

    forms = yaml.safe_load((BUNDLE_ROOT / "contracts/step_ui_manifest.yaml").read_text())
    form = next(item for item in forms["forms"] if item["operation_id"] == "send_message")
    fields = {item["name"]: item for item in form["fields"]}
    assert [option["value"] for option in fields["format"]["options"]] == list(main_send.FORMATS)

    manifest = yaml.safe_load((BUNDLE_ROOT / "extension.yaml").read_text())
    effect = next(e for e in manifest["external_effects"] if e["operation_id"] == "send_message")
    assert effect["test_mode_behavior"] == "suppress"

    for language in ("en", "lt"):
        bundle = yaml.safe_load((BUNDLE_ROOT / "locales" / f"{language}.yaml").read_text())
        assert bundle["op.send_message.display_name"]
        assert bundle["step.send_message.field.text.label"]

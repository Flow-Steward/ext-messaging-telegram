"""The "New Telegram message" workflow trigger: a plain message becomes an event.

Closed by default: only a chat that is an enabled channel with
``capabilities.workflow_trigger`` starts workflows; every other chat is ignored
silently and still answered successfully so Telegram does not retry.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUNDLE_ROOT))

import main as telegram_main  # noqa: E402
from main_ingress import _ingress_event_response  # noqa: E402
from main_state import _SUPPORTED_UPDATE_TYPES, _channel_items_from_config  # noqa: E402

CHAT_ID = -100777


def _channel(
    *, chat_id: int = CHAT_ID, enabled: bool = True, trigger: bool | None = True, **extra: Any
) -> dict[str, Any]:
    capabilities: dict[str, Any] = {"notify": True, "human_decision": False}
    if trigger is not None:
        capabilities["workflow_trigger"] = trigger
    return {
        "channel_id": "inbox",
        "chat_id": str(chat_id),
        "enabled": enabled,
        "capabilities": capabilities,
        **extra,
    }


def _request(
    update: dict[str, Any], channels: list[dict[str, Any]], **provider_extra: Any
) -> dict[str, Any]:
    return {
        "callback": {"authenticated": True, "project_id": "p-1"},
        "provider": {"provider_config": {"channels": channels}, **provider_extra},
        "query": {"project_id": "p-1"},
        "payload": update,
    }


def _text_update(**message: Any) -> dict[str, Any]:
    return {
        "update_id": 9001,
        "message": {
            "message_id": 55,
            "date": 1_790_000_000,
            "chat": {"id": CHAT_ID, "type": "supergroup", "title": "Inbox"},
            "from": {"id": 4242, "username": "alice", "first_name": "Alice"},
            "text": "hello bot",
            **message,
        },
    }


def test_a_plain_message_from_an_allowed_chat_returns_the_event_with_every_field() -> None:
    response = _ingress_event_response(
        _request(_text_update(), [_channel()], connection_id="conn-7")
    )

    assert response["ok"] is True
    [event] = response["events"]
    assert event["event_type"] == "telegram.message.received"
    assert event["correlation_id"] == "9001"
    assert event["project_id"] == "p-1"
    assert event["summary"] == "Telegram message from @alice in chat -100777"
    assert event["payload"] == {
        "chat_id": "-100777",
        "user_id": "4242",
        "message_id": 55,
        "text": "hello bot",
        "username": "alice",
        "chat_type": "supergroup",
        "date": 1_790_000_000,
        "has_file": False,
        "file_id": "",
        "file_kind": "",
        "file_name": "",
        "caption": "",
        "reply_to_message_id": 0,
        "update_id": 9001,
        "connection_id": "conn-7",
    }


def test_the_event_leaves_connection_id_out_when_there_is_none() -> None:
    response = _ingress_event_response(_request(_text_update(), [_channel()]))

    assert "connection_id" not in response["events"][0]["payload"]


@pytest.mark.parametrize(
    ("message", "kind", "file_id", "file_name"),
    [
        (
            {"photo": [{"file_id": "small"}, {"file_id": "large"}], "caption": "look"},
            "photo",
            "large",
            "",
        ),
        ({"document": {"file_id": "doc-1", "file_name": "a.pdf"}}, "document", "doc-1", "a.pdf"),
        ({"video": {"file_id": "vid-1", "file_name": "v.mp4"}}, "video", "vid-1", "v.mp4"),
        ({"audio": {"file_id": "aud-1", "file_name": "s.mp3"}}, "audio", "aud-1", "s.mp3"),
        ({"voice": {"file_id": "voi-1"}}, "voice", "voi-1", ""),
        (
            {
                "animation": {"file_id": "gif-1", "file_name": "g.mp4"},
                "document": {"file_id": "gif-doc", "file_name": "g.mp4"},
            },
            "animation",
            "gif-1",
            "g.mp4",
        ),
        ({"sticker": {"file_id": "stk-1"}}, "sticker", "stk-1", ""),
    ],
)
def test_a_file_message_carries_only_metadata_and_the_file_id(
    message: dict[str, Any], kind: str, file_id: str, file_name: str
) -> None:
    update = _text_update(**message)
    update["message"].pop("text")
    response = _ingress_event_response(_request(update, [_channel()]))

    payload = response["events"][0]["payload"]
    assert payload["has_file"] is True
    assert (payload["file_kind"], payload["file_id"], payload["file_name"]) == (
        kind,
        file_id,
        file_name,
    )
    assert payload["text"] == ""
    assert payload["caption"] == message.get("caption", "")


def test_a_reply_names_the_message_it_answers() -> None:
    update = _text_update(reply_to_message={"message_id": 12})

    response = _ingress_event_response(_request(update, [_channel()]))

    assert response["events"][0]["payload"]["reply_to_message_id"] == 12


@pytest.mark.parametrize(
    "channels",
    [
        pytest.param([], id="chat_is_not_a_channel"),
        pytest.param([_channel(chat_id=-100999)], id="other_chat"),
        pytest.param([_channel(enabled=False)], id="channel_disabled"),
        pytest.param([_channel(trigger=False)], id="capability_off"),
        pytest.param([_channel(trigger=None)], id="capability_absent_is_closed"),
    ],
)
def test_every_other_chat_is_ignored_silently(channels: list[dict[str, Any]]) -> None:
    response = _ingress_event_response(_request(_text_update(), channels))

    assert response["ok"] is True
    assert response["events"] == []
    assert response["result"]["ignored"] is True


def test_the_capability_defaults_to_off_for_a_stored_channel() -> None:
    [channel] = _channel_items_from_config({"channels": [_channel(trigger=None)]})
    assert channel["capabilities"] == {
        "notify": True,
        "human_decision": False,
        "workflow_trigger": False,
    }
    [enabled] = _channel_items_from_config({"channels": [_channel(trigger=True)]})
    assert enabled["capabilities"]["workflow_trigger"] is True


def test_an_approval_command_is_not_a_trigger_message() -> None:
    update = _text_update(text="/approve nope")

    response = _ingress_event_response(_request(update, [_channel()]))

    assert not any(e["event_type"] == "telegram.message.received" for e in response["events"])


def test_a_reply_to_the_reject_prompt_keeps_its_own_flow() -> None:
    update = _text_update(
        text="because",
        reply_to_message={"message_id": 3, "text": "Flow Steward reject context: x"},
    )

    response = _ingress_event_response(_request(update, [_channel()]))

    assert response["ok"] is False
    assert response["error"] == "invalid_reject_comment_context"
    assert response["events"] == []


def test_the_webhook_action_returns_the_event_with_the_connection_id() -> None:
    response, code = telegram_main._handle_action(
        {
            "action_id": "ingress_event",
            "input": {
                "callback": {"authenticated": True, "project_id": "p-1"},
                "request": _request(_text_update(), [_channel()]),
            },
            "runtime_context": {
                "resources": {
                    "project_connection": {
                        "connection_id": "conn-9",
                        "project_id": "p-1",
                        "connection_config": {"channels": [_channel()]},
                    },
                    "provider_secrets": {"bot_token": "fake-token"},
                }
            },
        }
    )

    assert code == 0
    assert response["ok"] is True
    assert response["events"][0]["payload"]["connection_id"] == "conn-9"
    assert response["events"][0]["event_type"] == "telegram.message.received"


def test_polling_returns_the_same_event(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Logger:
        def __init__(self) -> None:
            self.saved: dict[str, Any] = {}

        def get_project_connection(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "connection_id": "conn-3",
                "connection_config": {"channels": [_channel()]},
            }

        def upsert_project_connection(self, **kwargs: Any) -> None:
            self.saved = dict(kwargs)

    updates = [
        _text_update(),
        {
            "update_id": 9002,
            "message": {
                "message_id": 56,
                "chat": {"id": -100888, "type": "group"},
                "from": {"id": 1},
                "text": "from a chat that is not allowed",
            },
        },
    ]
    monkeypatch.setattr(
        telegram_main,
        "_telegram_get_updates",
        lambda *_a, **_k: {"ok": True, "result": updates},
    )
    from main_polling import _poll_updates

    response, code = _poll_updates(
        bl=_Logger(),
        project_id="p-1",
        inbound_state={
            "inbound_mode": "polling",
            "webhook_status": {"status": "not_configured", "url_configured": False},
        },
        token="fake-token",
    )

    assert code == 0
    assert response["result"]["processed_updates"] == 2
    [event] = response["events"]
    assert event["event_type"] == "telegram.message.received"
    assert event["correlation_id"] == "9001"
    assert event["payload"]["connection_id"] == "conn-3"
    assert event["payload"]["text"] == "hello bot"


def test_messages_are_still_requested_from_telegram() -> None:
    assert "message" in _SUPPORTED_UPDATE_TYPES


def test_the_trigger_is_declared_in_the_manifest() -> None:
    manifest = yaml.safe_load((BUNDLE_ROOT / "extension.yaml").read_text())

    [trigger] = manifest["workflow_triggers"]
    assert trigger["trigger_type_id"] == "message_received"
    assert trigger["provider_key"] == "flowsteward.telegram"
    assert trigger["display_name"] == "New Telegram message"
    assert trigger["event_name"] == "telegram.message.received"
    schema = trigger["config_schema"]
    assert set(schema["properties"]) == {"chat_id", "user_id"}
    assert all(item["type"] == "string" for item in schema["properties"].values())
    assert "required" not in schema
    assert [field["name"] for field in trigger["ui_schema"]["fields"]] == ["chat_id", "user_id"]
    assert all(field["help_text"] and field["label"] for field in trigger["ui_schema"]["fields"])
    subscribed = {item["event_type"]: item for item in manifest["event_subscriptions"]}
    assert subscribed["telegram.message.received"]["severity"] == "any"


def test_the_capability_is_part_of_the_channel_contract_and_form() -> None:
    connection = yaml.safe_load((BUNDLE_ROOT / "contracts/connection_types.yaml").read_text())
    capabilities = connection["connection_types"][0]["config_schema"]["properties"]["channels"][
        "items"
    ]["properties"]["capabilities"]
    assert capabilities["properties"]["workflow_trigger"] == {"type": "boolean"}
    # Existing channels without the capability stay valid.
    assert capabilities["required"] == ["notify", "human_decision"]

    form = yaml.safe_load((BUNDLE_ROOT / "ui/components/channels_form.yaml").read_text())
    repeater = form["ui_schema"]["fields"][0]["props"]["fields"]
    field = next(item for item in repeater if item["path"] == "capabilities.workflow_trigger")
    assert field["default"] is False
    for language in ("en", "lt"):
        bundle = yaml.safe_load((BUNDLE_ROOT / "locales" / f"{language}.yaml").read_text())
        key = "component.channels_form.field.channels.field.capabilities.workflow_trigger.label"
        assert bundle[key]

from __future__ import annotations

from pathlib import Path

import yaml

BUNDLE_ROOT = Path(__file__).resolve().parents[1]


def _load_yaml(relative_path: str) -> dict:
    return yaml.safe_load((BUNDLE_ROOT / relative_path).read_text(encoding="utf-8"))


def test_telegram_declares_a_v2_notification_connection() -> None:
    connection_types = _load_yaml("contracts/connection_types.yaml")["connection_types"]
    operation_manifest = _load_yaml("contracts/operation_manifest.yaml")["operations"]
    step_ui_manifest = _load_yaml("contracts/step_ui_manifest.yaml").get("forms", [])

    assert [row["connection_type_id"] for row in connection_types] == ["telegram_bot"]
    delivery = [row for row in operation_manifest if row["operation_id"] == "deliver_notification"]
    assert delivery == [
        {
            "operation_id": "deliver_notification",
            "display_name": "Deliver Notification",
            "description": "Internal notification-provider delivery contract.",
            "operation_kind": "action",
            "category": "notification",
            "workflow_visible": False,
            "inputs": [
                {"name": "event", "value_type": "object", "required": True},
                {"name": "route", "value_type": "object", "required": True},
            ],
            "outputs": [
                {"name": "delivered", "value_type": "boolean"},
                {"name": "provider_message_id", "value_type": "text"},
            ],
        }
    ]
    # The one form is the Send Message step's; delivery has none.
    assert [form["operation_id"] for form in step_ui_manifest] == ["send_message"]

    config_schema = connection_types[0]["config_schema"]
    assert "parse_mode" not in config_schema["properties"]


def test_telegram_management_actions_are_not_workflow_steps() -> None:
    """Only Send Message is a workflow step; delivery and setup actions are not."""
    operations = _load_yaml("contracts/operation_manifest.yaml")["operations"]
    actions = _load_yaml("ui/actions/actions.yaml")["actions"]

    assert [row["operation_id"] for row in operations if row.get("workflow_visible")] == [
        "send_message"
    ]
    assert [row["action_id"] for row in actions if row.get("workflow_visible")] == [
        "send_message"
    ]


def test_telegram_delivery_declares_an_idempotent_external_effect() -> None:
    manifest = _load_yaml("extension.yaml")
    actions = _load_yaml("ui/actions/actions.yaml")["actions"]

    effects = {row["operation_id"]: row for row in manifest["external_effects"]}
    assert set(effects) == {"deliver_notification", "send_message"}
    assert effects["send_message"]["test_mode_behavior"] == "suppress"
    assert effects["send_message"]["idempotency"] == "required"
    assert [effects["deliver_notification"]] == [
        {
            "operation_id": "deliver_notification",
            "effect_kind": "message_delivery",
            "channel": "telegram",
            "idempotency": "required",
            "test_mode_behavior": "suppress",
            "observability": {
                "target_fields": ["channel_id"],
                "target_kind": "channel",
                "target_label": "Telegram channel",
            },
        }
    ]
    delivery_action = next(row for row in actions if row.get("action_id") == "deliver_notification")
    assert delivery_action["workflow_visible"] is False
    assert delivery_action["handler"]["config"]["host_mutations"] == ["project_connection_upsert"]
    assert delivery_action["handler"]["config"]["pre_dispatch_host_mutations"] == [
        "project_connection_upsert"
    ]
    assert delivery_action["mutates_platform"] is True
    assert delivery_action["command_type"] == "invoke_extension_action"


def test_telegram_ui_actions_declare_the_inputs_their_handlers_consume() -> None:
    action_definitions = _load_yaml("ui/actions/actions.yaml")["actions"]
    actions = {str(row["action_id"]): row for row in action_definitions if isinstance(row, dict)}

    expected_inputs = {
        "test_delivery": {"channel_id"},
        "save_config": {
            "disable_web_page_preview",
            "message_prefix",
        },
        "save_inbound_settings": {"inbound_mode", "polling_interval_seconds"},
        "save_secrets": {"bot_token"},
        "resolve_chat": {"chat_link"},
        "discover_chat": {"chat_link"},
        "save_discovered_chat": {
            "chat_id",
            "channel_id",
            "display_name",
            "title",
            "username",
        },
        "upsert_channel": {"channels"},
        "toggle_channel": {"channel_id"},
        "delete_channel": {"channel_id"},
    }

    assert {
        action_id: {
            str(parameter["name"])
            for parameter in actions[action_id]["parameters"]
            if isinstance(parameter, dict)
        }
        for action_id in expected_inputs
    } == expected_inputs
    test_channel = next(
        parameter
        for parameter in actions["test_delivery"]["parameters"]
        if parameter["name"] == "channel_id"
    )
    assert test_channel["required"] is True

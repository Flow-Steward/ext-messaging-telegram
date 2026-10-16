from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml
from subprocess_contract_helpers import host_envelope

BUNDLE_ROOT = Path(__file__).resolve().parents[1]


def _invoke(request: dict, timeout: int = 5) -> tuple[dict, int]:
    proc = subprocess.run(
        [sys.executable, str(BUNDLE_ROOT / "main.py")],
        input=json.dumps(host_envelope(request)),
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(BUNDLE_ROOT),
    )
    return (json.loads(proc.stdout.strip()) if proc.stdout.strip() else {}, proc.returncode)


_UI = BUNDLE_ROOT / "ui"
_PAGE_ORDER = ["overview", "setup", "chats_channels", "inbound", "deliveries"]


def _page(page_id: str) -> dict:
    return yaml.safe_load((_UI / "pages" / f"{page_id}.yaml").read_text()) or {}


def _components(page: dict) -> list[dict]:
    rows = []
    for item in page.get("components") or []:
        path = (_UI / "pages" / str(item["ref"])).resolve()
        assert path.is_file(), f"unresolved component ref {item['ref']}"
        rows.append(yaml.safe_load(path.read_text()) or {})
    return rows


def test_ui_is_five_pages_in_setup_order() -> None:
    manifest = yaml.safe_load((_UI / "ui_manifest.yaml").read_text()) or {}
    page_ids = [_page_id_of(item) for item in manifest["pages"]]
    assert page_ids == _PAGE_ORDER
    assert not (_UI / "pages" / "configuration.yaml").exists()


def _page_id_of(item: dict) -> str:
    return str((yaml.safe_load((_UI / item["ref"]).read_text()) or {}).get("page_id"))


def test_overview_is_read_only() -> None:
    components = _components(_page("overview"))
    assert [c["type"] for c in components]
    assert all(c["type"] != "form" for c in components)
    assert any(
        c["type"] == "health_panel" and c["query_id"] == "provider_health" for c in components
    )


def test_setup_puts_the_token_card_first_before_guidance_and_optional_defaults() -> None:
    components = _components(_page("setup"))
    ids = [c["component_id"] for c in components]
    assert ids[0] == "secrets_form"
    assert ids.index("secrets_form") < ids.index("quick_start") < ids.index("config_form")
    quick_start = components[ids.index("quick_start")]
    assert quick_start["type"] == "markdown"
    assert "https://t.me/BotFather" in quick_start["content"]
    assert "https://core.telegram.org/bots/tutorial" in quick_start["content"]
    guide = next(c for c in components if c["component_id"] == "setup_guide")
    assert guide["type"] == "markdown"
    assert guide.get("collapsible") is True
    for topic in ("public chat", "private", "permission", "polling", "webhook", "revok"):
        assert topic in guide["content"].lower(), topic


def test_chats_and_channels_explains_before_it_asks() -> None:
    components = _components(_page("chats_channels"))
    ids = [c["component_id"] for c in components]
    assert ids[0].endswith("intro")
    assert components[0]["type"] == "markdown"
    assert (
        ids.index("chat_discovery_form")
        < ids.index("discovered_chats_grid")
        < ids.index("channels_form")
        < ids.index("channel_test_grid")
    )
    channels = components[ids.index("channels_form")]
    assert "test_action" not in channels["data"]
    test_grid = components[ids.index("channel_test_grid")]
    assert test_grid["type"] == "grid"
    assert test_grid["query_id"] == "list_channels"
    assert test_grid["row_actions"] == [
        {
            "action_id": "test_delivery",
            "label": "Send test",
            "scope": "row",
            "visible_if": [
                {"path": "enabled", "eq": True},
                {"path": "capabilities.notify", "eq": True},
            ],
        }
    ]


def test_inbound_is_optional_and_groups_mode_webhook_and_status() -> None:
    components = _components(_page("inbound"))
    ids = [c["component_id"] for c in components]
    assert components[0]["type"] == "markdown"
    assert "optional" in components[0]["content"].lower()
    assert (
        ids.index("inbound_mode_form")
        < ids.index("telegram_webhook_setup")
        < ids.index("inbound_status_grid")
    )


def test_deliveries_owns_one_summary_and_one_history() -> None:
    components = _components(_page("deliveries"))
    assert [c["type"] for c in components] == ["stats_row", "grid"]
    grid = components[1]
    assert [c["key"] for c in grid["columns"]] == [
        "workflow_label",
        "event_label",
        "severity",
        "status",
        "delivered_at",
        "provider_message_id",
        "error_text",
    ]
    assert not (_UI / "components" / "config_delivery_grid.yaml").exists()


def test_every_component_is_referenced_once_and_ids_are_unique() -> None:
    seen: list[str] = []
    for page_id in _PAGE_ORDER:
        seen.extend(c["component_id"] for c in _components(_page(page_id)))
    assert len(seen) == len(set(seen)), seen
    on_disk = {p.name for p in (_UI / "components").glob("*.yaml")}
    referenced = {
        str(item["ref"]).rsplit("/", 1)[-1]
        for page_id in _PAGE_ORDER
        for item in _page(page_id).get("components") or []
    }
    assert on_disk == referenced


def test_channel_component_uses_repeater_contract() -> None:
    component = (
        yaml.safe_load((BUNDLE_ROOT / "ui" / "components" / "channels_form.yaml").read_text()) or {}
    )
    fields = (component.get("ui_schema") or {}).get("fields") or []
    repeater = next(item for item in fields if item.get("field_id") == "channels")
    assert repeater["widget"] == "repeater"
    repeater_fields = (repeater.get("props") or {}).get("fields") or []
    paths = [str(item.get("path") or "") for item in repeater_fields]
    assert "channel_id" in paths
    assert "display_name" in paths
    assert "chat_id" in paths
    assert "capabilities.notify" in paths
    assert "capabilities.human_decision" in paths
    channel_id_field = next(item for item in repeater_fields if item.get("path") == "channel_id")
    enabled_field = next(item for item in repeater_fields if item.get("path") == "enabled")
    notify_field = next(
        item for item in repeater_fields if item.get("path") == "capabilities.notify"
    )
    human_decision_field = next(
        item for item in repeater_fields if item.get("path") == "capabilities.human_decision"
    )
    assert channel_id_field["required"] is True
    assert ((channel_id_field.get("props") or {}).get("immutable_on_edit")) is True
    assert ((channel_id_field.get("props") or {}).get("unique_in_list")) is True
    display_name_field = next(
        item for item in repeater_fields if item.get("path") == "display_name"
    )
    chat_id_field = next(item for item in repeater_fields if item.get("path") == "chat_id")
    assert display_name_field["required"] is True
    assert chat_id_field["required"] is True
    assert enabled_field["default"] is True
    assert notify_field["default"] is True
    assert human_decision_field["default"] is False


def test_deliver_notification_targets_named_channel_and_returns_message_id() -> None:
    response, exit_code = _invoke(
        {
            "action": "deliver_notification",
            "event": {
                "event_type": "job.failed",
                "severity": "error",
                "payload": {"message": "Build failed"},
            },
            "route": {"severity": "error", "route_config": {"channel_id": "ops"}},
            "provider": {
                "provider_secrets": {"bot_token": "fake-token"},
                "provider_config": {
                    "channels": [
                        {
                            "channel_id": "ops",
                            "display_name": "Ops",
                            "chat_id": "-100100",
                            "enabled": True,
                            "capabilities": {"notify": True, "human_decision": True},
                        }
                    ],
                },
            },
        }
    )

    assert exit_code == 0
    assert response["ok"] is True
    assert response["provider_message_id"] == "1"


def test_ingress_event_keeps_project_id_as_string() -> None:
    response, exit_code = _invoke(
        {
            "action": "ingress_event",
            "request": {
                "callback": {"authenticated": True},
                "provider": {
                    "project_id": "58eeaeb3-a9c1-4c51-b292-14eb86899d86",
                    "ingress_config": {
                        "pending_hitl_tokens": {
                            "project-token": {
                                "project_id": "58eeaeb3-a9c1-4c51-b292-14eb86899d86",
                                "job_id": "42",
                                "approval_id": "gate-42",
                                "allowed_actions": ["approve", "reject"],
                                "chat_id": "-100123",
                                "created_at": datetime.now(UTC).isoformat(),
                            }
                        }
                    },
                    "provider_config": {
                        "channels": [
                            {
                                "channel_id": "ops",
                                "chat_id": "-100123",
                                "enabled": True,
                                "capabilities": {"notify": True, "human_decision": True},
                            }
                        ]
                    },
                },
                "payload": {
                    "callback_query": {
                        "id": "cb-1",
                        "data": "tgx:a:project-token",
                        "from": {"id": 10, "username": "alice"},
                        "message": {
                            "message_id": 100,
                            "chat": {"id": -100123},
                            "text": "Review requested",
                        },
                    }
                },
            },
        }
    )

    assert exit_code == 0
    assert response["ok"] is True
    assert response["result"]["project_id"] == "58eeaeb3-a9c1-4c51-b292-14eb86899d86"


def test_project_scoped_actions_require_project_context() -> None:
    response, exit_code = _invoke(
        {
            "mode": "action",
            "action": {"action_id": "test_delivery", "input": {}},
        }
    )

    assert exit_code == 2
    assert response["ok"] is False
    assert response["error_code"] == "project_context_required"
    assert response["error"] == "Select a project before running this Telegram action."


def test_prepare_notification_requires_named_channel_in_canonical_flow() -> None:
    project_id = "58eeaeb3-a9c1-4c51-b292-14eb86899d86"
    response, exit_code = _invoke(
        {
            "action": "deliver_notification",
            "delivery_phase": "prepare",
            "runtime_context": {
                "resources": {
                    "project_connection": {
                        "connection_id": "conn-hitl",
                        "project_id": project_id,
                        "extension_id": "flowsteward.telegram",
                        "extension_install_id": "install-hitl",
                        "connection_type": "telegram_bot",
                        "enabled": True,
                        "connection_config": {},
                        "ingress_config": {},
                    }
                }
            },
            "event": {
                "event_type": "approval.requested",
                "severity": "warning",
                "project_id": project_id,
                "job_id": "6434ac0d-8323-4746-a5f4-3305d42abeb9",
                "payload": {
                    "message": "Please approve",
                    "approval_id": "gate-42",
                    "allowed_actions": ["approve", "reject"],
                },
            },
            "route": {
                "severity": "warning",
                "route_config": {"channel_id": "approvals", "message_suffix": "via Flow Steward"},
            },
            "provider": {
                "project_id": project_id,
                "provider_secrets": {"bot_token": "fake-token"},
                "provider_config": {
                    "channels": [
                        {
                            "channel_id": "approvals",
                            "display_name": "Approvals",
                            "chat_id": "-100124",
                            "enabled": True,
                            "capabilities": {"notify": True, "human_decision": True},
                        }
                    ],
                },
            },
        }
    )

    assert exit_code == 0
    assert response["ok"] is True
    assert response["delivered"] is False
    assert response["prepared_delivery"]["reply_markup"]["inline_keyboard"]
    assert "provider_message_id" not in response
    assert [row["kind"] for row in response["host_mutations"]] == ["project_connection_upsert"]

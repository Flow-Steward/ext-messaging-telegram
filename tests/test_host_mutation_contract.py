from __future__ import annotations

import yaml
from subprocess_contract_helpers import BUNDLE_ROOT, _invoke


def _connection() -> dict:
    return {
        "project_id": "proj-7",
        "extension_id": "flowsteward.telegram",
        "enabled": True,
        "connection_config": {
            "channels": [
                {
                    "channel_id": "ops",
                    "display_name": "Ops",
                    "chat_id": "-100123",
                    "enabled": True,
                    "capabilities": {"notify": True, "human_decision": False},
                }
            ]
        },
        "ingress_config": {
            "discovered_chats": [
                {
                    "chat_id": "-100456",
                    "title": "Release Room",
                    "display_name": "Release Room",
                }
            ]
        },
    }


def test_retry_delivery_returns_declared_host_log_mutation() -> None:
    response, code = _invoke(
        {
            "mode": "action",
            "runtime_context": {
                "resources": {
                    "project_connection": _connection(),
                    "provider_secrets": {"bot_token": "fake-token"},
                    "notification_deliveries": [
                        {
                            "delivery_id": "delivery-1",
                            "project_id": "proj-7",
                            "provider_key": "flowsteward.telegram",
                            "event_type": "job.failed",
                            "severity": "error",
                            "status": "failed",
                            "request": {
                                "event": {
                                    "event_type": "job.failed",
                                    "severity": "error",
                                    "summary": "Failure",
                                },
                                "route": {"route_config": {"channel_id": "ops"}},
                            },
                        }
                    ],
                }
            },
            "action": {
                "action_id": "retry_delivery",
                "context": {"project_id": "proj-7"},
                "target": {"item_ids": ["delivery-1"]},
                "input": {},
            },
        }
    )

    assert code == 0
    assert response["result"]["delivered"] is True
    assert response["host_mutations"][0]["kind"] == "notification_delivery_log"
    assert response["host_mutations"][0]["payload"]["status"] == "delivered"


def test_save_discovered_chat_returns_connection_upsert_mutation() -> None:
    response, code = _invoke(
        {
            "mode": "action",
            "runtime_context": {"resources": {"project_connection": _connection()}},
            "action": {
                "action_id": "save_discovered_chat",
                "context": {"project_id": "proj-7"},
                "target": {"chat_id": "-100456", "title": "Release Room"},
                "input": {},
            },
        }
    )

    assert code == 0
    mutation = response["host_mutations"][0]
    assert mutation["kind"] == "project_connection_upsert"
    channels = mutation["payload"]["connection_config"]["channels"]
    assert any(row["chat_id"] == "-100456" for row in channels)
    assert all("_meta" not in row for row in channels)
    assert all("_meta" in row for row in response["values"]["channels"])


def test_manifest_declares_every_emitted_host_mutation() -> None:
    payload = yaml.safe_load((BUNDLE_ROOT / "ui/actions/actions.yaml").read_text())
    actions = {row["action_id"]: row for row in payload["actions"]}

    retry = actions["retry_delivery"]["handler"]["config"]
    save_chat = actions["save_discovered_chat"]["handler"]["config"]
    assert retry["host_mutations"] == ["notification_delivery_log"]
    assert save_chat["host_mutations"] == ["project_connection_upsert"]

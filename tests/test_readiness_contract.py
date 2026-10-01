"""Overview readiness is derived from current facts, in the spec's vocabulary.

`Setup incomplete` = required configuration missing; `Ready` = valid token and
an enabled notification channel; `Needs attention` = a configured optional or
operational part is degraded; `Failed` only for a confirmed failure. A failed
delivery marks Deliveries failed and rolls the summary to Needs attention
without claiming the bot is unavailable. Inbound `Off` never reduces outbound
readiness. No token value ever appears in the result.
"""

from __future__ import annotations

import json
import sys

import pytest
from subprocess_contract_helpers import BUNDLE_ROOT

if str(BUNDLE_ROOT) not in sys.path:
    sys.path.insert(0, str(BUNDLE_ROOT))

from main_readiness import derive_readiness

_TOKEN = "123456:TOKEN-MUST-NEVER-BE-ECHOED"
_ME_OK = {"ok": True, "result": {"id": 42, "is_bot": True, "username": "flow_steward_bot"}}
_ME_401 = {"ok": False, "error": "HTTP Error 401: Unauthorized"}
_ME_DOWN = {"ok": False, "error": "<urlopen error [Errno 8] nodename nor servname provided>"}

_NOTIFY = {
    "channel_id": "ops",
    "display_name": "Ops",
    "chat_id": "-100123",
    "enabled": True,
    "capabilities": {"notify": True, "human_decision": False},
}
_HITL = {
    "channel_id": "hitl",
    "display_name": "Approvals",
    "chat_id": "-100456",
    "enabled": True,
    "capabilities": {"notify": False, "human_decision": True},
}
_DISABLED = {
    "channel_id": "old",
    "display_name": "Old",
    "chat_id": "-100789",
    "enabled": False,
    "capabilities": {"notify": True, "human_decision": False},
}


def _readiness(**overrides):
    params = {
        "connection": {"connection_id": "conn-7"},
        "token_present": True,
        "identity_response": _ME_OK,
        "channels": [_NOTIFY],
        "inbound_state": {"inbound_mode": "auto", "webhook_status": {}, "polling_status": "idle"},
        "deliveries": [],
    }
    params.update(overrides)
    return derive_readiness(**params)


def _check(result: dict, name: str) -> dict:
    return next(row for row in result["checks"] if row["name"] == name)


def test_no_token_is_setup_incomplete_with_a_configure_bot_action():
    result = _readiness(token_present=False, identity_response=None)

    bot = _check(result, "Bot")
    assert bot["readiness"] == "setup_incomplete"
    assert bot["action"]["label"] == "Configure bot"
    assert bot["action"]["page_id"] == "setup"
    assert result["summary"]["readiness"] == "setup_incomplete"
    assert result["summary"]["status"] == "Setup incomplete"


def test_rejected_token_is_failed_with_a_replace_token_action():
    result = _readiness(identity_response=_ME_401)

    bot = _check(result, "Bot")
    assert bot["readiness"] == "failed"
    assert bot["status"] == "failed"
    assert "rejected this bot token" in bot["detail"]
    assert bot["action"]["label"] == "Replace token"
    assert result["summary"]["readiness"] == "failed"


def test_unreachable_telegram_is_needs_attention_not_failed():
    result = _readiness(identity_response=_ME_DOWN)

    bot = _check(result, "Bot")
    assert bot["readiness"] == "needs_attention"
    assert "could not be reached" in bot["detail"]
    assert result["summary"]["readiness"] == "needs_attention"


def test_valid_token_without_an_enabled_notification_channel_is_setup_incomplete():
    result = _readiness(channels=[_HITL, _DISABLED])

    assert _check(result, "Bot")["readiness"] == "ready"
    assert _check(result, "Bot")["detail"] == "Connected as @flow_steward_bot"
    channels = _check(result, "Channels")
    assert channels["readiness"] == "setup_incomplete"
    assert channels["action"]["label"] == "Add channel"
    assert channels["action"]["page_id"] == "chats_channels"
    assert result["summary"]["readiness"] == "setup_incomplete"


def test_valid_token_and_an_enabled_notification_channel_is_ready():
    result = _readiness(channels=[_NOTIFY, _HITL, _DISABLED])

    channels = _check(result, "Channels")
    assert channels["readiness"] == "ready"
    assert channels["detail"] == "Enabled: 1 notification, 1 human-decision"
    assert "action" not in channels
    assert result["summary"]["readiness"] == "ready"
    assert result["summary"]["status"] == "Ready"


def test_malformed_enabled_notification_channel_is_not_ready():
    malformed = {
        "channel_id": "ops",
        "display_name": "Ops",
        "enabled": True,
        "capabilities": {"notify": True, "human_decision": False},
    }

    result = _readiness(channels=[malformed])

    channels = _check(result, "Channels")
    assert channels["readiness"] == "setup_incomplete"
    assert result["summary"]["readiness"] == "setup_incomplete"


def test_inbound_off_is_reported_optional_and_does_not_degrade_readiness():
    result = _readiness()

    inbound = _check(result, "Inbound")
    assert inbound["readiness"] == "off"
    assert inbound["detail"].startswith("Off")
    assert "optional" in inbound["detail"]
    assert inbound["action"]["label"] == "Configure inbound"
    assert inbound["action"]["page_id"] == "inbound"
    assert result["summary"]["readiness"] == "ready"


def test_configured_webhook_transport_failing_is_needs_attention():
    result = _readiness(
        inbound_state={
            "inbound_mode": "webhook",
            "webhook_status": {
                "status": "failed",
                "url_configured": False,
                "last_error_message": "boom",
            },
            "polling_status": "idle",
        }
    )

    inbound = _check(result, "Inbound")
    assert inbound["readiness"] == "needs_attention"
    assert inbound["detail"].startswith("Webhook")
    assert result["summary"]["readiness"] == "needs_attention"


def test_healthy_polling_is_reported_as_polling():
    result = _readiness(
        inbound_state={"inbound_mode": "polling", "webhook_status": {}, "polling_status": "healthy"}
    )

    inbound = _check(result, "Inbound")
    assert inbound["readiness"] == "ready"
    assert inbound["detail"].startswith("Polling")


def test_recent_delivery_failure_rolls_to_needs_attention_without_blaming_the_bot():
    result = _readiness(
        deliveries=[
            {
                "status": "failed",
                "created_at": "2026-09-05T10:00:00Z",
                "error_text": "chat not found",
            },
            {"status": "delivered", "created_at": "2026-09-05T09:00:00Z"},
        ]
    )

    deliveries = _check(result, "Deliveries")
    assert deliveries["readiness"] == "failed"
    assert deliveries["status"] == "failed"
    assert "1 delivered, 1 failed" in deliveries["detail"]
    assert "chat not found" in deliveries["detail"]
    assert _check(result, "Bot")["readiness"] == "ready"
    assert result["summary"]["readiness"] == "needs_attention"


def test_no_historical_health_row_and_no_token_in_the_result():
    result = _readiness(
        identity_response={"ok": False, "error": f"https://api.telegram.org/bot{_TOKEN}/getMe boom"}
    )

    assert [row["name"] for row in result["checks"]] == ["Bot", "Channels", "Inbound", "Deliveries"]
    assert _TOKEN not in json.dumps(result)


# --- the query itself, in-process with the Bot API answered locally ----------


@pytest.fixture
def bot_api(monkeypatch):
    import main_queries

    responses: dict[str, dict] = {"getMe": _ME_OK}
    calls: list[str] = []

    def _api(token: str, method_name: str, payload: dict | None = None) -> dict:
        calls.append(method_name)
        return responses.get(method_name, {"ok": False, "error": f"unexpected {method_name}"})

    monkeypatch.setattr(main_queries, "_telegram_api_call", _api)
    return responses, calls


def _query(*, secrets: dict | None, connection: dict | None):
    import main_queries

    resources: dict = {}
    if connection is not None:
        resources["project_connection"] = connection
    if secrets is not None:
        resources["provider_secrets"] = secrets
    return main_queries._handle_query(
        {
            "query_id": "provider_health",
            "context": {"project_id": "proj-7"},
            "runtime_context": {"resources": resources},
        }
    )


def test_query_derives_ready_from_stored_facts_and_a_live_getme(bot_api):
    _responses, calls = bot_api
    response, code = _query(
        secrets={"bot_token": _TOKEN},
        connection={
            "connection_id": "conn-7",
            "extension_id": "flowsteward.telegram",
            "connection_config": {"channels": [_NOTIFY]},
            "ingress_config": {"inbound_mode": "auto"},
        },
    )

    assert code == 0, response
    assert calls == ["getMe"]
    assert response["summary"]["readiness"] == "ready"
    assert response["summary"]["bot_username"] == "flow_steward_bot"
    assert _TOKEN not in json.dumps(response)


def test_query_does_not_call_telegram_without_a_token(bot_api):
    _responses, calls = bot_api
    response, code = _query(secrets={}, connection=None)

    assert code == 0, response
    assert calls == []
    assert response["summary"]["readiness"] == "setup_incomplete"


def test_every_contextual_action_targets_an_anchored_component_on_its_page():
    # The renderer focuses `extension-anchor-<page>-<anchor>`; an action pointing at a
    # component without `anchor_id` would switch pages and land nowhere.
    import yaml

    ui = BUNDLE_ROOT / "ui"
    targets = set()
    for result in (
        _readiness(token_present=False, identity_response=None),
        _readiness(identity_response=_ME_401),
        _readiness(identity_response=_ME_DOWN),
        _readiness(channels=[]),
        _readiness(deliveries=[{"status": "failed", "created_at": "2026-09-05T10:00:00Z"}]),
    ):
        for row in result["checks"]:
            action = row.get("action")
            if action:
                targets.add((action["page_id"], action["component_id"]))
    assert len(targets) >= 4
    for page_id, component_id in sorted(targets):
        page = yaml.safe_load((ui / "pages" / f"{page_id}.yaml").read_text())
        components = [
            yaml.safe_load((ui / "pages" / item["ref"]).resolve().read_text())
            for item in page["components"]
        ]
        match = [c for c in components if c["component_id"] == component_id]
        assert match, (page_id, component_id)
        assert match[0].get("anchor_id") == component_id, (page_id, component_id)

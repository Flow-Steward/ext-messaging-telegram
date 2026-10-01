"""Verify and test-delivery answer with actionable copy, never with a token or a code alone.

Verification uses `getMe` with the transport's bounded timeout and shows the
bot username. A rejected token and an unreachable Telegram are different
outcomes: one asks for a new token, the other says the saved token is
unverified and asks to retry.
"""

from __future__ import annotations

import json
import sys

import pytest
import yaml
from subprocess_contract_helpers import BUNDLE_ROOT

if str(BUNDLE_ROOT) not in sys.path:
    sys.path.insert(0, str(BUNDLE_ROOT))

_TOKEN = "123456:TOKEN-MUST-NEVER-BE-ECHOED"


@pytest.fixture
def bot_api(monkeypatch):
    import main_actions
    import main_state

    responses: dict[str, dict] = {}
    calls: list[str] = []

    def _api(token: str, method_name: str, payload: dict | None = None) -> dict:
        calls.append(method_name)
        return responses.get(method_name, {"ok": False, "error": f"unexpected {method_name}"})

    monkeypatch.setattr(main_actions, "_telegram_api_call", _api)
    main_state._REQUEST_HOST_MUTATIONS.clear()
    yield responses, calls
    main_state._REQUEST_HOST_MUTATIONS.clear()


def _run(action_id: str, *, secrets: dict | None, connection: dict | None = None, payload=None):
    import main_actions

    resources: dict = {}
    if connection is not None:
        resources["project_connection"] = connection
    if secrets is not None:
        resources["provider_secrets"] = secrets
    return main_actions._handle_action(
        {
            "action_id": action_id,
            "context": {"project_id": "proj-7"},
            "input": payload or {},
            "runtime_context": {"resources": resources},
        }
    )


def test_verify_reports_the_connected_bot_without_the_token(bot_api):
    responses, calls = bot_api
    responses["getMe"] = {"ok": True, "result": {"id": 42, "username": "flow_steward_bot"}}

    response, code = _run("verify_bot_token", secrets={"bot_token": _TOKEN})

    assert code == 0
    assert calls == ["getMe"]
    assert response["ok"] is True
    assert response["message"] == "Connected as @flow_steward_bot"
    assert response["result"] == {
        "verified": True,
        "bot_username": "flow_steward_bot",
        "bot_id": 42,
    }
    assert _TOKEN not in json.dumps(response)


def test_verify_names_a_rejected_token_and_asks_for_a_new_one(bot_api):
    responses, _calls = bot_api
    responses["getMe"] = {"ok": False, "error": "HTTP Error 401: Unauthorized"}

    response, code = _run("verify_bot_token", secrets={"bot_token": _TOKEN})

    assert code != 0
    assert response["ok"] is False
    assert response["error"] == "bot_token_rejected"
    assert response["message"] == (
        "Telegram rejected this bot token. Copy a current token from @BotFather and try again."
    )
    assert response["result"]["verified"] is False


def test_verify_distinguishes_an_unreachable_telegram_from_a_bad_token(bot_api):
    responses, _calls = bot_api
    responses["getMe"] = {"ok": False, "error": "<urlopen error timed out>"}

    response, code = _run("verify_bot_token", secrets={"bot_token": _TOKEN})

    assert code != 0
    assert response["error"] == "telegram_unreachable"
    assert response["message"] == (
        "The token was saved, but Telegram could not be reached for verification. Try Verify again."
    )


def test_verify_without_a_saved_token_points_at_the_token_card(bot_api):
    _responses, calls = bot_api

    response, code = _run("verify_bot_token", secrets={})

    assert code != 0
    assert calls == []
    assert response["error"] == "bot_token_required"
    assert "Save a bot token" in response["message"]


def test_test_delivery_without_an_enabled_channel_says_so_before_calling_telegram(bot_api):
    _responses, calls = bot_api
    connection = {
        "connection_id": "conn-7",
        "extension_id": "flowsteward.telegram",
        "connection_config": {
            "channels": [{"channel_id": "old", "chat_id": "-1", "enabled": False}],
        },
    }

    response, code = _run("test_delivery", secrets={"bot_token": _TOKEN}, connection=connection)

    assert code != 0
    assert calls == []
    assert response["error"] == "channel_required"
    assert response["message"] == (
        "Choose an enabled Telegram channel before sending a test notification."
    )


def test_test_delivery_requires_and_uses_the_explicit_selected_channel(bot_api, monkeypatch):
    import main_actions

    _responses, calls = bot_api
    delivered_routes: list[dict] = []
    connection = {
        "connection_id": "conn-7",
        "extension_id": "flowsteward.telegram",
        "connection_config": {
            "channels": [
                {
                    "channel_id": "old-self-bot",
                    "chat_id": "-1",
                    "enabled": True,
                    "capabilities": {"notify": True},
                },
                {
                    "channel_id": "release-audit-group",
                    "chat_id": "-1007",
                    "enabled": True,
                    "capabilities": {"notify": True},
                },
            ],
        },
    }

    def deliver(_event, route, _provider):
        delivered_routes.append(route)
        return {"ok": True, "delivered": True, "provider_message_id": "message-7"}

    monkeypatch.setattr(main_actions, "_deliver", deliver)

    missing, missing_code = _run(
        "test_delivery",
        secrets={"bot_token": _TOKEN},
        connection=connection,
    )
    selected, selected_code = _run(
        "test_delivery",
        secrets={"bot_token": _TOKEN},
        connection=connection,
        payload={"channel_id": "release-audit-group"},
    )

    assert missing_code != 0
    assert missing["error"] == "channel_required"
    assert delivered_routes == [
        {"severity": "info", "route_config": {"channel_id": "release-audit-group"}}
    ]
    assert selected_code == 0
    assert selected["ok"] is True
    assert selected["result"]["provider_message_id"] == "message-7"
    assert calls == []


def test_manifest_declares_verify_and_the_token_card_offers_it():
    actions = yaml.safe_load((BUNDLE_ROOT / "ui/actions/actions.yaml").read_text())["actions"]
    verify = next(row for row in actions if row["action_id"] == "verify_bot_token")
    assert verify["handler"]["mode"] == "extension"
    assert verify["mutates_platform"] is False
    assert verify["handler"]["config"]["runtime_resources"]["provider_secrets"] == ["bot_token"]
    assert {"bot_token_required", "bot_token_rejected", "telegram_unreachable"} <= set(
        verify["error_codes"]
    )

    form = yaml.safe_load((BUNDLE_ROOT / "ui/components/secrets_form.yaml").read_text())
    assert form["ui_schema"]["actions"]["verify"]["action_id"] == "verify_bot_token"
    token_field = next(f for f in form["ui_schema"]["fields"] if f["field_id"] == "bot_token")
    assert "never displayed after save" in token_field["help"]
    assert "authenticates the bot" in token_field["help"]

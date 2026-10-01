from __future__ import annotations

from typing import Any
from unittest.mock import patch

from main_ingress import (
    _ingress_event_response,
    _registration_project_mismatch,
)

REGISTRATION_PROJECT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_PROJECT = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
FOREIGN_JOB = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def _request_payload(*, message_text: str) -> dict[str, Any]:
    return {
        "callback": {
            "authenticated": True,
            "project_id": REGISTRATION_PROJECT,
        },
        "provider": {
            "provider_config": {
                "project_id": REGISTRATION_PROJECT,
                "token": "bot-token",
                "channels": [
                    {
                        "channel_id": "ops",
                        "chat_id": "-100123",
                        "enabled": True,
                        "capabilities": {"notify": True, "human_decision": True},
                    }
                ],
            },
        },
        "payload": {
            "message": {
                "text": message_text,
                "chat": {"id": -100123},
                "from": {"id": 42, "username": "attacker"},
                "message_id": 7,
            }
        },
    }


def test_registration_project_mismatch_helper():
    assert _registration_project_mismatch(
        registration_project_id=REGISTRATION_PROJECT,
        resolved_project_id=OTHER_PROJECT,
    )
    assert not _registration_project_mismatch(
        registration_project_id=REGISTRATION_PROJECT,
        resolved_project_id=REGISTRATION_PROJECT,
    )
    assert not _registration_project_mismatch(
        registration_project_id=None,
        resolved_project_id=OTHER_PROJECT,
    )


def test_message_decision_rejected_for_cross_project_job():
    request_payload = _request_payload(message_text=f"approve {FOREIGN_JOB}")

    with patch(
        "main_ingress._resolve_pending_hitl_token",
        return_value={"project_id": OTHER_PROJECT, "job_id": FOREIGN_JOB},
    ):
        result = _ingress_event_response(request_payload)

    assert result["ok"] is False
    assert result["error"] == "cross_project_human_decision"
    assert result["events"] == []


def test_message_decision_allowed_for_same_project_job():
    same_job = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    request_payload = _request_payload(message_text=f"approve {same_job}")

    resolved = {
        "project_id": REGISTRATION_PROJECT,
        "job_id": same_job,
        "approval_id": "gate-1",
        "allowed_actions": ["approve", "reject"],
    }
    with patch("main_ingress._resolve_pending_hitl_token", return_value=resolved):
        result = _ingress_event_response(request_payload)

    assert result["ok"] is True
    assert result["events"], "same-project decision should emit a reaction event"
    assert result["events"][0]["project_id"] == REGISTRATION_PROJECT

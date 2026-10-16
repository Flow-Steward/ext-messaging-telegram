"""The final extension_host_v2 envelope, execution scope and approval targets."""

from __future__ import annotations

import io
import json
import sys
from typing import Any

import pytest
from subprocess_contract_helpers import (
    FOR_EACH_STEP_SCOPE,
    GROUP_STEP_SCOPE,
    NO_STEP_SCOPE,
    ROOT_STEP_SCOPE,
    _invoke,
    host_envelope,
)

PROJECT_ID = "58eeaeb3-a9c1-4c51-b292-14eb86899d86"
PARENT_JOB_ID = "a1111111-1111-4111-8111-111111111111"
CHILD_JOB_ID = "c2222222-2222-4222-8222-222222222222"
APPROVALS_CHAT_ID = "-100124"


def _channels() -> list[dict[str, Any]]:
    return [
        {
            "channel_id": "approvals",
            "display_name": "Approvals",
            "chat_id": APPROVALS_CHAT_ID,
            "enabled": True,
            "capabilities": {"notify": True, "human_decision": True},
        }
    ]


def _connection(*, ingress_config: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "connection_id": "conn-hitl",
        "project_id": PROJECT_ID,
        "extension_id": "flowsteward.telegram",
        "extension_install_id": "install-hitl",
        "connection_type": "telegram_bot",
        "enabled": True,
        "config": {"channels": _channels()},
        "ingress_config": ingress_config or {},
        "secrets": {"bot_token": "fake-token"},
    }


def _send_message_action() -> dict[str, Any]:
    return {
        "mode": "action",
        "action": {
            "action_id": "send_message",
            "context": {"project_id": PROJECT_ID},
            "target": {"connection": _connection()},
            "input": {"channel_id": "approvals", "text": "Order shipped"},
        },
    }


def _deliver_request(event: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "action": "deliver_notification",
        "event": event,
        "route": {"severity": "info", "route_config": {"channel_id": "approvals"}},
        "provider": {"project_id": PROJECT_ID},
        "action_payload": {
            "action_id": "deliver_notification",
            "target": {"connection": _connection()},
        },
        **extra,
    }


def _ingress_request() -> dict[str, Any]:
    return {
        "action": "ingress_event",
        "request": {
            "callback": {"authenticated": True},
            "provider": {"project_id": PROJECT_ID, "provider_config": {"channels": _channels()}},
            "payload": {
                "callback_query": {
                    "id": "cb-1",
                    "data": "tgx:a:abc123token",
                    "from": {"id": 10, "username": "alice"},
                    "message": {"message_id": 7, "chat": {"id": int(APPROVALS_CHAT_ID)}},
                }
            },
        },
    }


def _run_main(monkeypatch: pytest.MonkeyPatch, capsys, request: dict[str, Any]):
    import main as telegram_main

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    code = telegram_main.main()
    return json.loads(capsys.readouterr().out.strip()), code


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every Telegram send of the request, without a network call."""
    import main as telegram_main
    import main_send

    calls: list[dict[str, Any]] = []

    def fake_send(token: str, chat_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"chat_id": chat_id, "message": message, **kwargs})
        return {"ok": True, "message_id": 42}

    monkeypatch.setattr(telegram_main, "_send_message", fake_send)
    monkeypatch.setattr(main_send, "_send_message", fake_send)
    return calls


@pytest.mark.parametrize(
    "scope",
    [ROOT_STEP_SCOPE, GROUP_STEP_SCOPE, FOR_EACH_STEP_SCOPE],
    ids=["root", "group", "for_each"],
)
def test_send_message_step_runs_for_every_final_scope_owner(
    monkeypatch: pytest.MonkeyPatch, capsys, sent: list[dict[str, Any]], scope: dict[str, Any]
) -> None:
    response, code = _run_main(monkeypatch, capsys, host_envelope(_send_message_action(), scope))

    assert code == 0, response
    assert response["ok"] is True
    assert response["delivered"] is True
    assert response["provider_message_id"] == "42"
    assert [call["chat_id"] for call in sent] == [APPROVALS_CHAT_ID]


@pytest.mark.parametrize(
    ("scope", "scope_lines", "absent"),
    [
        (ROOT_STEP_SCOPE, ["Step: Notify team"], ["Group:", "For each:"]),
        (
            GROUP_STEP_SCOPE,
            ["Group: Review changes", "Step: Notify team"],
            ["For each:"],
        ),
        (
            FOR_EACH_STEP_SCOPE,
            ["For each: Each order", "Step: Notify team"],
            ["Group:"],
        ),
    ],
    ids=["root", "group", "for_each"],
)
def test_notification_delivery_renders_the_scope_of_its_event(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    sent: list[dict[str, Any]],
    scope: dict[str, Any],
    scope_lines: list[str],
    absent: list[str],
) -> None:
    event = {
        "event_type": "job.step_failed",
        "severity": "error",
        "project_id": PROJECT_ID,
        "job_id": PARENT_JOB_ID,
        "payload": {
            "workflow_label": "Order sync",
            "message": "The step failed.",
            "step_label": "Notify team",
            "group_label": "Review changes" if scope["group_id"] else None,
            "for_each_label": "Each order" if scope["for_each_id"] else None,
            **scope,
        },
    }

    response, code = _run_main(monkeypatch, capsys, host_envelope(_deliver_request(event), scope))

    assert code == 0, response
    assert response["delivered"] is True
    message = sent[0]["message"]
    for line in scope_lines:
        assert line in message
    for line in absent:
        assert line not in message
    assert "Phase" not in message


_BAD_ENVELOPES = [
    pytest.param({"contract_version": "extension_host_v1"}, "Unsupported host contract", id="v1"),
    pytest.param({"contract_version": ""}, "Unsupported host contract", id="empty"),
    pytest.param({"contract_version": None}, "Unsupported host contract", id="missing"),
    pytest.param(
        {"runtime_context": {k: v for k, v in GROUP_STEP_SCOPE.items() if k != "for_each_id"}},
        "misses execution scope",
        id="missing-scope-key",
    ),
    pytest.param(
        {"runtime_context": {**FOR_EACH_STEP_SCOPE, "group_id": "review_changes"}},
        "both a Group and a For each",
        id="two-owners",
    ),
    pytest.param(
        {"runtime_context": {**ROOT_STEP_SCOPE, "step_execution_id": None}},
        "without its logical execution",
        id="step-without-execution",
    ),
    pytest.param(
        {"runtime_context": {**NO_STEP_SCOPE, "step_execution_id": "5d1c6a3e"}},
        "or the reverse",
        id="execution-without-step",
    ),
]

_REQUESTS = [
    pytest.param(lambda: {"mode": "query", "query": {"query_id": "provider_health"}}, id="query"),
    pytest.param(_send_message_action, id="action"),
    pytest.param(
        lambda: _deliver_request(
            {
                "event_type": "approval.requested",
                "project_id": PROJECT_ID,
                "job_id": PARENT_JOB_ID,
                "payload": {"approval_id": "gate-1", "allowed_actions": ["approve", "reject"]},
            },
            delivery_phase="prepare",
        ),
        id="deliver_notification",
    ),
    pytest.param(_ingress_request, id="ingress_event"),
]


@pytest.mark.parametrize("build_request", _REQUESTS)
@pytest.mark.parametrize(("override", "reason"), _BAD_ENVELOPES)
def test_foreign_or_incomplete_envelope_is_refused_before_any_effect(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    build_request,
    override: dict[str, Any],
    reason: str,
) -> None:
    import urllib.request

    import main as telegram_main

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a refused envelope must not read, send or save anything")

    for name in (
        "_handle_query",
        "_handle_action",
        "_deliver",
        "_handle_ingress_event_payload",
        "_send_message",
        "_telegram_api_call",
    ):
        monkeypatch.setattr(telegram_main, name, forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    request = host_envelope(build_request(), ROOT_STEP_SCOPE)
    if "runtime_context" in override:
        request["runtime_context"] = dict(override["runtime_context"])
    if "contract_version" in override:
        if override["contract_version"] is None:
            request.pop("contract_version")
        else:
            request["contract_version"] = override["contract_version"]

    response, code = _run_main(monkeypatch, capsys, request)

    assert code == 2
    assert response["ok"] is False
    assert response["error_code"] == "invalid_host_envelope"
    assert reason in response["error"]
    assert response["definitely_no_external_effect"] is True
    assert "host_mutations" not in response
    if request.get("action") == "deliver_notification":
        assert response["delivered"] is False


def test_refused_envelope_from_the_real_subprocess() -> None:
    response, code = _invoke(
        {**_send_message_action(), "contract_version": "extension_host_v1"}, envelope=False
    )

    assert code == 2
    assert response["error_code"] == "invalid_host_envelope"
    assert response["definitely_no_external_effect"] is True


def _propagated_event(**payload: Any) -> dict[str, Any]:
    """A child's approval delivered to its waiting parent (Core's propagated_payload)."""
    return {
        "event_type": "approval.requested",
        "severity": "info",
        "project_id": PROJECT_ID,
        "workflow_id": "parent_flow",
        "job_id": PARENT_JOB_ID,
        "payload": {
            "status": "awaiting_child",
            "message": "A child workflow is waiting for approval.",
            "propagated": True,
            "origin_job_id": CHILD_JOB_ID,
            "child_job_id": CHILD_JOB_ID,
            "approval_job_id": CHILD_JOB_ID,
            "approval_id": "child-gate-1",
            "allowed_actions": ["approve", "abort"],
            "wait_id": "wait-1",
            "reason": "recovery",
            "step_id": "run_child",
            "step_execution_id": "5d1c6a3e-2f4b-4c8e-9a1d-7e0f3b2c0003",
            "group_id": None,
            **payload,
        },
    }


def test_propagated_approval_is_decided_on_the_child_approval_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main as telegram_main
    import main_transport

    registered: list[dict[str, Any]] = []

    def register(*_args: Any, **kwargs: Any) -> str:
        registered.append(kwargs)
        return "child-token"

    monkeypatch.setattr(main_transport, "_runtime_logger", lambda *_args: object())
    monkeypatch.setattr(main_transport, "_register_pending_hitl_token", register)
    monkeypatch.setenv("FS_PUBLIC_BASE_URL", "https://flow-steward.example")
    event = _propagated_event()

    reply_markup = telegram_main._approval_reply_markup(event, chat_id=APPROVALS_CHAT_ID)
    message = telegram_main._render_message(event, {"route_config": {}})

    assert [row["job_id"] for row in registered] == [CHILD_JOB_ID]
    assert registered[0]["approval_id"] == "child-gate-1"
    assert [button["text"] for button in reply_markup["inline_keyboard"][0]] == [
        "Approve",
        "Abort",
    ]
    assert f"/jobs/{CHILD_JOB_ID}" in message
    assert PARENT_JOB_ID not in message
    assert "approve or abort" in message
    assert "reject" not in message.lower()


def test_propagated_approval_without_its_job_mints_no_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main as telegram_main
    import main_transport

    def register(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("the parent job must never receive a decision token")

    monkeypatch.setattr(main_transport, "_runtime_logger", lambda *_args: object())
    monkeypatch.setattr(main_transport, "_register_pending_hitl_token", register)

    reply_markup = telegram_main._approval_reply_markup(_propagated_event(approval_job_id=None))

    assert reply_markup == {}


def test_propagated_approval_round_trip_addresses_the_child_job() -> None:
    prepared, code = _invoke(_deliver_request(_propagated_event(), delivery_phase="prepare"))

    assert code == 0, prepared
    ingress_config = prepared["host_mutations"][0]["payload"]["ingress_config"]
    tokens = ingress_config["pending_hitl_tokens"]
    assert len(tokens) == 1
    token, record = next(iter(tokens.items()))
    assert record["job_id"] == CHILD_JOB_ID
    assert record["approval_id"] == "child-gate-1"
    assert record["allowed_actions"] == ["approve", "abort"]
    keyboard = prepared["prepared_delivery"]["reply_markup"]["inline_keyboard"][0]
    assert [button["callback_data"] for button in keyboard] == [f"tgx:a:{token}", f"tgx:b:{token}"]

    def press(data: str) -> tuple[dict[str, Any], int]:
        request = _ingress_request()
        request["request"]["provider"]["ingress_config"] = ingress_config
        request["request"]["payload"]["callback_query"]["data"] = data
        return _invoke(request)

    refused, refused_code = press(f"tgx:r:{token}")
    decided, decided_code = press(f"tgx:b:{token}")

    assert refused_code == 2
    assert refused["error_code"] == "approval_action_not_allowed"
    assert decided_code == 0, decided
    assert decided["result"]["job_id"] == CHILD_JOB_ID
    [event] = decided["events"]
    assert event["event_type"] == "notification.interaction_received"
    assert event["job_id"] == CHILD_JOB_ID
    assert event["payload"]["job_id"] == CHILD_JOB_ID
    assert event["payload"]["approval_id"] == "child-gate-1"
    assert event["payload"]["action"] == "abort"
    assert PARENT_JOB_ID not in json.dumps(decided)


def test_approval_with_reject_offers_no_abort_and_records_the_job() -> None:
    event = {
        "event_type": "approval.requested",
        "project_id": PROJECT_ID,
        "job_id": PARENT_JOB_ID,
        "payload": {
            "approval_id": "gate-9",
            "allowed_actions": ["approve", "reject"],
            "group_id": "review_changes",
            "group_label": "Review changes",
            "step_id": "human_approval",
            "step_execution_id": "5d1c6a3e-2f4b-4c8e-9a1d-7e0f3b2c0009",
        },
    }

    prepared, code = _invoke(_deliver_request(event, delivery_phase="prepare"))

    assert code == 0, prepared
    tokens = prepared["host_mutations"][0]["payload"]["ingress_config"]["pending_hitl_tokens"]
    record = next(iter(tokens.values()))
    assert record["job_id"] == PARENT_JOB_ID
    assert record["allowed_actions"] == ["approve", "reject"]
    keyboard = prepared["prepared_delivery"]["reply_markup"]["inline_keyboard"][0]
    assert [button["text"] for button in keyboard] == ["Approve", "Reject"]

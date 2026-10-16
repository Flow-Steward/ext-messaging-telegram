from __future__ import annotations

import io
import json
import sys

import pytest
from subprocess_contract_helpers import _invoke, host_envelope


class TestTelegramDeliverNotification:
    @pytest.mark.parametrize("legacy_field", ["chat_id", "chat_id_override"])
    def test_delivery_rejects_legacy_raw_chat_routes(self, legacy_field):
        resp, code = _invoke(
            {
                "action": "deliver_notification",
                "event": {"event_type": "job.failed", "severity": "error"},
                "route": {"route_config": {legacy_field: "-100123"}},
                "provider": {"provider_secrets": {"bot_token": "fake-token"}},
            }
        )

        assert code == 2
        assert resp["ok"] is False
        assert resp["error_code"] == "channel_id_required"
        assert resp["errors"][0]["code"] == "channel_id_required"
        assert resp["errors"][0]["message"] == resp["error"]
        assert "fake-token" not in str(resp)

    def test_connection_secret_cannot_be_overridden_by_provider_config(self, monkeypatch):
        import main as telegram_main

        captured = {}
        request = {
            "action": "deliver_notification",
            "event": {"event_type": "job.failed"},
            "route": {"route_config": {}},
            "provider": {"provider_config": {"bot_token": "legacy-config-token"}},
            "action_payload": {
                "target": {
                    "connection": {
                        "config": {"bot_token": "legacy-connection-config-token"},
                        "secrets": {"bot_token": "project-token"},
                    }
                }
            },
        }

        def capture(_event, _route, provider, **_kwargs):
            captured.update(provider)
            return {"ok": True}

        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(host_envelope(request))))
        monkeypatch.setattr(telegram_main, "_deliver", capture)

        assert telegram_main.main() == 0
        assert captured["provider_secrets"] == {"bot_token": "project-token"}
        assert "bot_token" not in captured["provider_config"]

    def test_render_message_prefers_workflow_context_over_raw_job_id(self):
        import main as telegram_main

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setenv(
            "FS_PUBLIC_BASE_URL", "https://powder-skating-actors-merchandise.trycloudflare.com"
        )
        try:
            message = telegram_main._render_message(
                {
                    "event_type": "approval.requested",
                    "severity": "info",
                    "job_id": "6434ac0d-8323-4746-a5f4-3305d42abeb9",
                    "workflow_id": "weekly_report",
                    "payload": {
                        "workflow_label": "Weekly Report",
                        "group_id": "review",
                        "group_label": "Review",
                        "step_id": "human_approval",
                        "step_label": "Human Approval",
                        "message": "Please review the generated draft.",
                    },
                },
                {"route_config": {}},
            )
        finally:
            monkeypatch.undo()

        assert "[INFO] Approval requested" in message
        assert "Workflow: <a href=" in message
        assert ">Weekly Report</a>" in message
        assert "Group: Review" in message
        assert "Phase" not in message
        assert "For each:" not in message
        assert "Step: Human Approval" in message
        assert "Please review the generated draft." in message
        assert 'Job: <a href="' in message
        assert ">6434ac0d…</a>" in message

    @pytest.mark.parametrize(
        ("allowed_actions", "expected_labels", "expected_instruction"),
        [
            (["approve", "reject"], ["Approve", "Reject"], "approve or reject"),
            (["approve", "abort"], ["Approve", "Abort"], "approve or abort"),
        ],
    )
    def test_approval_message_and_buttons_follow_allowed_actions(
        self, monkeypatch, allowed_actions, expected_labels, expected_instruction
    ):
        import main as telegram_main
        import main_transport

        monkeypatch.setattr(main_transport, "_runtime_logger", lambda *_args: object())
        monkeypatch.setattr(
            main_transport,
            "_register_pending_hitl_token",
            lambda *_args, **_kwargs: "job-token",
        )
        event = {
            "event_type": "job.awaiting_approval",
            "project_id": "project-1",
            "job_id": "job-1",
            "payload": {
                "approval_id": "gate-1",
                "allowed_actions": allowed_actions,
            },
        }

        message = telegram_main._render_message(event, {"route_config": {}})
        reply_markup = telegram_main._approval_reply_markup(event)

        buttons = reply_markup["inline_keyboard"][0]
        assert [button["text"] for button in buttons] == expected_labels
        assert expected_instruction in message.lower()
        unexpected = "reject" if "abort" in expected_instruction else "abort"
        assert unexpected not in message.lower()

    def test_missing_allowed_actions_fail_closed_without_buttons(self, monkeypatch):
        import main as telegram_main
        import main_transport

        monkeypatch.setattr(main_transport, "_runtime_logger", lambda *_args: object())
        monkeypatch.setattr(
            main_transport,
            "_register_pending_hitl_token",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("a missing allow-list must not mint an actionable token")
            ),
        )
        event = {
            "event_type": "approval.requested",
            "project_id": "project-1",
            "job_id": "job-1",
            "payload": {"approval_id": "gate-1"},
        }

        message = telegram_main._render_message(event, {"route_config": {}})
        reply_markup = telegram_main._approval_reply_markup(event)

        assert reply_markup == {}
        assert "no approval actions are currently available" in message.lower()

    @pytest.mark.parametrize("raw_allowed_actions", [[], "approve", ["unknown"]])
    def test_explicit_invalid_allowed_actions_fail_closed_without_buttons(
        self, monkeypatch, raw_allowed_actions
    ):
        import main as telegram_main
        import main_transport

        monkeypatch.setattr(main_transport, "_runtime_logger", lambda *_args: object())
        monkeypatch.setattr(
            main_transport,
            "_register_pending_hitl_token",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("an invalid allow-list must not mint an actionable token")
            ),
        )
        event = {
            "event_type": "approval.requested",
            "project_id": "project-1",
            "job_id": "job-1",
            "payload": {
                "approval_id": "gate-1",
                "allowed_actions": raw_allowed_actions,
            },
        }

        message = telegram_main._render_message(event, {"route_config": {}})
        reply_markup = telegram_main._approval_reply_markup(event)

        assert reply_markup == {}
        assert "no approval actions are currently available" in message.lower()

    def test_render_message_distinguishes_similar_approvals_by_task_and_job(self):
        import main as telegram_main

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setenv("FS_PUBLIC_BASE_URL", "https://flow-steward.example")
        common = {
            "event_type": "approval.requested",
            "severity": "info",
            "project_id": "project-1",
            "workflow_id": "weekly_report",
            "payload": {
                "workflow_label": "Weekly Report",
                "group_id": "review",
                "group_label": "Review",
                "step_id": "human_approval",
                "step_label": "Human Approval",
                "message": "Please review the generated draft.",
            },
        }
        try:
            first = telegram_main._render_message(
                {
                    **common,
                    "job_id": "6434ac0d-8323-4746-a5f4-3305d42abeb9",
                    "payload": {
                        **common["payload"],
                        "task_summary": "Repair checkout retries",
                    },
                },
                {"route_config": {}},
            )
            second = telegram_main._render_message(
                {
                    **common,
                    "job_id": "759dec53-e2f5-4a6d-93f3-e3c9a99a8226",
                    "payload": {
                        **common["payload"],
                        "task_summary": "Repair invoice retries",
                    },
                },
                {"route_config": {}},
            )
        finally:
            monkeypatch.undo()

        assert first != second
        assert "Task: Repair checkout retries" in first
        assert "Task: Repair invoice retries" in second
        assert 'Job: <a href="https://flow-steward.example/jobs/6434ac0d-' in first
        assert '">6434ac0d…</a>' in first
        assert 'Job: <a href="https://flow-steward.example/jobs/759dec53-' in second
        assert '">759dec53…</a>' in second

    def test_render_message_uses_job_task_summary_as_fallback(self, monkeypatch):
        import main as telegram_main
        import main_state

        monkeypatch.delenv("FS_PUBLIC_BASE_URL", raising=False)

        class Store:
            def get_job(self, _job_id):
                return {"task_summary": "Fallback task from persisted job"}

        monkeypatch.setattr(main_state, "_runtime_logger", lambda _context: Store())

        message = telegram_main._render_message(
            {
                "event_type": "approval.requested",
                "job_id": "job-42",
                "payload": {"message": "Please review."},
            },
            {"route_config": {}},
        )

        assert "Task: Fallback task from persisted job" in message
        assert "Job: job-42" in message

    @pytest.mark.parametrize(
        ("action", "expected"),
        [
            ("approve", ["Approval applied", "job will continue"]),
            ("abort", ["Abort applied", "job was stopped"]),
        ],
    )
    def test_render_authoritative_approval_resolution(self, action, expected):
        import main as telegram_main

        message = telegram_main._render_message(
            {
                "event_type": "approval.resolved",
                "job_id": "job-42",
                "payload": {
                    "action": action,
                    "applied": True,
                    "accepted": True,
                    "outcome_code": "applied",
                },
            },
            {"route_config": {}},
        )

        for phrase in expected:
            assert phrase in message
        assert "older approval buttons no longer apply" in message
        assert "Job: job-42" in message

    def test_render_authoritative_rejection_preserves_exact_comment(self):
        import main as telegram_main

        message = telegram_main._render_message(
            {
                "event_type": "approval.resolved",
                "payload": {
                    "action": "reject",
                    "applied": True,
                    "comment": "Fix retry <limit> & preserve IDs",
                },
            },
            {"route_config": {}},
        )

        assert "Rejection applied" in message
        assert "Reason: Fix retry &lt;limit&gt; &amp; preserve IDs" in message
        assert "sent back for rework" in message
        assert "continue through the rework path" in message
        assert "Retry when ready" not in message

    @pytest.mark.parametrize(
        ("outcome_code", "expected_reason"),
        [
            ("stale", "This approval is no longer current."),
            ("already_handled", "This approval was already handled."),
            ("not_allowed", "This action is no longer allowed for this approval."),
        ],
    )
    def test_render_non_applied_resolution_never_claims_success(
        self, outcome_code, expected_reason
    ):
        import main as telegram_main

        message = telegram_main._render_message(
            {
                "event_type": "approval.resolved",
                "payload": {
                    "action": "approve",
                    "applied": False,
                    "accepted": False,
                    "outcome_code": outcome_code,
                    "reason": "stale_approval_gate",
                },
            },
            {"route_config": {}},
        )

        assert "Decision was not applied" in message
        assert expected_reason in message
        assert "stale_approval_gate" not in message
        assert "Refresh the approval state" in message
        assert "Approval applied" not in message

    def test_resolution_prepare_terminalizes_pending_decision_token(self, monkeypatch):
        import main_delivery

        consumed = []
        monkeypatch.setattr(main_delivery, "_runtime_logger", lambda *_args: object())
        monkeypatch.setattr(
            main_delivery,
            "_consume_pending_hitl_approval",
            lambda logger, **kwargs: consumed.append((logger, kwargs)) or {"approval_id": "gate-1"},
        )

        result = main_delivery._deliver(
            {
                "event_type": "approval.resolved",
                "project_id": "project-1",
                "job_id": "job-1",
                "payload": {
                    "action": "approve",
                    "applied": True,
                    "reply_target": {
                        "external_conversation_ref": "-100123",
                        "external_message_ref": "44",
                    },
                    "approval_id": "gate-1",
                },
            },
            {
                "route_config": {
                    "channel_id": "approvals",
                    "required_capability": "human_decision",
                }
            },
            {
                "project_id": "project-1",
                "provider_config": {
                    "channels": [
                        {
                            "channel_id": "approvals",
                            "chat_id": "-100123",
                            "enabled": True,
                            "capabilities": {"human_decision": True},
                        }
                    ]
                },
                "ingress_config": {"pending_hitl_tokens": {"job-token": {}}},
            },
            delivery_phase="prepare",
        )

        assert result["ok"] is True
        assert len(consumed) == 1
        assert consumed[0][1]["approval_id"] == "gate-1"
        assert consumed[0][1]["job_id"] == "job-1"
        assert consumed[0][1]["chat_id"] == "-100123"

    def test_resolution_delivery_clears_buttons_before_final_message(self, monkeypatch):
        import main_delivery

        calls = []
        monkeypatch.setattr(
            main_delivery,
            "_edit_message_reply_markup",
            lambda token, chat_id, message_id: (
                calls.append(("edit", token, chat_id, message_id)) or {"ok": True}
            ),
        )
        monkeypatch.setattr(
            main_delivery,
            "_send_message",
            lambda token, chat_id, message, **kwargs: (
                calls.append(("send", token, chat_id, message, kwargs.get("reply_to_message_id")))
                or {"ok": True, "message_id": 55}
            ),
        )

        result = main_delivery._deliver(
            {
                "event_type": "approval.resolved",
                "payload": {
                    "action": "approve",
                    "applied": True,
                    "reply_target": {
                        "external_conversation_ref": "-100123",
                        "external_message_ref": "44",
                    },
                },
            },
            {
                "route_config": {
                    "channel_id": "approvals",
                    "required_capability": "human_decision",
                }
            },
            {
                "provider_secrets": {"bot_token": "fake-token"},
                "provider_config": {
                    "channels": [
                        {
                            "channel_id": "approvals",
                            "chat_id": "-100123",
                            "enabled": True,
                            "capabilities": {"human_decision": True},
                        }
                    ]
                },
            },
        )

        assert result["delivered"] is True
        assert [call[0] for call in calls] == ["edit", "send"]
        assert calls[0][2:] == ("-100123", 44)
        assert calls[1][4] == 44

    def test_resolution_delivery_sends_reliable_final_when_button_edit_fails(self, monkeypatch):
        import main_delivery

        sent = []
        monkeypatch.setattr(
            main_delivery,
            "_edit_message_reply_markup",
            lambda *_args, **_kwargs: {"ok": False, "error": "message_not_modified"},
        )
        monkeypatch.setattr(
            main_delivery,
            "_send_message",
            lambda _token, _chat_id, message, **_kwargs: (
                sent.append(message) or {"ok": True, "message_id": 56}
            ),
        )

        result = main_delivery._deliver(
            {
                "event_type": "approval.resolved",
                "payload": {
                    "action": "abort",
                    "applied": True,
                    "reply_target": {
                        "external_conversation_ref": "-100123",
                        "external_message_ref": "44",
                    },
                },
            },
            {
                "route_config": {
                    "channel_id": "approvals",
                    "required_capability": "human_decision",
                }
            },
            {
                "provider_secrets": {"bot_token": "fake-token"},
                "provider_config": {
                    "channels": [
                        {
                            "channel_id": "approvals",
                            "chat_id": "-100123",
                            "enabled": True,
                            "capabilities": {"human_decision": True},
                        }
                    ]
                },
            },
        )

        assert result["delivered"] is True
        assert len(sent) == 1
        assert "older approval buttons no longer apply" in sent[0]

    @pytest.mark.parametrize(
        ("scope", "expected", "absent"),
        [
            # Workflow-level event: all four scope keys null.
            (
                {"group_id": None, "for_each_id": None, "step_id": None},
                [],
                ["Group:", "For each:", "Step:"],
            ),
            # Group-level event names only its container.
            (
                {"group_id": "review_changes", "group_label": "Review changes"},
                ["Group: Review changes"],
                ["For each:", "Step:"],
            ),
            # For each-level event names only its container.
            (
                {"for_each_id": "each_order", "for_each_label": "Each order"},
                ["For each: Each order"],
                ["Group:", "Step:"],
            ),
            # Root step: no owner, so no Group line and no root label.
            (
                {"group_id": None, "for_each_id": None, "step_id": "notify_team"},
                ["Step: notify_team"],
                ["Group:", "For each:", "Root"],
            ),
            # Step directly in a Group.
            (
                {
                    "group_id": "review_changes",
                    "group_label": "Review changes",
                    "step_id": "human_approval",
                    "step_label": "Human Approval",
                },
                ["Group: Review changes", "Step: Human Approval"],
                ["For each:"],
            ),
            # Step in a For each: never a Group line.
            (
                {
                    "group_id": None,
                    "for_each_id": "each_order",
                    "for_each_label": "Each order",
                    "step_id": "notify_team",
                    "step_label": "Notify team",
                },
                ["For each: Each order", "Step: Notify team"],
                ["Group:"],
            ),
            # Without a label the id is shown.
            (
                {"group_id": "review_changes", "step_id": "human_approval"},
                ["Group: review_changes", "Step: human_approval"],
                ["For each:"],
            ),
        ],
    )
    def test_render_message_shows_only_the_final_scope_levels(self, scope, expected, absent):
        import main as telegram_main

        message = telegram_main._render_message(
            {
                "event_type": "workflow.automation.notify",
                "severity": "info",
                "payload": {
                    "workflow_label": "Weekly Report",
                    "message": "Scoped message.",
                    # A leftover phase or a label without its id is not a scope level.
                    "phase_label": "Review",
                    **({} if scope.get("group_id") else {"group_label": "Stale group"}),
                    **scope,
                },
            },
            {"route_config": {}},
        )

        assert "Workflow: Weekly Report" in message
        assert "Phase" not in message
        for line in expected:
            assert line in message
        for line in absent:
            assert line not in message

    def test_render_message_reads_scope_from_the_event_root(self):
        import main as telegram_main

        message = telegram_main._render_message(
            {
                "event_type": "job.step_failed",
                "severity": "error",
                "group_id": "review_changes",
                "step_id": "human_approval",
                "payload": {"group_label": "Review changes", "message": "Failed."},
            },
            {"route_config": {}},
        )

        assert "Group: Review changes" in message
        assert "Step: human_approval" in message

    def test_render_message_makes_workflow_label_clickable_when_public_base_url_exists(self):
        import main as telegram_main

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setenv(
            "FS_PUBLIC_BASE_URL", "https://powder-skating-actors-merchandise.trycloudflare.com"
        )
        try:
            message = telegram_main._render_message(
                {
                    "event_type": "approval.requested",
                    "severity": "info",
                    "project_id": "58eeaeb3-a9c1-4c51-b292-14eb86899d86",
                    "workflow_id": "weekly_report",
                    "payload": {
                        "workflow_label": "Weekly Report",
                        "message": "Please review.",
                    },
                },
                {"route_config": {}},
            )
        finally:
            monkeypatch.undo()

        assert (
            'Workflow: <a href="https://powder-skating-actors-merchandise.trycloudflare.com/registry/workflows/weekly_report/builder?project_id=58eeaeb3-a9c1-4c51-b292-14eb86899d86">Weekly Report</a>'
            in message
        )

    def test_render_message_adds_job_review_link_when_job_exists(self):
        import main as telegram_main

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setenv(
            "FS_PUBLIC_BASE_URL", "https://powder-skating-actors-merchandise.trycloudflare.com"
        )
        try:
            message = telegram_main._render_message(
                {
                    "event_type": "approval.requested",
                    "severity": "info",
                    "project_id": "58eeaeb3-a9c1-4c51-b292-14eb86899d86",
                    "job_id": "6434ac0d-8323-4746-a5f4-3305d42abeb9",
                    "workflow_id": "weekly_report",
                    "payload": {
                        "workflow_label": "Weekly Report",
                        "message": "Please review.",
                    },
                },
                {"route_config": {}},
            )
        finally:
            monkeypatch.undo()

        assert (
            'Job: <a href="https://powder-skating-actors-merchandise.trycloudflare.com/jobs/6434ac0d-8323-4746-a5f4-3305d42abeb9?project_id=58eeaeb3-a9c1-4c51-b292-14eb86899d86">6434ac0d…</a>'
            in message
        )

    def test_deliver_notification_uses_named_channel(self):
        resp, code = _invoke(
            {
                "action": "deliver_notification",
                "event": {
                    "event_type": "job.failed",
                    "severity": "error",
                    "summary": "Build failed",
                },
                "route": {"severity": "error", "route_config": {"channel_id": "ops"}},
                "provider": {
                    "provider_secrets": {"bot_token": "fake-token"},
                    "provider_config": {
                        "channels": [
                            {
                                "channel_id": "ops",
                                "display_name": "Ops",
                                "chat_id": "-100123",
                                "enabled": True,
                                "capabilities": {"notify": True, "human_decision": True},
                            }
                        ],
                    },
                },
            }
        )
        assert code == 0
        assert resp["ok"] is True
        assert "provider_message_id" in resp

    def test_delivery_propagates_definitive_pre_send_status(self):
        resp, code = _invoke(
            {
                "action": "deliver_notification",
                "event": {"event_type": "job.failed", "severity": "error"},
                "route": {"route_config": {"channel_id": "ops"}},
                "provider": {
                    "provider_config": {
                        "channels": [
                            {
                                "channel_id": "ops",
                                "chat_id": "-100123",
                                "enabled": True,
                                "capabilities": {"notify": True},
                            }
                        ]
                    }
                },
            }
        )

        assert code == 2
        assert resp["ok"] is False
        assert resp["external_effect_status"] == "failed"
        assert resp["definitely_no_external_effect"] is True

    def test_hitl_delivery_without_persistence_fails_before_telegram_send(self):
        resp, code = _invoke(
            {
                "action": "deliver_notification",
                "event": {
                    "event_type": "approval.requested",
                    "severity": "warning",
                    "job_id": 42,
                    "workflow_id": "weekly_report",
                    "payload": {"message": "Please approve step"},
                },
                "route": {
                    "severity": "warning",
                    "route_config": {
                        "message_suffix": "— via Flow Steward",
                        "channel_id": "approvals",
                    },
                },
                "provider": {
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
        assert code == 2
        assert resp["ok"] is False
        assert resp["delivered"] is False
        assert resp["error_code"] == "prepared_delivery_required"
        assert resp["errors"][0] == {
            "code": "prepared_delivery_required",
            "message": resp["error"],
        }
        assert resp.get("host_mutations") in (None, [])

    def test_hitl_delivery_prepares_mutation_then_executes_prepared_payload(self):
        project_id = "58eeaeb3-a9c1-4c51-b292-14eb86899d86"
        event = {
            "event_type": "job.awaiting_approval",
            "project_id": project_id,
            "job_id": "6434ac0d-8323-4746-a5f4-3305d42abeb9",
            "payload": {
                "approval_id": "gate-2",
                "allowed_actions": ["approve", "reject"],
                "summary": "Review production rollout",
            },
        }
        route = {"route_config": {"channel_id": "approvals"}}
        connection = {
            "connection_id": "conn-hitl",
            "project_id": project_id,
            "extension_id": "flowsteward.telegram",
            "extension_install_id": "install-hitl",
            "connection_type": "telegram_bot",
            "enabled": True,
            "config": {
                "channels": [
                    {
                        "channel_id": "approvals",
                        "chat_id": "-100124",
                        "enabled": True,
                        "capabilities": {"human_decision": True},
                    }
                ]
            },
            "ingress_config": {},
        }
        provider = {"project_id": project_id}
        prepared, prepare_code = _invoke(
            {
                "action": "deliver_notification",
                "delivery_phase": "prepare",
                "action_payload": {
                    "action_id": "deliver_notification",
                    "target": {"connection": connection},
                },
                "event": event,
                "route": route,
                "provider": provider,
            }
        )

        assert prepare_code == 0
        assert prepared["ok"] is True
        assert prepared["delivered"] is False
        assert "provider_message_id" not in prepared
        mutations = prepared.get("host_mutations") or []
        assert [row["kind"] for row in mutations] == ["project_connection_upsert"]
        pending = mutations[0]["payload"]["ingress_config"]["pending_hitl_tokens"]
        assert len(pending) == 1
        assert next(iter(pending.values()))["approval_id"] == "gate-2"

        executed, execute_code = _invoke(
            {
                "action": "deliver_notification",
                "delivery_phase": "execute",
                "prepared_delivery": prepared["prepared_delivery"],
                "action_payload": {
                    "action_id": "deliver_notification",
                    "target": {
                        "connection": {
                            **connection,
                            "secrets": {"bot_token": "fake-token"},
                        }
                    },
                },
                "event": event,
                "route": route,
                "provider": provider,
            }
        )

        assert execute_code == 0
        assert executed["ok"] is True
        assert executed["delivered"] is True
        assert executed.get("host_mutations") in (None, [])

    def test_standard_action_uses_target_connection(self):
        resp, code = _invoke(
            {
                "mode": "action",
                "action": {
                    "action_id": "deliver_notification",
                    "context": {"project_id": "project-action-envelope"},
                    "target": {
                        "connection": {
                            "extension_id": "flowsteward.telegram",
                            "config": {
                                "channels": [
                                    {
                                        "channel_id": "ops",
                                        "chat_id": "-100987",
                                        "enabled": True,
                                        "capabilities": {
                                            "notify": True,
                                            "human_decision": True,
                                        },
                                    }
                                ]
                            },
                            "secrets": {"bot_token": "test-action-envelope-secret"},
                        }
                    },
                    "input": {
                        "event": {"event_type": "job.failed", "severity": "error"},
                        "route": {
                            "severity": "error",
                            "route_config": {"channel_id": "ops"},
                        },
                    },
                },
            }
        )

        assert code == 0
        assert resp["ok"] is True
        assert resp["result"].get("error") != "unknown_channel_id"
        assert "test-action-envelope-secret" not in str(resp)

    def test_unsupported_request(self):
        resp, code = _invoke({"action": "unknown_action"})
        assert code == 2
        assert resp["ok"] is False

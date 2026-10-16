from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from subprocess_contract_helpers import _invoke


def _fresh_created_at() -> str:
    return datetime.now(UTC).isoformat()


def _provider_with_human_decision_channel(chat_id: str = "-100123") -> dict[str, Any]:
    return {
        "provider_config": {
            "channels": [
                {
                    "channel_id": "ops",
                    "chat_id": chat_id,
                    "enabled": True,
                    "capabilities": {"notify": True, "human_decision": True},
                }
            ]
        }
    }


def _provider_with_pending_token(
    *,
    token: str = "abc123token",
    project_id: str = "11",
    job_id: str = "77",
    chat_id: str = "-100123",
    allowed_actions: list[str] | None = None,
) -> dict[str, Any]:
    provider = _provider_with_human_decision_channel(chat_id)
    explicit_allowed_actions = (
        list(allowed_actions) if allowed_actions is not None else ["approve", "reject"]
    )
    provider.update(
        {
            "project_id": project_id,
            "ingress_config": {
                "pending_hitl_tokens": {
                    token: {
                        "project_id": project_id,
                        "job_id": job_id,
                        "approval_id": "gate-1",
                        "allowed_actions": explicit_allowed_actions,
                        "channel_id": "ops",
                        "chat_id": chat_id,
                        "created_at": _fresh_created_at(),
                    }
                }
            },
        }
    )
    return provider


class TestTelegramIngressEvent:
    def test_ingress_event_fails_closed_without_secret_or_authenticated_callback(self):
        from main_ingress import _ingress_event_response

        resp = _ingress_event_response(
            {
                "provider": _provider_with_human_decision_channel(),
                "payload": {
                    "message": {
                        "message_id": 101,
                        "chat": {"id": -100123},
                        "text": "/approve abc123token Looks good",
                        "from": {"id": 9, "username": "reviewer"},
                    }
                },
            }
        )

        assert resp["ok"] is False
        assert resp["error"] == "unauthorized_telegram_ingress"

    def test_ingress_event_rejects_unauthenticated_callback_context(self):
        resp, code = _invoke(
            {
                "action": "ingress_event",
                "request": {
                    "callback": {
                        "extension_id": "flowsteward.telegram",
                        "callback_id": "updates",
                        "project_id": "11",
                    },
                    "payload": {
                        "callback_query": {
                            "id": "cb-unauth",
                            "data": "tgx:a:11:77",
                            "from": {"id": 10, "username": "alice"},
                        }
                    },
                },
            }
        )
        assert code == 2
        assert resp["ok"] is False
        assert resp["error_code"] == "unauthorized_telegram_ingress"

    def test_ingress_event_rejects_legacy_direct_job_callback_context(self):
        resp, code = _invoke(
            {
                "action": "ingress_event",
                "request": {
                    "callback": {
                        "extension_id": "flowsteward.telegram",
                        "callback_id": "updates",
                        "project_id": "11",
                        "authenticated": True,
                    },
                    "provider": _provider_with_pending_token(),
                    "payload": {
                        "callback_query": {
                            "id": "cb-auth",
                            "data": "tgx:a:11:77",
                            "from": {"id": 10, "username": "alice"},
                            "message": {
                                "message_id": 102,
                                "chat": {"id": -100123},
                                "text": "Please review",
                            },
                        }
                    },
                },
            }
        )
        assert code == 0
        assert resp["ok"] is True
        assert resp["result"]["ignored"] is True
        assert resp["result"]["reason"] == "unsupported_callback_data"

    def test_ingress_event_approve_command_raw_uuid_rejected(self):
        # A typed /approve <job_uuid> must NOT be accepted on the strength of a raw
        # job UUID alone — possession of the per-job pending HITL token is required.
        resp, code = _invoke(
            {
                "action": "ingress_event",
                "request": {
                    "callback": {"authenticated": True},
                    "provider": _provider_with_human_decision_channel("-100456"),
                    "payload": {
                        "message": {
                            "message_id": 101,
                            "chat": {"id": -100456},
                            "text": "/approve 6434ac0d-8323-4746-a5f4-3305d42abeb9 Looks good",
                            "from": {"id": 9, "username": "reviewer"},
                        }
                    },
                },
            }
        )
        assert code == 2
        assert resp["ok"] is False
        assert resp["error_code"] == "invalid_reject_comment_context"
        assert resp.get("events") == []

    def test_ingress_event_approve_command_with_pending_token(self):
        # Positive path: a typed /approve <token> resolves the pending HITL token to
        # the bound job and approves it.
        import main as telegram_main

        class _FakeLogger:
            def get_project_connection(
                self, project_id: str, extension_id: str, enabled_only: bool = False
            ) -> dict:
                return {
                    "project_id": project_id,
                    "extension_id": extension_id,
                    "connection_kind": "extension",
                    "enabled": True,
                    "connection_config": _provider_with_human_decision_channel()["provider_config"],
                    "ingress_config": {
                        "pending_hitl_tokens": {
                            "abc123token": {
                                "project_id": "11",
                                "job_id": "6434ac0d-8323-4746-a5f4-3305d42abeb9",
                                "approval_id": "gate-1",
                                "allowed_actions": ["approve", "reject"],
                                "created_at": _fresh_created_at(),
                            }
                        }
                    },
                }

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            telegram_main, "_runtime_logger", lambda *_args, **_kwargs: _FakeLogger()
        )
        try:
            resp, code = telegram_main._handle_action(
                {
                    "action_id": "ingress_event",
                    "input": {
                        "callback": {"authenticated": True, "project_id": "11"},
                        "request": {
                            "provider": _provider_with_human_decision_channel(),
                            "query": {"project_id": "11"},
                            "payload": {
                                "message": {
                                    "message_id": 101,
                                    "chat": {"id": -100123},
                                    "text": "/approve abc123token Looks good",
                                    "from": {"id": 9, "username": "reviewer"},
                                }
                            },
                        },
                    },
                }
            )
        finally:
            monkeypatch.undo()
        assert code == 0
        assert resp["ok"] is True
        assert resp["result"]["job_id"] == "6434ac0d-8323-4746-a5f4-3305d42abeb9"
        assert resp["result"]["approved"] is True
        assert "abc123token" not in json.dumps(resp["events"])
        assert resp["events"][0]["payload"]["reply_target"] == {
            "provider_key": "flowsteward.telegram",
            "channel_id": "",
            "external_conversation_ref": "-100123",
            "external_message_ref": "101",
        }

    def test_ingress_event_reject_callback(self):
        resp, code = _invoke(
            {
                "action": "ingress_event",
                "request": {
                    "callback": {"authenticated": True},
                    "provider": _provider_with_pending_token(),
                    "payload": {
                        "callback_query": {
                            "id": "cb-2",
                            "data": "tgx:r:abc123token",
                            "from": {"id": 10, "username": "alice"},
                            "message": {
                                "message_id": 102,
                                "chat": {"id": -100123},
                                "text": "Please review",
                            },
                        }
                    },
                },
            }
        )
        assert code == 0
        assert resp["ok"] is True
        assert resp["result"]["project_id"] == "11"
        assert resp["result"]["job_id"] == "77"
        assert resp["result"]["approved"] is False
        assert resp["result"]["pending_reason"] is False
        assert resp["result"]["prompt_sent"] is False
        assert resp["result"]["prompt_status"] in {"failed", "timeout_unknown"}
        assert resp.get("events") == []

    def test_abort_callback_is_parsed_and_emitted_without_a_reject_prompt(self):
        resp, code = _invoke(
            {
                "action": "ingress_event",
                "request": {
                    "callback": {"authenticated": True},
                    "provider": _provider_with_pending_token(allowed_actions=["approve", "abort"]),
                    "payload": {
                        "callback_query": {
                            "id": "cb-abort",
                            "data": "tgx:b:abc123token",
                            "from": {"id": 10, "username": "alice"},
                            "message": {
                                "message_id": 102,
                                "chat": {"id": -100123},
                                "text": "Please review",
                            },
                        }
                    },
                },
            }
        )

        assert code == 0
        assert resp["ok"] is True
        assert resp["result"]["action"] == "abort"
        assert resp["result"]["approved"] is False
        assert resp["events"][0]["payload"]["action"] == "abort"
        assert resp["events"][0]["payload"]["approval_id"] == "gate-1"
        assert resp["events"][0]["payload"]["dedupe_key"] == "cb-abort"
        assert resp["events"][0]["payload"]["reply_target"] == {
            "provider_key": "flowsteward.telegram",
            "channel_id": "ops",
            "external_conversation_ref": "-100123",
            "external_message_ref": "102",
        }

    def test_reject_prompt_flow_replies_to_original_approval_message(self, monkeypatch):
        import main_ingress
        import main_state

        provider = _provider_with_pending_token()
        mutable_state = dict(provider["ingress_config"])
        store = main_state.RuntimePayloadStore(
            {
                "connection": {
                    "project_id": "11",
                    "extension_id": "flowsteward.telegram",
                    "connection_id": "conn-1",
                    "connection_type": "telegram_bot",
                    "enabled": True,
                    "connection_config": provider["provider_config"],
                    "ingress_config": mutable_state,
                }
            }
        )
        monkeypatch.setattr(main_ingress, "_runtime_logger", lambda *_args: store)
        monkeypatch.setattr(main_ingress, "_answer_callback_query", lambda *_a, **_k: True)
        monkeypatch.setattr(
            main_ingress,
            "_send_reject_reason_prompt",
            lambda *_a, **_k: {"ok": True},
        )

        prompted = main_ingress._ingress_event_response(
            {
                "callback": {"authenticated": True, "project_id": "11"},
                "provider": provider,
                "payload": {
                    "callback_query": {
                        "id": "cb-reject-original",
                        "data": "tgx:r:abc123token",
                        "from": {"id": 10, "username": "alice"},
                        "message": {
                            "message_id": 102,
                            "chat": {"id": -100123},
                            "text": "Please review",
                        },
                    }
                },
            },
            mutable_inbound_state=mutable_state,
        )
        provider["ingress_config"] = mutable_state
        resolved = main_ingress._ingress_event_response(
            {
                "callback": {"authenticated": True, "project_id": "11"},
                "provider": provider,
                "payload": {
                    "message": {
                        "message_id": 103,
                        "chat": {"id": -100123},
                        "text": "Needs more detail",
                        "from": {"id": 10, "username": "alice"},
                        "reply_to_message": {
                            "from": {"id": 999, "is_bot": True},
                            "text": (
                                "Flow Steward reject context: project_id=11 job_ref=abc123token"
                            ),
                        },
                    }
                },
            },
            mutable_inbound_state=mutable_state,
        )

        assert prompted["result"]["pending_reason"] is True
        interaction = next(
            event
            for event in resolved["events"]
            if event["event_type"] == "notification.interaction_received"
        )
        assert interaction["payload"]["reply_target"]["external_message_ref"] == "102"

    def test_transient_core_failure_can_be_retried_with_same_pending_token(self):
        import main_ingress

        provider = _provider_with_pending_token(allowed_actions=["approve"])
        mutable_state = dict(provider["ingress_config"])
        request = {
            "callback": {"authenticated": True},
            "provider": provider,
            "payload": {
                "callback_query": {
                    "id": "cb-retryable",
                    "data": "tgx:a:abc123token",
                    "from": {"id": 10, "username": "alice"},
                    "message": {
                        "message_id": 102,
                        "chat": {"id": -100123},
                        "text": "Please review",
                    },
                }
            },
        }

        first = main_ingress._ingress_event_response(request, mutable_inbound_state=mutable_state)
        second = main_ingress._ingress_event_response(request, mutable_inbound_state=mutable_state)

        assert first["events"][0]["payload"]["dedupe_key"] == "cb-retryable"
        assert second["events"][0]["payload"]["dedupe_key"] == "cb-retryable"
        assert "abc123token" in mutable_state["pending_hitl_tokens"]

    def test_authoritative_resolution_consumes_matching_approval_token_once(self):
        import main_state

        class _FakeLogger:
            def __init__(self) -> None:
                self.tokens: list[str] = []

            def consume_project_connection_ingress_token(self, **kwargs) -> bool:
                self.tokens.append(kwargs["token"])
                return True

        logger = _FakeLogger()
        state = _provider_with_pending_token()["ingress_config"]

        first = main_state._consume_pending_hitl_approval(
            logger,
            project_id="11",
            approval_id="gate-1",
            job_id="77",
            inbound_state=state,
            chat_id="-100123",
            channel_id="ops",
        )
        replay = main_state._consume_pending_hitl_approval(
            logger,
            project_id="11",
            approval_id="gate-1",
            job_id="77",
            inbound_state=state,
            chat_id="-100123",
            channel_id="ops",
        )

        assert first["job_id"] == "77"
        assert replay == {}
        assert logger.tokens == ["abc123token"]

    def test_forbidden_callback_action_returns_visible_feedback_without_consuming_token(
        self, monkeypatch
    ):
        import main as telegram_main
        import main_ingress

        answers = []
        monkeypatch.setattr(
            main_ingress,
            "_answer_callback_query",
            lambda *_args, **kwargs: answers.append(kwargs) or True,
        )
        monkeypatch.setattr(
            main_ingress,
            "_send_reject_reason_prompt",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("a forbidden Reject must not open a reason prompt")
            ),
        )
        request = {
            "callback": {"authenticated": True},
            "provider": _provider_with_pending_token(allowed_actions=["approve", "abort"]),
            "payload": {
                "callback_query": {
                    "id": "cb-forbidden-reject",
                    "data": "tgx:r:abc123token",
                    "from": {"id": 10, "username": "alice"},
                    "message": {
                        "message_id": 102,
                        "chat": {"id": -100123},
                        "text": "Please review",
                    },
                }
            },
        }

        resp = telegram_main._ingress_event_response(request)

        assert resp["ok"] is False
        assert resp["error"] == "approval_action_not_allowed"
        assert resp["events"] == []
        assert answers[-1]["show_alert"] is True
        assert "no longer available" in answers[-1]["text"].lower()

    @pytest.mark.parametrize(
        ("message", "allowed_actions"),
        [
            ("/reject abc123token Needs changes", ["approve", "abort"]),
            ("/abort abc123token", ["approve", "reject"]),
        ],
    )
    def test_forbidden_text_command_does_not_consume_the_pending_token(
        self, message, allowed_actions
    ):
        import main as telegram_main

        request = {
            "callback": {"authenticated": True, "project_id": "11"},
            "provider": _provider_with_pending_token(allowed_actions=allowed_actions),
            "payload": {
                "message": {
                    "message_id": 103,
                    "chat": {"id": -100123},
                    "text": message,
                    "from": {"id": 10, "username": "alice"},
                }
            },
        }

        resp = telegram_main._ingress_event_response(request)

        assert resp["ok"] is False
        assert resp["error"] == "approval_action_not_allowed"
        assert resp["events"] == []

    def test_forbidden_reject_prompt_reply_does_not_consume_the_pending_token(self):
        import main as telegram_main

        request = {
            "callback": {"authenticated": True, "project_id": "11"},
            "provider": _provider_with_pending_token(allowed_actions=["approve", "abort"]),
            "payload": {
                "message": {
                    "message_id": 103,
                    "chat": {"id": -100123},
                    "text": "Needs more detail",
                    "from": {"id": 10, "username": "alice"},
                    "reply_to_message": {
                        "from": {"id": 999, "is_bot": True},
                        "text": ("Flow Steward reject context: project_id=11 job_ref=abc123token"),
                    },
                }
            },
        }

        resp = telegram_main._ingress_event_response(request)

        assert resp["ok"] is False
        assert resp["error"] == "approval_action_not_allowed"
        assert resp["events"] == []

    @pytest.mark.parametrize("malformed_actions", [None, [], ["unknown"]])
    def test_missing_or_malformed_allow_list_does_not_enable_callback_actions(
        self, malformed_actions
    ):
        import main as telegram_main

        provider = _provider_with_pending_token(
            allowed_actions=malformed_actions if malformed_actions is not None else []
        )
        if malformed_actions is None:
            provider["ingress_config"]["pending_hitl_tokens"]["abc123token"].pop("allowed_actions")
        request = {
            "callback": {"authenticated": True, "project_id": "11"},
            "provider": provider,
            "payload": {
                "callback_query": {
                    "id": "cb-malformed-actions",
                    "data": "tgx:a:abc123token",
                    "from": {"id": 10, "username": "alice"},
                    "message": {
                        "message_id": 102,
                        "chat": {"id": -100123},
                        "text": "Please review",
                    },
                }
            },
        }

        resp = telegram_main._ingress_event_response(request)

        assert resp["ok"] is False
        assert resp["error"] == "approval_action_not_allowed"
        assert resp["events"] == []

    def test_ingress_event_reject_callback_sends_without_pre_prompt_mutation(self):
        import main as telegram_main

        class _FakeLogger:
            def __init__(self) -> None:
                self.saved_payload: dict[str, Any] | None = None

            def get_project_connection(
                self, project_id: str, extension_id: str, enabled_only: bool = False
            ) -> dict:
                return {
                    "project_id": project_id,
                    "extension_id": extension_id,
                    "connection_kind": "extension",
                    "enabled": True,
                    "connection_config": _provider_with_human_decision_channel()["provider_config"],
                    "ingress_config": _provider_with_pending_token()["ingress_config"],
                }

            def upsert_project_connection(self, **kwargs) -> None:
                raise AssertionError("reject prompt must not depend on a host mutation")

        fake_logger = _FakeLogger()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(telegram_main, "_runtime_logger", lambda *_args, **_kwargs: fake_logger)
        monkeypatch.setattr(telegram_main, "_answer_callback_query", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(
            telegram_main,
            "_send_reject_reason_prompt",
            lambda *_args, **_kwargs: {"ok": True, "external_effect_status": "succeeded"},
        )
        try:
            resp, code = telegram_main._handle_action(
                {
                    "action_id": "ingress_event",
                    "input": {
                        "callback": {"authenticated": True, "project_id": "11"},
                        "request": {
                            "provider": _provider_with_human_decision_channel(),
                            "payload": {
                                "callback_query": {
                                    "id": "cb-ctx",
                                    "data": "tgx:r:abc123token",
                                    "from": {"id": 10, "username": "alice"},
                                    "message": {
                                        "message_id": 102,
                                        "chat": {"id": -100123},
                                        "text": "Please review",
                                    },
                                }
                            },
                        },
                    },
                }
            )
        finally:
            monkeypatch.undo()

        assert code == 0
        assert resp["ok"] is True
        assert resp["result"]["pending_reason"] is True
        assert fake_logger.saved_payload is None

    @pytest.mark.parametrize("prompt_status", ["failed", "timeout_unknown"])
    def test_reject_callback_does_not_claim_pending_reason_when_prompt_fails(self, prompt_status):
        import main as telegram_main

        class _FakeLogger:
            def get_project_connection(
                self, project_id: str, extension_id: str, enabled_only: bool = False
            ) -> dict:
                return {
                    "project_id": project_id,
                    "extension_id": extension_id,
                    "enabled": True,
                    "connection_config": _provider_with_human_decision_channel()["provider_config"],
                    "ingress_config": _provider_with_pending_token()["ingress_config"],
                }

            def upsert_project_connection(self, **_kwargs) -> None:
                raise AssertionError("failed prompt must not mutate host state")

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(telegram_main, "_runtime_logger", lambda *_a, **_k: _FakeLogger())
        monkeypatch.setattr(telegram_main, "_answer_callback_query", lambda *_a, **_k: True)
        monkeypatch.setattr(
            telegram_main,
            "_send_reject_reason_prompt",
            lambda *_a, **_k: {
                "ok": False,
                "error": "prompt failed",
                "external_effect_status": prompt_status,
            },
        )
        try:
            resp, code = telegram_main._handle_action(
                {
                    "action_id": "ingress_event",
                    "input": {
                        "callback": {"authenticated": True, "project_id": "11"},
                        "request": {
                            "provider": _provider_with_pending_token(),
                            "payload": {
                                "callback_query": {
                                    "id": "cb-prompt-failure",
                                    "data": "tgx:r:abc123token",
                                    "from": {"id": 10, "username": "alice"},
                                    "message": {
                                        "message_id": 102,
                                        "chat": {"id": -100123},
                                    },
                                }
                            },
                        },
                    },
                }
            )
        finally:
            monkeypatch.undo()

        assert code == 0
        assert resp["ok"] is True
        assert resp["result"]["pending_reason"] is False
        assert resp["result"]["prompt_status"] == prompt_status
        assert resp.get("events") == []

    def test_send_reject_reason_prompt_includes_command_fallback(self):
        import main as telegram_main

        captured: list[dict[str, Any]] = []

        def fake_send_message(*args, **kwargs):
            captured.append({"args": args, "kwargs": kwargs})
            return {"ok": True, "message_id": 1}

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(telegram_main, "_send_message", fake_send_message)
        try:
            result = telegram_main._send_reject_reason_prompt(
                "fake-token",
                -100123,
                project_id="11",
                job_id="77",
                job_ref="77",
            )
        finally:
            monkeypatch.undo()

        assert result["ok"] is True
        assert captured
        assert "Fallback: /reject 77 <comment>" in captured[0]["args"][2]
        assert "Reply to this message with a rejection reason." in captured[0]["args"][2]

    def test_send_reject_reason_prompt_uses_context_label_when_present(self):
        import main as telegram_main

        captured: list[dict[str, Any]] = []

        def fake_send_message(*args, **kwargs):
            captured.append({"args": args, "kwargs": kwargs})
            return {"ok": True, "message_id": 1}

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(telegram_main, "_send_message", fake_send_message)
        try:
            result = telegram_main._send_reject_reason_prompt(
                "fake-token",
                -100123,
                project_id="11",
                job_id="77",
                job_ref="77",
                context_label="this request",
            )
        finally:
            monkeypatch.undo()

        assert result["ok"] is True
        assert captured
        assert (
            "Reply to this message with a rejection reason for this request."
            in captured[0]["args"][2]
        )

    def test_send_reject_reason_prompt_replies_to_original_message(self):
        import main as telegram_main

        captured: list[dict[str, Any]] = []

        def fake_send_message(*args, **kwargs):
            captured.append({"args": args, "kwargs": kwargs})
            return {"ok": True, "message_id": 1}

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(telegram_main, "_send_message", fake_send_message)
        try:
            result = telegram_main._send_reject_reason_prompt(
                "fake-token",
                -100123,
                project_id="11",
                job_id="77",
                job_ref="77",
                reply_to_message_id=102,
            )
        finally:
            monkeypatch.undo()

        assert result["ok"] is True
        assert captured
        assert captured[0]["kwargs"]["reply_to_message_id"] == 102

    def test_send_reject_reason_prompt_falls_back_to_plain_chat_message(self):
        import main as telegram_main

        captured: list[dict[str, Any]] = []

        def fake_send_message(*args, **kwargs):
            captured.append({"args": args, "kwargs": kwargs})
            if len(captured) == 1:
                return {
                    "ok": False,
                    "error": "reply_not_available",
                    "error_code": "telegram_reply_target_invalid",
                    "external_effect_status": "failed",
                    "definitely_no_external_effect": True,
                }
            return {"ok": True, "message_id": 2}

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(telegram_main, "_send_message", fake_send_message)
        try:
            result = telegram_main._send_reject_reason_prompt(
                "fake-token",
                -100123,
                project_id="11",
                job_id="77",
                job_ref="77",
                reply_to_message_id=102,
            )
        finally:
            monkeypatch.undo()

        assert result["ok"] is True
        assert len(captured) == 2
        assert captured[0]["kwargs"]["reply_to_message_id"] == 102
        assert "reply_to_message_id" not in captured[1]["kwargs"]

    def test_send_reject_reason_prompt_does_not_retry_ambiguous_failure(self):
        import main as telegram_main

        captured: list[dict[str, Any]] = []

        def fake_send_message(*args, **kwargs):
            captured.append({"args": args, "kwargs": kwargs})
            return {
                "ok": False,
                "error": "timed out",
                "error_code": "telegram_send_ambiguous",
                "external_effect_status": "timeout_unknown",
            }

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(telegram_main, "_send_message", fake_send_message)
        try:
            result = telegram_main._send_reject_reason_prompt(
                "real-token",
                -100123,
                project_id="11",
                job_id="77",
                job_ref="abc123token",
                reply_to_message_id=102,
            )
        finally:
            monkeypatch.undo()

        assert result["ok"] is False
        assert result["external_effect_status"] == "timeout_unknown"
        assert len(captured) == 1

    def test_approval_reply_markup_registers_uuid_job_token(self):
        import main as telegram_main

        class _FakeLogger:
            def __init__(self) -> None:
                self.saved_payload: dict[str, Any] | None = None

            def get_project_connection(
                self, project_id: str, extension_id: str, enabled_only: bool = False
            ) -> dict:
                return {
                    "project_id": project_id,
                    "extension_id": extension_id,
                    "connection_kind": "extension",
                    "enabled": True,
                    "connection_config": {},
                    "ingress_config": {},
                }

            def upsert_project_connection(self, **kwargs) -> None:
                self.saved_payload = dict(kwargs)

        fake_logger = _FakeLogger()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(telegram_main, "_runtime_logger", lambda *_args, **_kwargs: fake_logger)
        try:
            reply_markup = telegram_main._approval_reply_markup(
                {
                    "event_type": "approval.requested",
                    "project_id": "58eeaeb3-a9c1-4c51-b292-14eb86899d86",
                    "job_id": "6434ac0d-8323-4746-a5f4-3305d42abeb9",
                    "payload": {
                        "approval_id": "gate-1",
                        "allowed_actions": ["approve", "reject"],
                    },
                },
                {"project_id": "58eeaeb3-a9c1-4c51-b292-14eb86899d86", "provider_config": {}},
            )
        finally:
            monkeypatch.undo()

        approve_data = reply_markup["inline_keyboard"][0][0]["callback_data"]
        reject_data = reply_markup["inline_keyboard"][0][1]["callback_data"]
        assert approve_data.startswith("tgx:a:")
        assert reject_data.startswith("tgx:r:")
        assert len(approve_data) <= 64
        saved_ingress = (fake_logger.saved_payload or {}).get("ingress_config") or {}
        tokens = saved_ingress.get("pending_hitl_tokens") or {}
        assert tokens
        assert next(iter(tokens.values()))["job_id"] == "6434ac0d-8323-4746-a5f4-3305d42abeb9"

    def test_ingress_event_resolves_uuid_job_token(self):
        import main as telegram_main

        class _FakeLogger:
            def get_project_connection(
                self, project_id: str, extension_id: str, enabled_only: bool = False
            ) -> dict:
                return {
                    "project_id": project_id,
                    "extension_id": extension_id,
                    "connection_kind": "extension",
                    "enabled": True,
                    "connection_config": _provider_with_human_decision_channel()["provider_config"],
                    "ingress_config": {
                        "pending_hitl_tokens": {
                            "abc123token": {
                                "project_id": "58eeaeb3-a9c1-4c51-b292-14eb86899d86",
                                "job_id": "6434ac0d-8323-4746-a5f4-3305d42abeb9",
                                "approval_id": "gate-1",
                                "allowed_actions": ["approve", "reject"],
                                "created_at": _fresh_created_at(),
                            }
                        }
                    },
                }

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            telegram_main, "_runtime_logger", lambda *_args, **_kwargs: _FakeLogger()
        )
        try:
            resp, code = telegram_main._handle_action(
                {
                    "action_id": "ingress_event",
                    "input": {
                        "callback": {"authenticated": True},
                        "request": {
                            "provider": _provider_with_human_decision_channel(),
                            "payload": {
                                "callback_query": {
                                    "id": "cb-uuid",
                                    "data": "tgx:a:abc123token",
                                    "from": {"id": 10, "username": "alice"},
                                    "message": {
                                        "message_id": 100,
                                        "chat": {"id": -100123},
                                        "text": "Review requested",
                                    },
                                }
                            },
                            "query": {"project_id": "58eeaeb3-a9c1-4c51-b292-14eb86899d86"},
                        },
                    },
                }
            )
        finally:
            monkeypatch.undo()

        assert code == 0
        assert resp["ok"] is True
        assert resp["result"]["job_id"] == "6434ac0d-8323-4746-a5f4-3305d42abeb9"
        assert resp["result"]["project_id"] == "58eeaeb3-a9c1-4c51-b292-14eb86899d86"

    def test_pending_hitl_token_rejects_expired_cross_project_and_wrong_chat(self):
        import main as telegram_main

        token_state = {
            "pending_hitl_tokens": {
                "expired": {
                    "project_id": "11",
                    "job_id": "77",
                    "approval_id": "gate-1",
                    "allowed_actions": ["approve", "reject"],
                    "chat_id": "-100123",
                    "created_at": "2020-01-01T00:00:00+00:00",
                },
                "bound": {
                    "project_id": "11",
                    "job_id": "77",
                    "approval_id": "gate-1",
                    "allowed_actions": ["approve", "reject"],
                    "chat_id": "-100123",
                    "created_at": _fresh_created_at(),
                },
            }
        }

        assert not telegram_main._resolve_pending_hitl_token(
            None,
            project_id="11",
            job_ref="expired",
            inbound_state=token_state,
            chat_id=-100123,
        )
        assert not telegram_main._resolve_pending_hitl_token(
            None,
            project_id="12",
            job_ref="bound",
            inbound_state=token_state,
            chat_id=-100123,
        )
        assert not telegram_main._resolve_pending_hitl_token(
            None,
            project_id="11",
            job_ref="bound",
            inbound_state=token_state,
            chat_id=-100999,
        )

    def test_pending_hitl_token_is_consumed_exactly_once(self):
        import main as telegram_main

        class _MutableLogger:
            def __init__(self) -> None:
                self.connection = {
                    "project_id": "11",
                    "extension_id": "flowsteward.telegram",
                    "connection_kind": "extension",
                    "enabled": True,
                    "connection_config": {},
                    "ingress_config": {},
                }

            def get_project_connection(self, **_kwargs) -> dict[str, Any]:
                return dict(self.connection)

            def upsert_project_connection(self, **kwargs) -> None:
                self.connection.update(kwargs)

        logger = _MutableLogger()
        inbound_state = {
            "pending_hitl_tokens": {
                "once": {
                    "project_id": "11",
                    "job_id": "77",
                    "approval_id": "gate-1",
                    "allowed_actions": ["approve", "reject"],
                    "chat_id": "-100123",
                    "created_at": _fresh_created_at(),
                }
            }
        }

        first = telegram_main._consume_pending_hitl_token(
            logger,
            project_id="11",
            job_ref="once",
            inbound_state=inbound_state,
            chat_id=-100123,
        )
        second = telegram_main._consume_pending_hitl_token(
            logger,
            project_id="11",
            job_ref="once",
            inbound_state=inbound_state,
            chat_id=-100123,
        )

        assert first["job_id"] == "77"
        assert second == {}

    def test_registering_a_new_gate_invalidates_the_old_token_for_the_same_job(self, monkeypatch):
        import main as telegram_main
        import main_state

        class _MutableLogger:
            def __init__(self) -> None:
                self.saved_payload: dict[str, Any] = {}

            def get_project_connection(self, **_kwargs) -> dict[str, Any]:
                return {
                    "project_id": "11",
                    "extension_id": "flowsteward.telegram",
                    "enabled": True,
                    "connection_config": {},
                    "ingress_config": {},
                }

            def upsert_project_connection(self, **kwargs) -> None:
                self.saved_payload = dict(kwargs)

        inbound_state = {
            "pending_hitl_tokens": {
                "old-token": {
                    "project_id": "11",
                    "job_id": "77",
                    "approval_id": "old-gate",
                    "chat_id": "-100123",
                    "created_at": _fresh_created_at(),
                }
            }
        }
        monkeypatch.setattr(main_state, "_build_hitl_job_ref", lambda *_args: "new-token")

        job_ref = telegram_main._register_pending_hitl_token(
            _MutableLogger(),
            project_id="11",
            inbound_state=inbound_state,
            job_id="77",
            approval_id="new-gate",
            allowed_actions=["approve", "abort"],
            chat_id="-100123",
        )

        assert job_ref == "new-token"
        assert "old-token" not in inbound_state["pending_hitl_tokens"]
        assert inbound_state["pending_hitl_tokens"]["new-token"]["allowed_actions"] == [
            "approve",
            "abort",
        ]

    def test_ingress_event_resolves_uuid_job_token_from_callback_project_context(self):
        import main as telegram_main

        class _FakeLogger:
            def get_project_connection(
                self, project_id: str, extension_id: str, enabled_only: bool = False
            ) -> dict:
                return {
                    "project_id": project_id,
                    "extension_id": extension_id,
                    "connection_kind": "extension",
                    "enabled": True,
                    "connection_config": _provider_with_human_decision_channel()["provider_config"],
                    "ingress_config": {
                        "pending_hitl_tokens": {
                            "abc123token": {
                                "project_id": "58eeaeb3-a9c1-4c51-b292-14eb86899d86",
                                "job_id": "6434ac0d-8323-4746-a5f4-3305d42abeb9",
                                "approval_id": "gate-1",
                                "allowed_actions": ["approve", "reject"],
                                "created_at": _fresh_created_at(),
                            }
                        }
                    },
                }

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            telegram_main, "_runtime_logger", lambda *_args, **_kwargs: _FakeLogger()
        )
        try:
            resp, code = telegram_main._handle_action(
                {
                    "action_id": "ingress_event",
                    "input": {
                        "callback": {
                            "project_id": "58eeaeb3-a9c1-4c51-b292-14eb86899d86",
                            "authenticated": True,
                        },
                        "request": {
                            "provider": _provider_with_human_decision_channel(),
                            "payload": {
                                "callback_query": {
                                    "id": "cb-uuid",
                                    "data": "tgx:a:abc123token",
                                    "from": {"id": 10, "username": "alice"},
                                    "message": {
                                        "message_id": 100,
                                        "chat": {"id": -100123},
                                        "text": "Review requested",
                                    },
                                }
                            },
                        },
                    },
                }
            )
        finally:
            monkeypatch.undo()

        assert code == 0
        assert resp["ok"] is True
        assert resp["result"]["job_id"] == "6434ac0d-8323-4746-a5f4-3305d42abeb9"
        assert resp["result"]["project_id"] == "58eeaeb3-a9c1-4c51-b292-14eb86899d86"

    def test_ingress_event_rejects_unauthorized_human_decision_channel(self):
        resp, code = _invoke(
            {
                "action": "ingress_event",
                "request": {
                    "callback": {"authenticated": True},
                    "provider": {
                        "project_id": "11",
                        "ingress_config": _provider_with_pending_token()["ingress_config"],
                        "provider_config": {
                            "channels": [
                                {
                                    "channel_id": "alerts",
                                    "chat_id": "-100123",
                                    "enabled": True,
                                    "capabilities": {"notify": True, "human_decision": False},
                                }
                            ]
                        },
                    },
                    "payload": {
                        "callback_query": {
                            "id": "cb-unauthorized-chat",
                            "data": "tgx:a:abc123token",
                            "from": {"id": 10, "username": "alice"},
                            "message": {
                                "message_id": 102,
                                "chat": {"id": -100123},
                                "text": "Please review",
                            },
                        }
                    },
                },
            }
        )
        assert code == 2
        assert resp["ok"] is False
        assert resp["error_code"] == "unauthorized_human_decision_channel"
        assert resp.get("events") == []

    def test_ingress_event_reject_prompt_reply(self):
        resp, code = _invoke(
            {
                "action": "ingress_event",
                "request": {
                    "callback": {"authenticated": True},
                    "provider": _provider_with_pending_token(),
                    "payload": {
                        "message": {
                            "message_id": 103,
                            "chat": {"id": -100123},
                            "text": "Needs more detail",
                            "from": {"id": 10, "username": "alice"},
                            "reply_to_message": {
                                "from": {"id": 999, "is_bot": True},
                                "text": (
                                    "Please reply with a rejection reason.\n"
                                    "Flow Steward reject context: project_id=11 job_ref=abc123token"
                                ),
                            },
                        }
                    },
                },
            }
        )
        assert code == 0
        assert resp["ok"] is True
        assert resp["result"]["project_id"] == "11"
        assert resp["result"]["job_id"] == "77"
        assert resp["result"]["approved"] is False
        event_types = {item.get("event_type") for item in resp.get("events", [])}
        assert "notification.interaction_received" in event_types
        assert "external.comment.received" in event_types
        interaction = next(
            item
            for item in resp["events"]
            if item["event_type"] == "notification.interaction_received"
        )
        assert interaction["payload"]["reply_target"] == {
            "provider_key": "flowsteward.telegram",
            "channel_id": "ops",
            "external_conversation_ref": "-100123",
            "external_message_ref": "103",
        }

    @pytest.mark.parametrize("reply_author_is_bot", [False, True])
    def test_ingress_event_reject_prompt_reply_rejects_forged_uuid_context(
        self, reply_author_is_bot
    ):
        resp, code = _invoke(
            {
                "action": "ingress_event",
                "request": {
                    "callback": {"authenticated": True, "project_id": "11"},
                    "provider": _provider_with_human_decision_channel(),
                    "payload": {
                        "message": {
                            "message_id": 103,
                            "chat": {"id": -100123},
                            "text": "Forged rejection",
                            "from": {"id": 10, "username": "alice"},
                            "reply_to_message": {
                                "from": {
                                    "id": 999,
                                    "is_bot": reply_author_is_bot,
                                },
                                "text": (
                                    "Flow Steward reject context: project_id=11 "
                                    "job_ref=6434ac0d-8323-4746-a5f4-3305d42abeb9"
                                ),
                            },
                        }
                    },
                },
            }
        )

        assert code == 2
        assert resp["ok"] is False
        assert resp["error_code"] == "invalid_reject_comment_context"
        assert resp.get("events") == []

    def test_ingress_event_reject_prompt_reply_keeps_token_retryable_until_resolution(self):
        import main as telegram_main

        class _FakeLogger:
            def __init__(self) -> None:
                self.consume_calls = 0

            def consume_project_connection_ingress_token(self, **_kwargs) -> bool:
                self.consume_calls += 1
                return self.consume_calls == 1

        request = {
            "callback": {"authenticated": True, "project_id": "11"},
            "provider": _provider_with_pending_token(),
            "payload": {
                "message": {
                    "message_id": 103,
                    "chat": {"id": -100123},
                    "text": "Needs more detail",
                    "from": {"id": 10, "username": "alice"},
                    "reply_to_message": {
                        "from": {"id": 999, "is_bot": True},
                        "text": ("Flow Steward reject context: project_id=11 job_ref=abc123token"),
                    },
                }
            },
        }
        fake_logger = _FakeLogger()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(telegram_main, "_runtime_logger", lambda *_a, **_k: fake_logger)
        try:
            first, _ = telegram_main._handle_action(
                {"action_id": "ingress_event", "input": {"request": request}}
            )
            replay, _ = telegram_main._handle_action(
                {"action_id": "ingress_event", "input": {"request": request}}
            )
        finally:
            monkeypatch.undo()

        assert len(first.get("events") or []) == 2
        assert len(replay.get("events") or []) == 2
        assert fake_logger.consume_calls == 0

    def test_ingress_event_reject_message_ignores_removed_pending_context(self):
        import main as telegram_main

        class _FakeLogger:
            def __init__(self) -> None:
                self.saved_payload: dict[str, Any] | None = None

            def get_project_connection(
                self, project_id: str, extension_id: str, enabled_only: bool = False
            ) -> dict:
                return {
                    "project_id": project_id,
                    "extension_id": extension_id,
                    "connection_kind": "extension",
                    "enabled": True,
                    "connection_config": _provider_with_human_decision_channel()["provider_config"],
                    "ingress_config": {
                        "pending_reject_context": {
                            "-100123:10": {
                                "project_id": "11",
                                "job_id": "6434ac0d-8323-4746-a5f4-3305d42abeb9",
                                "job_ref": "6434ac0d-8323-4746-a5f4-3305d42abeb9",
                                "chat_id": -100123,
                                "actor_id": 10,
                                "created_at": "2026-05-10T08:00:00+00:00",
                            }
                        }
                    },
                }

            def upsert_project_connection(self, **kwargs) -> None:
                self.saved_payload = dict(kwargs)

        fake_logger = _FakeLogger()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(telegram_main, "_runtime_logger", lambda *_args, **_kwargs: fake_logger)
        try:
            resp, code = telegram_main._handle_action(
                {
                    "action_id": "ingress_event",
                    "input": {
                        "callback": {"authenticated": True},
                        "request": {
                            "provider": _provider_with_human_decision_channel(),
                            "payload": {
                                "message": {
                                    "message_id": 103,
                                    "chat": {"id": -100123},
                                    "text": "Needs more detail",
                                    "from": {"id": 10, "username": "alice"},
                                }
                            },
                            "query": {"project_id": "11"},
                        },
                    },
                }
            )
        finally:
            monkeypatch.undo()

        assert code == 0
        assert resp["ok"] is True
        assert resp["result"]["ignored"] is True
        assert resp.get("events") == []
        assert fake_logger.saved_payload is None

    def test_ingress_event_reject_prompt_reply_does_not_accept_removed_uuid_context(self):
        import main as telegram_main

        class _FakeLogger:
            def __init__(self) -> None:
                self.saved_payload: dict[str, Any] | None = None

            def get_project_connection(
                self, project_id: str, extension_id: str, enabled_only: bool = False
            ) -> dict:
                return {
                    "project_id": project_id,
                    "extension_id": extension_id,
                    "connection_kind": "extension",
                    "enabled": True,
                    "connection_config": _provider_with_human_decision_channel()["provider_config"],
                    "ingress_config": {
                        "pending_reject_context": {
                            "-100123:10": {
                                "project_id": "11",
                                "job_id": "77",
                                "job_ref": "77",
                                "chat_id": -100123,
                                "actor_id": 10,
                                "created_at": "2026-05-10T08:00:00+00:00",
                            }
                        }
                    },
                }

            def upsert_project_connection(self, **kwargs) -> None:
                self.saved_payload = dict(kwargs)

        fake_logger = _FakeLogger()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(telegram_main, "_runtime_logger", lambda *_args, **_kwargs: fake_logger)
        try:
            resp, code = telegram_main._handle_action(
                {
                    "action_id": "ingress_event",
                    "input": {
                        "callback": {"authenticated": True},
                        "request": {
                            "provider": _provider_with_human_decision_channel(),
                            "payload": {
                                "message": {
                                    "message_id": 103,
                                    "chat": {"id": -100123},
                                    "text": "Needs more detail",
                                    "from": {"id": 10, "username": "alice"},
                                    "reply_to_message": {
                                        "text": (
                                            "Reply to this message with a rejection reason.\n"
                                            "Flow Steward reject context: project_id=11 job_ref=6434ac0d-8323-4746-a5f4-3305d42abeb9\n"
                                            "Fallback: /reject 6434ac0d-8323-4746-a5f4-3305d42abeb9 <comment>"
                                        )
                                    },
                                }
                            },
                        },
                    },
                }
            )
        finally:
            monkeypatch.undo()

        assert code == 0
        assert resp["ok"] is False
        assert resp["error"] == "invalid_reject_comment_context"
        assert resp.get("events") == []
        assert fake_logger.saved_payload is None

    def test_ingress_event_reject_command_raw_uuid_rejected(self):
        # A typed /reject <job_uuid> must NOT be accepted on a raw job UUID alone;
        # the pending HITL token is required (reply-bound reject flows are separate).
        resp, code = _invoke(
            {
                "action": "ingress_event",
                "request": {
                    "callback": {"authenticated": True},
                    "query": {"project_id": "11"},
                    "provider": _provider_with_human_decision_channel(),
                    "payload": {
                        "message": {
                            "message_id": 104,
                            "chat": {"id": -100123},
                            "text": "/reject 6434ac0d-8323-4746-a5f4-3305d42abeb9 Needs more detail",
                            "from": {"id": 10, "username": "alice"},
                        }
                    },
                },
            }
        )
        assert code == 2
        assert resp["ok"] is False
        assert resp["error_code"] == "invalid_reject_comment_context"
        assert resp.get("events") == []

    def test_ingress_event_reject_message_does_not_treat_job_ref_token_as_job_id(self):
        import main as telegram_main

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(telegram_main, "_runtime_logger", lambda *_args, **_kwargs: None)
        try:
            resp, code = telegram_main._handle_action(
                {
                    "action_id": "ingress_event",
                    "input": {
                        "callback": {"authenticated": True},
                        "request": {
                            "query": {"project_id": "58eeaeb3-a9c1-4c51-b292-14eb86899d86"},
                            "payload": {
                                "message": {
                                    "message_id": 105,
                                    "chat": {"id": -100123},
                                    "text": "Needs more detail",
                                    "from": {"id": 10, "username": "alice"},
                                    "reply_to_message": {
                                        "text": (
                                            "Reply to this message with a rejection reason.\n"
                                            "Flow Steward reject context: project_id=58eeaeb3-a9c1-4c51-b292-14eb86899d86 "
                                            "job_ref=049e6ce5cddc\n"
                                            "Fallback: /reject 049e6ce5cddc <comment>"
                                        )
                                    },
                                }
                            },
                        },
                    },
                }
            )
        finally:
            monkeypatch.undo()

        assert code == 0
        assert resp["ok"] is False
        assert resp["error"] == "invalid_reject_comment_context"
        assert resp.get("events") == []

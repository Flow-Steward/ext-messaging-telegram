from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from subprocess_contract_helpers import _invoke


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


class TestTelegramActions:
    def test_ingress_event_uses_hydrated_project_token_for_reject_callback(self):
        import main as telegram_main

        answered_with: list[str] = []
        prompted_with: list[str] = []
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            telegram_main,
            "_answer_callback_query",
            lambda token, *_args, **_kwargs: answered_with.append(token) or True,
        )
        monkeypatch.setattr(
            telegram_main,
            "_send_reject_reason_prompt",
            lambda token, *_args, **_kwargs: (
                prompted_with.append(token) or {"ok": True, "external_effect_status": "succeeded"}
            ),
        )
        try:
            response, code = telegram_main._handle_action(
                {
                    "action_id": "ingress_event",
                    "context": {"project_id": "7"},
                    "runtime_context": {
                        "resources": {
                            "provider_secrets": {"bot_token": "project-token"},
                            "project_connection": {
                                "project_id": "7",
                                "connection_config": _provider_with_human_decision_channel()[
                                    "provider_config"
                                ],
                                "ingress_config": {
                                    "pending_hitl_tokens": {
                                        "action-token": {
                                            "project_id": "7",
                                            "job_id": "42",
                                            "approval_id": "gate-1",
                                            "allowed_actions": ["approve", "reject"],
                                            "chat_id": "-100123",
                                            "created_at": datetime.now(UTC).isoformat(),
                                        }
                                    }
                                },
                            },
                        }
                    },
                    "input": {
                        "callback": {"authenticated": True, "project_id": "7"},
                        "request": {
                            "provider": {"provider_config": {}},
                            "payload": {
                                "callback_query": {
                                    "id": "cb-reject",
                                    "data": "tgx:r:action-token",
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
        assert response["ok"] is True
        assert answered_with == ["project-token"]
        assert prompted_with == ["project-token"]
        assert "project-token" not in str(response)

    def test_polling_uses_project_token_for_callback_answers(self):
        import main as telegram_main

        inbound_state = {
            "inbound_mode": "polling",
            "pending_hitl_tokens": {
                "action-token": {
                    "project_id": "7",
                    "job_id": "42",
                    "approval_id": "gate-1",
                    "allowed_actions": ["approve", "reject"],
                    "chat_id": "-100123",
                    "created_at": datetime.now(UTC).isoformat(),
                }
            },
        }

        class _FakeLogger:
            def get_project_connection(self, **_kwargs):
                return {
                    "connection_config": _provider_with_human_decision_channel()["provider_config"]
                }

            def upsert_project_connection(self, **_kwargs):
                return None

            def consume_project_connection_ingress_token(self, **_kwargs):
                return True

        answered_with: list[str] = []
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            telegram_main,
            "_telegram_get_updates",
            lambda *_args, **_kwargs: {
                "ok": True,
                "result": [
                    {
                        "update_id": 10,
                        "callback_query": {
                            "id": "cb-approve",
                            "data": "tgx:a:action-token",
                            "from": {"id": 10, "username": "alice"},
                            "message": {
                                "message_id": 100,
                                "chat": {"id": -100123},
                                "text": "Review requested",
                            },
                        },
                    }
                ],
            },
        )
        monkeypatch.setattr(
            telegram_main,
            "_answer_callback_query",
            lambda token, *_args, **_kwargs: answered_with.append(token) or True,
        )
        try:
            response, code = telegram_main._poll_updates(
                bl=_FakeLogger(),
                project_id="7",
                inbound_state=inbound_state,
                token="project-token",
            )
        finally:
            monkeypatch.undo()

        assert code == 0
        assert response["ok"] is True
        assert answered_with == ["project-token"]
        assert "project-token" not in str(response)

    def test_action_retry_delivery(self):
        import main as telegram_main

        class _FakeLogger:
            def list_notification_delivery_logs(
                self, project_id: str, limit: int = 2000
            ) -> list[dict]:
                assert project_id == "proj-7"
                return [
                    {
                        "delivery_id": "msg_1",
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
                ]

            def get_project_connection(
                self, project_id: str, extension_id: str, enabled_only: bool = False
            ) -> dict:
                return {
                    "connection_config": {
                        "channels": [
                            {
                                "channel_id": "ops",
                                "chat_id": "-100123",
                                "enabled": True,
                                "capabilities": {"notify": True, "human_decision": False},
                            }
                        ]
                    }
                }

            def get_provider_secret(
                self,
                provider_key: str,
                secret_key: str,
                project_id: str,
                include_global: bool = True,
            ) -> str:
                return "fake-token"

            def log_notification_delivery(self, **kwargs) -> dict:
                return {"delivery_id": "retry-1", **kwargs}

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            telegram_main, "_runtime_logger", lambda *_args, **_kwargs: _FakeLogger()
        )
        monkeypatch.setattr(
            telegram_main,
            "_send_message",
            lambda *args, **kwargs: {"ok": True, "message_id": 42},
        )
        try:
            resp, code = telegram_main._handle_action(
                {
                    "action_id": "retry_delivery",
                    "context": {"project_id": "proj-7"},
                    "target": {"scope": "row", "item_ids": ["msg_1"]},
                }
            )
            assert code == 0
            assert resp["ok"] is True
            assert resp["result"]["delivered"] is True
            assert resp["result"]["provider_message_id"] == "42"
        finally:
            monkeypatch.undo()

    def test_action_retry_delivery_rejects_delivered_rows(self):
        import main as telegram_main

        class _FakeLogger:
            def list_notification_delivery_logs(
                self, project_id: str, limit: int = 2000
            ) -> list[dict]:
                assert project_id == "proj-7"
                return [
                    {
                        "delivery_id": "msg_1",
                        "project_id": "proj-7",
                        "provider_key": "flowsteward.telegram",
                        "event_type": "job.failed",
                        "severity": "error",
                        "status": "delivered",
                        "request": {
                            "event": {
                                "event_type": "job.failed",
                                "severity": "error",
                                "summary": "Failure",
                            },
                            "route": {"route_config": {"channel_id": "ops"}},
                        },
                    }
                ]

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            telegram_main, "_runtime_logger", lambda *_args, **_kwargs: _FakeLogger()
        )
        try:
            resp, code = telegram_main._handle_action(
                {
                    "action_id": "retry_delivery",
                    "context": {"project_id": "proj-7"},
                    "target": {"scope": "row", "item_ids": ["msg_1"]},
                }
            )
            assert code == 0
            assert resp["ok"] is False
            assert resp["error"] == "delivery_not_retryable"
            assert resp["result"]["original_status"] == "delivered"
        finally:
            monkeypatch.undo()

    def test_action_test_delivery_requires_project_context(self):
        resp, code = _invoke(
            {
                "mode": "action",
                "action": {"action_id": "test_delivery", "input": {}},
            }
        )
        assert code == 2
        assert resp["ok"] is False
        assert resp["error_code"] == "project_context_required"
        assert resp["error"] == "Select a project before running this Telegram action."
        assert resp["errors"] == [
            {
                "code": "project_context_required",
                "message": "Select a project before running this Telegram action.",
            }
        ]

    def test_action_toggle_route(self):
        resp, code = _invoke(
            {
                "mode": "action",
                "action": {
                    "action_id": "toggle_route",
                    "target": {"scope": "row", "item_ids": ["route_1"]},
                },
            }
        )
        assert code == 0
        assert resp["ok"] is True

    def test_action_ingress_event_with_callback_query(self):
        resp, code = _invoke(
            {
                "mode": "action",
                "runtime_context": {
                    "resources": {
                        "project_connection": {
                            "connection_id": "conn-telegram",
                            "project_id": "7",
                            "extension_id": "flowsteward.telegram",
                            "extension_install_id": "install-telegram",
                            "connection_type": "telegram_bot",
                            "enabled": True,
                            "ingress_config": {
                                "pending_hitl_tokens": {
                                    "action-token": {
                                        "project_id": "7",
                                        "job_id": "42",
                                        "approval_id": "gate-1",
                                        "allowed_actions": ["approve", "reject"],
                                        "chat_id": "-100123",
                                        "created_at": datetime.now(UTC).isoformat(),
                                    }
                                }
                            },
                        }
                    }
                },
                "action": {
                    "action_id": "ingress_event",
                    "input": {
                        "callback": {"authenticated": True},
                        "request": {
                            "provider": {
                                **_provider_with_human_decision_channel(),
                                "project_id": "7",
                                "ingress_config": {
                                    "pending_hitl_tokens": {
                                        "action-token": {
                                            "project_id": "7",
                                            "job_id": "42",
                                            "approval_id": "gate-1",
                                            "allowed_actions": ["approve", "reject"],
                                            "chat_id": "-100123",
                                            "created_at": datetime.now(UTC).isoformat(),
                                        }
                                    }
                                },
                            },
                            "payload": {
                                "callback_query": {
                                    "id": "cb-1",
                                    "data": "tgx:a:action-token",
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
                },
            }
        )
        assert code == 0
        assert resp["ok"] is True
        assert resp["result"]["project_id"] == "7"
        assert resp["result"]["job_id"] == "42"
        event_types = {item.get("event_type") for item in resp.get("events", [])}
        assert "notification.interaction_received" in event_types
        assert "external.comment.received" not in event_types
        interaction_event = next(
            item
            for item in resp.get("events", [])
            if item.get("event_type") == "notification.interaction_received"
        )
        assert interaction_event["payload"]["telegram"]["chat_id"] == -100123
        mutations = resp.get("host_mutations") or []
        assert mutations == []

    def test_action_configure_webhook_fake_mode_returns_typed_result(self):
        resp, code = _invoke(
            {
                "mode": "action",
                "action": {
                    "action_id": "configure_webhook",
                    "input": {
                        "callback_url": "https://example.test/webhook/extensions/flowsteward.telegram/updates/t",
                        "callback_secret": "secret-value",
                        "runtime": {"fake_mode": True},
                    },
                },
            }
        )
        assert code == 0
        assert resp["ok"] is True
        assert resp["result"]["url_configured"] is True
        assert "secret-value" not in json.dumps(resp)

    def test_action_get_webhook_status_fake_mode_returns_typed_result(self):
        resp, code = _invoke(
            {
                "mode": "action",
                "action": {
                    "action_id": "get_webhook_status",
                    "input": {"runtime": {"fake_mode": True}},
                },
            }
        )
        assert code == 0
        assert resp["ok"] is True
        assert resp["result"]["url_configured"] is True
        assert "pending_update_count" in resp["result"]

    def test_action_unknown_returns_error(self):
        resp, code = _invoke({"mode": "action", "action": {"action_id": "nonexistent"}})
        assert code == 2
        assert resp["ok"] is False

    def test_action_resolve_chat_saves_discovered_chat(self):
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
        monkeypatch.setattr(
            telegram_main, "_telegram_token_for_project", lambda *_args, **_kwargs: "fake-token"
        )
        monkeypatch.setattr(
            telegram_main,
            "_telegram_get_chat",
            lambda *_args, **_kwargs: {
                "id": -100123456,
                "title": "Ops Alerts",
                "type": "supergroup",
                "username": "ops_alerts",
            },
        )
        try:
            resp, code = telegram_main._handle_action(
                {
                    "action_id": "resolve_chat",
                    "context": {"project_id": "proj-7"},
                    "input": {"chat_link": "@ops_alerts"},
                }
            )
        finally:
            monkeypatch.undo()

        assert code == 0
        assert resp["ok"] is True
        saved_ingress = (fake_logger.saved_payload or {}).get("ingress_config") or {}
        discovered = saved_ingress.get("discovered_chats") or []
        assert len(discovered) == 1
        assert discovered[0]["chat_id"] == "-100123456"
        assert discovered[0]["username"] == "ops_alerts"

    def test_action_resolve_chat_returns_structured_not_resolved_result(self):
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
                    "connection_config": {},
                    "ingress_config": {},
                }

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            telegram_main, "_runtime_logger", lambda *_args, **_kwargs: _FakeLogger()
        )
        monkeypatch.setattr(
            telegram_main, "_telegram_token_for_project", lambda *_args, **_kwargs: "fake-token"
        )
        monkeypatch.setattr(telegram_main, "_telegram_get_chat", lambda *_args, **_kwargs: {})
        try:
            resp, code = telegram_main._handle_action(
                {
                    "action_id": "resolve_chat",
                    "context": {"project_id": "proj-7"},
                    "input": {"chat_link": "@missing_chat"},
                }
            )
        finally:
            monkeypatch.undo()

        assert code == 0
        assert resp["ok"] is False
        assert resp["error"] == "chat_not_resolved"
        assert "could not resolve" in str(resp["message"]).lower()

    def test_action_discover_chat_private_invite_returns_guidance(self):
        import main as telegram_main

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            telegram_main, "_telegram_token_for_project", lambda *_args, **_kwargs: "fake-token"
        )
        monkeypatch.setattr(
            telegram_main,
            "_telegram_get_me",
            lambda *_args, **_kwargs: {"username": "flow_steward_test_bot"},
        )
        try:
            resp, code = telegram_main._handle_action(
                {
                    "action_id": "discover_chat",
                    "context": {"project_id": "proj-7"},
                    "input": {"chat_link": "https://t.me/+abcdef12345"},
                }
            )
        finally:
            monkeypatch.undo()

        assert code == 0
        assert resp["ok"] is True
        assert resp["result"]["pending_discovery"] is True
        assert "flow_steward_test_bot" in resp["message"]

    def test_action_poll_now_advances_cursor_and_emits_events(self):
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
                    "connection_config": _provider_with_human_decision_channel("-10099")[
                        "provider_config"
                    ],
                    "ingress_config": {
                        "inbound_mode": "polling",
                        "last_update_id": 100,
                        "webhook_status": {"status": "not_configured", "url_configured": False},
                        "pending_hitl_tokens": {
                            "polltoken": {
                                "project_id": "proj-7",
                                "job_id": "6434ac0d-8323-4746-a5f4-3305d42abeb9",
                                "approval_id": "gate-1",
                                "allowed_actions": ["approve", "reject"],
                                "chat_id": "-10099",
                                "created_at": datetime.now(UTC).isoformat(),
                            }
                        },
                    },
                }

            def upsert_project_connection(self, **kwargs) -> None:
                self.saved_payload = dict(kwargs)

        fake_logger = _FakeLogger()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(telegram_main, "_runtime_logger", lambda *_args, **_kwargs: fake_logger)
        monkeypatch.setattr(
            telegram_main, "_telegram_token_for_project", lambda *_args, **_kwargs: "fake-token"
        )
        monkeypatch.setattr(
            telegram_main,
            "_telegram_get_updates",
            lambda *_args, **_kwargs: {
                "ok": True,
                "result": [
                    {
                        "update_id": 101,
                        "message": {
                            "message_id": 55,
                            "chat": {"id": -10099, "title": "Ops", "type": "supergroup"},
                            "text": "/approve polltoken Looks good",
                            "from": {"id": 9, "username": "reviewer"},
                        },
                    },
                    {
                        "update_id": 102,
                        "my_chat_member": {
                            "chat": {"id": -10066, "title": "Audit", "type": "group"},
                        },
                    },
                ],
            },
        )
        try:
            resp, code = telegram_main._handle_action(
                {
                    "action_id": "poll_now",
                    "context": {"project_id": "proj-7"},
                }
            )
        finally:
            monkeypatch.undo()

        assert code == 0
        assert resp["ok"] is True
        assert resp["result"]["processed_updates"] == 2
        event_types = {item.get("event_type") for item in resp.get("events", [])}
        assert "notification.interaction_received" in event_types
        saved_ingress = (fake_logger.saved_payload or {}).get("ingress_config") or {}
        assert saved_ingress["last_update_id"] == 102
        assert len(saved_ingress.get("discovered_chats") or []) >= 1

    def test_action_poll_now_without_token_persists_polling_error_state(self):
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
                    "ingress_config": {
                        "inbound_mode": "polling",
                    },
                }

            def upsert_project_connection(self, **kwargs) -> None:
                self.saved_payload = dict(kwargs)

        fake_logger = _FakeLogger()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(telegram_main, "_runtime_logger", lambda *_args, **_kwargs: fake_logger)
        monkeypatch.setattr(
            telegram_main, "_telegram_token_for_project", lambda *_args, **_kwargs: ""
        )
        try:
            resp, code = telegram_main._handle_action(
                {
                    "action_id": "poll_now",
                    "context": {"project_id": "proj-7"},
                }
            )
        finally:
            monkeypatch.undo()

        assert code == 0
        assert resp["ok"] is False
        assert resp["error"] == "bot_token_required"
        saved_ingress = (fake_logger.saved_payload or {}).get("ingress_config") or {}
        assert saved_ingress["polling_status"] == "failed"
        assert saved_ingress["last_inbound_error"] == "bot_token_required"
        assert saved_ingress["active_transport"] == "polling"
        assert saved_ingress["last_inbound_sync_at"]

    def test_webhook_refresh_then_poll_disables_webhook_before_get_updates(self):
        import main as telegram_main

        class _FakeLogger:
            def __init__(self) -> None:
                self.connection = {
                    "connection_id": "conn-7",
                    "project_id": "proj-7",
                    "account_id": "acct-7",
                    "extension_id": "flowsteward.telegram",
                    "extension_install_id": "install-7",
                    "connection_kind": "extension",
                    "connection_type": "telegram_bot",
                    "enabled": True,
                    "connection_config": {},
                    "secret_refs": {},
                    "connection_routes": [],
                    "ingress_config": {},
                    "workflow_mappings": {},
                }

            def get_project_connection(self, **_kwargs) -> dict:
                return dict(self.connection)

            def upsert_project_connection(self, **kwargs) -> None:
                self.connection.update(kwargs)

        fake_logger = _FakeLogger()
        calls: list[str] = []

        def telegram_call(_token: str, method: str, _payload=None) -> dict:
            calls.append(method)
            if method == "getWebhookInfo":
                return {
                    "ok": True,
                    "result": {"url": "https://example.test/telegram", "pending_update_count": 0},
                }
            if method == "deleteWebhook":
                return {"ok": True, "result": True}
            if method == "getUpdates":
                return {"ok": True, "result": []}
            raise AssertionError(method)

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(telegram_main, "_runtime_logger", lambda *_a, **_k: fake_logger)
        monkeypatch.setattr(
            telegram_main, "_telegram_token_for_project", lambda *_a, **_k: "real-token"
        )
        monkeypatch.setattr(telegram_main, "_telegram_api_call", telegram_call)
        monkeypatch.setattr(
            telegram_main,
            "_telegram_delete_webhook",
            lambda token: telegram_call(token, "deleteWebhook", {}),
        )
        monkeypatch.setattr(
            telegram_main,
            "_telegram_get_updates",
            lambda token, offset=None: telegram_call(token, "getUpdates", {"offset": offset}),
        )
        try:
            status, status_code = telegram_main._handle_action(
                {"action_id": "get_webhook_status", "context": {"project_id": "proj-7"}}
            )
            poll, poll_code = telegram_main._handle_action(
                {"action_id": "poll_now", "context": {"project_id": "proj-7"}}
            )
        finally:
            monkeypatch.undo()

        assert status_code == poll_code == 0
        assert status["ok"] is True
        assert poll["ok"] is True
        assert calls == ["getWebhookInfo", "deleteWebhook", "getUpdates"]
        assert fake_logger.connection["ingress_config"]["polling_status"] == "healthy"

    def test_refresh_reconciles_stale_webhook_projection_before_polling(self):
        import main as telegram_main

        class _FakeLogger:
            def __init__(self) -> None:
                self.connection = {
                    "project_id": "proj-7",
                    "extension_id": "flowsteward.telegram",
                    "connection_kind": "extension",
                    "enabled": True,
                    "connection_config": {},
                    "ingress_config": {
                        "inbound_mode": "polling",
                        "webhook_status": {
                            "status": "not_configured",
                            "url_configured": False,
                        },
                    },
                }

            def get_project_connection(self, **_kwargs) -> dict:
                return dict(self.connection)

            def upsert_project_connection(self, **kwargs) -> None:
                self.connection.update(kwargs)

        fake_logger = _FakeLogger()
        calls: list[str] = []

        def telegram_call(_token: str, method: str, _payload=None) -> dict:
            calls.append(method)
            if method == "getWebhookInfo":
                return {
                    "ok": True,
                    "result": {"url": "https://example.test/telegram", "pending_update_count": 0},
                }
            if method == "deleteWebhook":
                return {"ok": True, "result": True}
            if method == "getUpdates":
                return {"ok": True, "result": []}
            raise AssertionError(method)

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(telegram_main, "_runtime_logger", lambda *_a, **_k: fake_logger)
        monkeypatch.setattr(
            telegram_main, "_telegram_token_for_project", lambda *_a, **_k: "real-token"
        )
        monkeypatch.setattr(telegram_main, "_telegram_api_call", telegram_call)
        monkeypatch.setattr(
            telegram_main,
            "_telegram_delete_webhook",
            lambda token: telegram_call(token, "deleteWebhook", {}),
        )
        monkeypatch.setattr(
            telegram_main,
            "_telegram_get_updates",
            lambda token, offset=None: telegram_call(token, "getUpdates", {"offset": offset}),
        )
        try:
            response, code = telegram_main._handle_action(
                {
                    "action_id": "refresh_discovered_chats",
                    "context": {"project_id": "proj-7"},
                }
            )
        finally:
            monkeypatch.undo()

        assert code == 0
        assert response["ok"] is True
        assert response["message"] == "No new updates found."
        assert response["result"]["processed_updates"] == 0
        assert calls == ["getWebhookInfo", "deleteWebhook", "getUpdates"]
        saved_ingress = fake_logger.connection["ingress_config"]
        assert saved_ingress["webhook_status"]["url_configured"] is False
        assert saved_ingress["polling_status"] == "healthy"

    def test_refresh_without_updates_is_a_successful_noop(self):
        import main as telegram_main

        class _FakeLogger:
            def __init__(self) -> None:
                self.connection = {
                    "project_id": "proj-7",
                    "extension_id": "flowsteward.telegram",
                    "connection_kind": "extension",
                    "enabled": True,
                    "connection_config": {},
                    "ingress_config": {"inbound_mode": "polling"},
                }

            def get_project_connection(self, **_kwargs) -> dict:
                return dict(self.connection)

            def upsert_project_connection(self, **kwargs) -> None:
                self.connection.update(kwargs)

        fake_logger = _FakeLogger()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(telegram_main, "_runtime_logger", lambda *_a, **_k: fake_logger)
        monkeypatch.setattr(
            telegram_main, "_telegram_token_for_project", lambda *_a, **_k: "real-token"
        )
        monkeypatch.setattr(
            telegram_main,
            "_telegram_api_call",
            lambda _token, method, _payload=None: (
                {"ok": True, "result": {"url": "", "pending_update_count": 0}}
                if method == "getWebhookInfo"
                else pytest.fail(f"unexpected Telegram method: {method}")
            ),
        )
        monkeypatch.setattr(
            telegram_main,
            "_telegram_get_updates",
            lambda *_a, **_k: {"ok": True, "result": []},
        )
        try:
            response, code = telegram_main._handle_action(
                {
                    "action_id": "refresh_discovered_chats",
                    "context": {"project_id": "proj-7"},
                }
            )
        finally:
            monkeypatch.undo()

        assert code == 0
        assert response["ok"] is True
        assert response["message"] == "No new updates found."
        assert response["result"]["processed_updates"] == 0
        assert response["reload"]
        assert fake_logger.connection["ingress_config"]["polling_status"] == "healthy"

    def test_poll_now_explains_competing_get_updates_conflict(self):
        import main as telegram_main

        class _FakeLogger:
            def __init__(self) -> None:
                self.saved_payload: dict[str, Any] | None = None

            def get_project_connection(self, **_kwargs) -> dict:
                return {
                    "project_id": "proj-7",
                    "extension_id": "flowsteward.telegram",
                    "connection_kind": "extension",
                    "enabled": True,
                    "connection_config": {},
                    "ingress_config": {"inbound_mode": "polling"},
                }

            def upsert_project_connection(self, **kwargs) -> None:
                self.saved_payload = dict(kwargs)

        fake_logger = _FakeLogger()
        provider_description = (
            "Conflict: terminated by other getUpdates request; "
            "make sure that only one bot instance is running"
        )
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(telegram_main, "_runtime_logger", lambda *_a, **_k: fake_logger)
        monkeypatch.setattr(
            telegram_main, "_telegram_token_for_project", lambda *_a, **_k: "real-token"
        )
        monkeypatch.setattr(
            telegram_main,
            "_telegram_get_updates",
            lambda *_a, **_k: {
                "ok": False,
                "error_code": 409,
                "description": provider_description,
                "error": provider_description,
            },
        )
        try:
            response, code = telegram_main._handle_action(
                {"action_id": "poll_now", "context": {"project_id": "proj-7"}}
            )
        finally:
            monkeypatch.undo()

        assert code == 0
        assert response["ok"] is False
        assert response["error"] == "polling_unavailable"
        assert response["message"] == (
            "Telegram rejected polling because another bot instance is already polling. "
            "Stop the other poller and try again."
        )
        assert response["definitely_no_external_effect"] is True
        assert (fake_logger.saved_payload or {})["ingress_config"][
            "last_inbound_error"
        ] == response["message"]

    def test_webhook_active_get_updates_conflict_is_not_reported_as_another_poller(self):
        import main as telegram_main

        class _FakeLogger:
            def __init__(self) -> None:
                self.saved_payload: dict[str, Any] | None = None

            def get_project_connection(self, **_kwargs) -> dict:
                return {
                    "project_id": "proj-7",
                    "extension_id": "flowsteward.telegram",
                    "connection_kind": "extension",
                    "enabled": True,
                    "connection_config": {},
                    "ingress_config": {"inbound_mode": "polling"},
                }

            def upsert_project_connection(self, **kwargs) -> None:
                self.saved_payload = dict(kwargs)

        fake_logger = _FakeLogger()
        provider_description = (
            "Conflict: can't use getUpdates method while webhook is active; "
            "use deleteWebhook to delete the webhook first"
        )
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(telegram_main, "_runtime_logger", lambda *_a, **_k: fake_logger)
        monkeypatch.setattr(
            telegram_main, "_telegram_token_for_project", lambda *_a, **_k: "real-token"
        )
        monkeypatch.setattr(
            telegram_main,
            "_telegram_get_updates",
            lambda *_a, **_k: {
                "ok": False,
                "error_code": 409,
                "description": provider_description,
                "error": provider_description,
            },
        )
        try:
            response, code = telegram_main._handle_action(
                {"action_id": "poll_now", "context": {"project_id": "proj-7"}}
            )
        finally:
            monkeypatch.undo()

        assert code == 0
        assert response["ok"] is False
        assert response["error"] == "polling_unavailable"
        assert response["message"] == provider_description
        assert "another bot instance" not in response["message"]
        assert response["definitely_no_external_effect"] is True

    @pytest.mark.parametrize(
        "provider_failure",
        [
            {"ok": False, "error": "timed out"},
            {
                "ok": False,
                "error_code": 500,
                "description": "Internal Server Error",
                "error": "Internal Server Error",
            },
        ],
        ids=["transport-response-lost", "provider-5xx"],
    )
    def test_poll_failure_without_explicit_conflict_is_not_marked_no_effect(
        self,
        provider_failure: dict[str, Any],
    ):
        import main as telegram_main

        class _FakeLogger:
            def get_project_connection(self, **_kwargs) -> dict:
                return {
                    "project_id": "proj-7",
                    "extension_id": "flowsteward.telegram",
                    "connection_kind": "extension",
                    "enabled": True,
                    "connection_config": {},
                    "ingress_config": {"inbound_mode": "polling"},
                }

            def upsert_project_connection(self, **_kwargs) -> None:
                return None

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(telegram_main, "_runtime_logger", lambda *_a, **_k: _FakeLogger())
        monkeypatch.setattr(
            telegram_main, "_telegram_token_for_project", lambda *_a, **_k: "real-token"
        )
        monkeypatch.setattr(
            telegram_main,
            "_telegram_get_updates",
            lambda *_a, **_k: dict(provider_failure),
        )
        try:
            response, code = telegram_main._handle_action(
                {"action_id": "poll_now", "context": {"project_id": "proj-7"}}
            )
        finally:
            monkeypatch.undo()

        assert code == 0
        assert response["error"] == "polling_unavailable"
        assert response.get("definitely_no_external_effect") is not True

    def test_poll_failure_after_webhook_delete_is_not_marked_no_effect(self):
        import main as telegram_main

        class _FakeLogger:
            def get_project_connection(self, **_kwargs) -> dict:
                return {
                    "project_id": "proj-7",
                    "extension_id": "flowsteward.telegram",
                    "connection_kind": "extension",
                    "enabled": True,
                    "connection_config": {},
                    "ingress_config": {
                        "inbound_mode": "polling",
                        "webhook_status": {
                            "status": "healthy",
                            "url_configured": True,
                        },
                    },
                }

            def upsert_project_connection(self, **_kwargs) -> None:
                return None

        provider_description = (
            "Conflict: terminated by other getUpdates request; "
            "make sure that only one bot instance is running"
        )
        calls: list[str] = []
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(telegram_main, "_runtime_logger", lambda *_a, **_k: _FakeLogger())
        monkeypatch.setattr(
            telegram_main, "_telegram_token_for_project", lambda *_a, **_k: "real-token"
        )
        monkeypatch.setattr(
            telegram_main,
            "_telegram_delete_webhook",
            lambda *_a, **_k: calls.append("deleteWebhook") or {"ok": True},
        )
        monkeypatch.setattr(
            telegram_main,
            "_telegram_get_updates",
            lambda *_a, **_k: (
                calls.append("getUpdates")
                or {
                    "ok": False,
                    "error_code": 409,
                    "description": provider_description,
                    "error": provider_description,
                }
            ),
        )
        try:
            response, code = telegram_main._handle_action(
                {"action_id": "poll_now", "context": {"project_id": "proj-7"}}
            )
        finally:
            monkeypatch.undo()

        assert code == 0
        assert response["error"] == "polling_unavailable"
        assert response.get("definitely_no_external_effect") is not True
        assert calls == ["deleteWebhook", "getUpdates"]

    def test_action_save_discovered_chat_rejects_existing_chat_duplicate(self):
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
                    "ingress_config": {},
                }

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            telegram_main, "_runtime_logger", lambda *_args, **_kwargs: _FakeLogger()
        )
        try:
            resp, code = telegram_main._handle_action(
                {
                    "action_id": "save_discovered_chat",
                    "context": {"project_id": "proj-7"},
                    "target": {"chat_id": "-100123", "title": "Ops"},
                    "data": {"chat_id": "-100123", "title": "Ops"},
                }
            )
        finally:
            monkeypatch.undo()

        assert code == 0
        assert resp["ok"] is False
        assert resp["error"] == "channel_already_exists"

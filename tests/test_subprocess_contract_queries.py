from __future__ import annotations

from datetime import UTC, datetime

import pytest
from subprocess_contract_helpers import _invoke


class TestTelegramQueries:
    def test_get_secrets_reports_only_project_scoped_token(self):
        import main as telegram_main

        calls = []

        class _FakeLogger:
            def get_provider_secret(self, **kwargs):
                calls.append(kwargs)
                return "global-token" if kwargs["include_global"] else ""

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            telegram_main, "_runtime_logger", lambda *_args, **_kwargs: _FakeLogger()
        )
        try:
            response, code = telegram_main._handle_query(
                {"query_id": "get_secrets", "context": {"project_id": "project-b"}}
            )
        finally:
            monkeypatch.undo()

        assert code == 0
        assert response["summary"]["bot_token_configured"] is False
        assert calls[0]["include_global"] is False

    def test_query_provider_health(self):
        resp, code = _invoke({"mode": "query", "query": {"query_id": "provider_health"}})
        assert code == 0
        assert resp["ok"] is True
        assert "provider" in resp.get("summary", {})

    def test_query_delivery_summary(self):
        resp, code = _invoke({"mode": "query", "query": {"query_id": "delivery_summary"}})
        assert code == 0
        assert resp["ok"] is True

    def test_query_list_deliveries(self):
        resp, code = _invoke({"mode": "query", "query": {"query_id": "list_deliveries"}})
        assert code == 0
        assert resp["ok"] is True
        assert "items" in resp

    def test_query_list_deliveries_serializes_datetime_rows(self):
        import main as telegram_main

        class _FakeLogger:
            def list_notification_delivery_logs(
                self, project_id: str, limit: int = 2000
            ) -> list[dict]:
                assert project_id == "proj-7"
                return [
                    {
                        "provider_key": "flowsteward.telegram",
                        "delivery_id": "d1",
                        "event_type": "job.failed",
                        "severity": "error",
                        "status": "failed",
                        "created_at": datetime(2026, 5, 9, 12, 30, tzinfo=UTC),
                    }
                ]

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            telegram_main, "_runtime_logger", lambda *_args, **_kwargs: _FakeLogger()
        )
        try:
            resp, code = telegram_main._handle_query(
                {"query_id": "list_deliveries", "context": {"project_id": "proj-7"}}
            )
        finally:
            monkeypatch.undo()

        assert code == 0
        assert resp["ok"] is True
        assert resp["items"][0]["delivered_at"] == "2026-05-09T12:30:00+00:00"

    def test_query_list_deliveries_includes_provider_message_id(self):
        import main as telegram_main

        class _FakeLogger:
            def list_notification_delivery_logs(
                self, project_id: str, limit: int = 2000
            ) -> list[dict]:
                assert project_id == "proj-7"
                return [
                    {
                        "provider_key": "flowsteward.telegram",
                        "delivery_id": "d1",
                        "event_type": "job.failed",
                        "severity": "error",
                        "status": "failed",
                        "provider_message_id": "45",
                        "created_at": datetime(2026, 5, 9, 12, 30, tzinfo=UTC),
                    }
                ]

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            telegram_main, "_runtime_logger", lambda *_args, **_kwargs: _FakeLogger()
        )
        try:
            resp, code = telegram_main._handle_query(
                {"query_id": "list_deliveries", "context": {"project_id": "proj-7"}}
            )
        finally:
            monkeypatch.undo()

        assert code == 0
        assert resp["ok"] is True
        assert resp["items"][0]["provider_message_id"] == "45"

    def test_query_list_routes(self):
        resp, code = _invoke({"mode": "query", "query": {"query_id": "list_routes"}})
        assert code == 0
        assert resp["ok"] is True

    def test_query_list_channels_adds_ui_metadata_without_requiring_it_in_storage(self):
        resp, code = _invoke(
            {
                "mode": "query",
                "runtime_context": {
                    "resources": {
                        "project_connection": {
                            "project_id": "proj-7",
                            "extension_id": "flowsteward.telegram",
                            "connection_config": {
                                "channels": [
                                    {
                                        "channel_id": "ops",
                                        "display_name": "Ops",
                                        "chat_id": "-100123",
                                        "enabled": True,
                                        "capabilities": {
                                            "notify": True,
                                            "human_decision": False,
                                        },
                                    }
                                ]
                            },
                        }
                    }
                },
                "query": {"query_id": "list_channels", "context": {"project_id": "proj-7"}},
            }
        )

        assert code == 0
        assert resp["items"][0]["_meta"] == {
            "existing": True,
            "immutable_fields": ["channel_id"],
            "original_channel_id": "ops",
        }

    def test_query_unknown_returns_error(self):
        resp, code = _invoke({"mode": "query", "query": {"query_id": "nonexistent"}})
        assert code == 2
        assert resp["ok"] is False

from __future__ import annotations

import main_state
from main_state import (
    _build_hitl_job_ref,
    _telegram_connection_row_for_project,
)


def test_telegram_connection_list_fallback_uses_canonical_extension_id() -> None:
    class BusinessLogic:
        def list_project_connections(
            self, project_id: str, enabled_only: bool = True
        ) -> list[dict]:
            assert project_id == "project-1"
            assert enabled_only is False
            return [
                {
                    "extension_id": "telegram",
                    "connection_config": {"source": "legacy"},
                },
                {
                    "extension_id": "flowsteward.telegram",
                    "connection_config": {"source": "canonical"},
                },
            ]

    row = _telegram_connection_row_for_project(BusinessLogic(), "project-1")

    assert row["connection_config"] == {"source": "canonical"}


def test_build_hitl_job_ref_uses_urlsafe_random_token(monkeypatch) -> None:
    calls: list[int] = []

    def fake_token_urlsafe(nbytes: int) -> str:
        calls.append(nbytes)
        return f"random-token-{len(calls)}"

    monkeypatch.setattr(main_state.secrets, "token_urlsafe", fake_token_urlsafe)

    first = _build_hitl_job_ref("project-1", "job-1")
    second = _build_hitl_job_ref("project-1", "job-1")

    assert first == "random-token-1"
    assert second == "random-token-2"
    assert calls == [16, 16]

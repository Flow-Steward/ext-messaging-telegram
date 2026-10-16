"""Shared helpers for Telegram subprocess contract tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

BUNDLE_ROOT = Path(__file__).resolve().parents[1]

HOST_CONTRACT_VERSION = "extension_host_v2"
# The final extension_host_v2 execution scope: all four keys, at most one owner.
# A query, a UI action or a workflow-level event names no step.
NO_STEP_SCOPE: dict[str, Any] = {
    "group_id": None,
    "for_each_id": None,
    "step_id": None,
    "step_execution_id": None,
}
ROOT_STEP_SCOPE: dict[str, Any] = {
    **NO_STEP_SCOPE,
    "step_id": "notify_team",
    "step_execution_id": "5d1c6a3e-2f4b-4c8e-9a1d-7e0f3b2c0001",
}
GROUP_STEP_SCOPE: dict[str, Any] = {**ROOT_STEP_SCOPE, "group_id": "review_changes"}
FOR_EACH_STEP_SCOPE: dict[str, Any] = {
    **ROOT_STEP_SCOPE,
    "for_each_id": "each_order",
    "step_execution_id": "5d1c6a3e-2f4b-4c8e-9a1d-7e0f3b2c0002",
}


def host_envelope(request: dict[str, Any], scope: dict[str, Any] | None = None) -> dict[str, Any]:
    """One request as the host sends it: the extension_host_v2 contract and scope.

    The request's own ``runtime_context`` keys win; the execution scope comes
    from ``scope`` (no step by default) unless the request names it.
    """
    envelope = dict(request)
    envelope.setdefault("contract_version", HOST_CONTRACT_VERSION)
    runtime_context = request.get("runtime_context")
    envelope["runtime_context"] = {
        **(NO_STEP_SCOPE if scope is None else scope),
        **(runtime_context if isinstance(runtime_context, dict) else {}),
    }
    return envelope


def _manifest() -> dict[str, Any]:
    return yaml.safe_load((BUNDLE_ROOT / "extension.yaml").read_text()) or {}


def _invoke(
    request: dict[str, Any], timeout: int = 5, *, envelope: bool = True
) -> tuple[dict[str, Any], int]:
    """Run main.py on one request; ``envelope=False`` sends it exactly as given."""
    proc = subprocess.run(
        [sys.executable, str(BUNDLE_ROOT / "main.py")],
        input=json.dumps(host_envelope(request) if envelope else request),
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(BUNDLE_ROOT),
    )
    try:
        response = json.loads(proc.stdout.strip()) if proc.stdout.strip() else {}
    except json.JSONDecodeError:
        response = {"raw_stdout": proc.stdout}
    return response, proc.returncode

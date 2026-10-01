"""Shared helpers for Telegram subprocess contract tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

BUNDLE_ROOT = Path(__file__).resolve().parents[1]


def _manifest() -> dict[str, Any]:
    return yaml.safe_load((BUNDLE_ROOT / "extension.yaml").read_text()) or {}


def _invoke(request: dict[str, Any], timeout: int = 5) -> tuple[dict[str, Any], int]:
    proc = subprocess.run(
        [sys.executable, str(BUNDLE_ROOT / "main.py")],
        input=json.dumps(request),
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

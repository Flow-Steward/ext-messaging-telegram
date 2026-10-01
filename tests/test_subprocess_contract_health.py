from __future__ import annotations

import json
import subprocess
import sys

from subprocess_contract_helpers import BUNDLE_ROOT


class TestTelegramHealthCheck:
    def test_health_check_returns_healthy(self):
        proc = subprocess.run(
            [sys.executable, str(BUNDLE_ROOT / "health.py")],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(BUNDLE_ROOT),
        )
        assert proc.returncode == 0
        data = json.loads(proc.stdout.strip())
        assert data["status"] == "healthy"
        assert data["provider"] == "telegram"

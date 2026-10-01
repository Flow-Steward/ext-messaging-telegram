#!/usr/bin/env python3
"""Telegram extension health check.

Validates that the extension entrypoint is importable and the subprocess
contract is functional. Does NOT check bot token validity (that requires
runtime secrets which are not available at health-check time).
"""

import json


def main() -> int:
    try:
        import importlib.util
        from pathlib import Path

        main_path = Path(__file__).parent / "main.py"
        if not main_path.exists():
            print(json.dumps({"status": "failed", "detail": "main.py not found"}))
            return 1

        spec = importlib.util.spec_from_file_location("telegram_main", str(main_path))
        if spec is None or spec.loader is None:
            print(json.dumps({"status": "failed", "detail": "main.py not loadable"}))
            return 1

        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        if not hasattr(mod, "main") or not callable(mod.main):
            print(json.dumps({"status": "failed", "detail": "main.py missing main()"}))
            return 1

        print(json.dumps({"status": "healthy", "provider": "telegram"}))
        return 0

    except Exception as exc:
        print(json.dumps({"status": "failed", "detail": str(exc)[:200]}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

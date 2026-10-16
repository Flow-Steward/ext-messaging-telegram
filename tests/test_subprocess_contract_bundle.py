from __future__ import annotations

import yaml
from subprocess_contract_helpers import (
    BUNDLE_ROOT,
    _manifest,
)


class TestTelegramBundleValidity:
    def test_manifest_has_schemas(self):
        manifest = _manifest()
        assert "schemas" in manifest
        assert "config" in manifest["schemas"]
        assert "secrets" in manifest["schemas"]
        assert "subscriptions" in manifest["schemas"]

    def test_manifest_declares_updates_callback(self):
        manifest = _manifest()
        callbacks = manifest.get("callbacks") or []
        updates = [item for item in callbacks if item.get("callback_id") == "updates"]
        assert updates
        assert updates[0]["action_id"] == "ingress_event"
        assert updates[0]["auth"]["type"] == "shared_secret_header"

    def test_reference_manifest_is_yaml(self):
        assert (BUNDLE_ROOT / "extension.yaml").is_file()

    def test_ui_manifest_has_multiple_pages(self):
        ui = yaml.safe_load((BUNDLE_ROOT / "ui" / "ui_manifest.yaml").read_text()) or {}
        pages = ui.get("pages") or []
        assert len(pages) == 5
        page_ids: set[str] = set()
        for page in pages:
            if not isinstance(page, dict):
                continue
            page_id = str(page.get("page_id") or "").strip()
            if not page_id:
                ref = str(page.get("ref") or "").strip()
                if ref:
                    ref_path = (BUNDLE_ROOT / "ui" / ref).resolve()
                    if ref_path.exists():
                        loaded = yaml.safe_load(ref_path.read_text()) or {}
                        page_id = str((loaded or {}).get("page_id") or "").strip()
            if page_id:
                page_ids.add(page_id)
        assert "overview" in page_ids
        assert "deliveries" in page_ids
        assert {"setup", "chats_channels", "inbound"} <= page_ids

    def test_schemas_are_valid_json_schema(self):
        for schema_file in (BUNDLE_ROOT / "schemas").glob("*.schema.yaml"):
            data = yaml.safe_load(schema_file.read_text())
            assert data.get("type") == "object"
            assert "properties" in data

    def test_has_no_forbidden_json_contract_files(self):
        forbidden = [
            path.relative_to(BUNDLE_ROOT).as_posix()
            for path in BUNDLE_ROOT.rglob("*.json")
            if path.name in {"extension.json", "manifest.json"}
            or path.relative_to(BUNDLE_ROOT).match("schemas/*.schema.json")
            or path.relative_to(BUNDLE_ROOT).match("ui/ui_manifest.json")
            or path.relative_to(BUNDLE_ROOT).match("ui/actions/actions.json")
            or path.relative_to(BUNDLE_ROOT).match("ui/queries/queries.json")
            or path.relative_to(BUNDLE_ROOT).match("ui/pages/*.json")
            or path.relative_to(BUNDLE_ROOT).match("ui/components/*.json")
        ]
        assert forbidden == []

    def test_bundle_carries_a_valid_logo(self):
        # Core requires icon.png next to extension.yaml: PNG, square, 32-512 px, <= 100 KB.
        icon = (BUNDLE_ROOT / "icon.png").read_bytes()
        assert icon[:8] == b"\x89PNG\r\n\x1a\n"
        assert icon[12:16] == b"IHDR"
        width = int.from_bytes(icon[16:20], "big")
        height = int.from_bytes(icon[20:24], "big")
        assert width == height
        assert 32 <= width <= 512
        assert len(icon) <= 100 * 1024

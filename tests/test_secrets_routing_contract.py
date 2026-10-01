from __future__ import annotations

import yaml
from subprocess_contract_helpers import BUNDLE_ROOT

_PROVIDER_KEY = "flowsteward.telegram"


def _save_secrets_handler() -> dict:
    payload = yaml.safe_load((BUNDLE_ROOT / "ui/actions/actions.yaml").read_text())
    actions = {row["action_id"]: row for row in payload["actions"]}
    return actions["save_secrets"]["handler"]


def test_save_secrets_binds_the_token_to_a_project_connection() -> None:
    # A token saved through the platform-only `provider_secrets_save` never
    # created the extension's project connection, so the runtime kept reporting
    # the bot as unconfigured right after the operator saved it.
    handler = _save_secrets_handler()

    assert handler["mode"] == "platform"
    assert handler["operation"] == "project_connection_secrets_save"


def test_save_secrets_declares_the_telegram_bot_connection_contract() -> None:
    config = _save_secrets_handler()["config"]

    assert config["provider_key"] == _PROVIDER_KEY
    assert config["connection_type_id"] == "telegram_bot"
    assert config["secret_fields"] == [{"input": "bot_token", "secret_key": "bot_token"}]


def _save_inbound_settings_handler() -> dict:
    payload = yaml.safe_load((BUNDLE_ROOT / "ui/actions/actions.yaml").read_text())
    actions = {row["action_id"]: row for row in payload["actions"]}
    return actions["save_inbound_settings"]["handler"]


def test_save_inbound_settings_writes_the_partition_the_runtime_reads() -> None:
    # The runtime reads inbound mode and polling interval from `ingress_config`;
    # the configuration patch wrote them into `connection_config`.
    handler = _save_inbound_settings_handler()

    assert handler["mode"] == "platform"
    assert handler["operation"] == "project_connection_ingress_patch"
    assert handler["config"]["connection_type_id"] == "telegram_bot"
    assert handler["config"]["fields"] == ["inbound_mode", "polling_interval_seconds"]


def test_save_config_declares_the_install_bound_connection_type() -> None:
    payload = yaml.safe_load((BUNDLE_ROOT / "ui/actions/actions.yaml").read_text())
    actions = {row["action_id"]: row for row in payload["actions"]}

    handler = actions["save_config"]["handler"]

    assert handler["operation"] == "project_connection_config_patch"
    assert handler["config"]["connection_type_id"] == "telegram_bot"


def test_ingress_event_declares_the_project_scoped_bot_token() -> None:
    payload = yaml.safe_load((BUNDLE_ROOT / "ui/actions/actions.yaml").read_text())
    actions = {row["action_id"]: row for row in payload["actions"]}

    resources = actions["ingress_event"]["handler"]["config"]["runtime_resources"]

    assert resources["provider_secrets"] == ["bot_token"]

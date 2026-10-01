"""One dispatch path, complete declarations.

The compiled action manifest is authoritative. Two things follow, and both are
enforced here against the real subprocess source rather than a hand-kept list:

* an action the manifest routes to a platform operation has no subprocess
  branch — a branch Core never dispatches to is a hidden second implementation;
* an action whose subprocess branch can write host state declares that write as
  a host mutation and is marked platform-mutating. Core rejects an undeclared
  mutation after the external Telegram call has already happened.
"""

from __future__ import annotations

import ast
import sys

import pytest
import yaml
from subprocess_contract_helpers import BUNDLE_ROOT, _invoke

if str(BUNDLE_ROOT) not in sys.path:
    sys.path.insert(0, str(BUNDLE_ROOT))

# Helpers reachable from an action branch that end in a host write, and the
# mutation kind Core sees for each. `_poll_updates` saves ingress state inside
# `main_polling`; the rest are the direct bridge writes.
_WRITE_HELPERS = {
    "_save_telegram_connection_ingress": "project_connection_upsert",
    "_save_telegram_connection_config": "project_connection_upsert",
    "_save_telegram_connection_secret_refs": "project_connection_upsert",
    "upsert_project_connection": "project_connection_upsert",
    "_poll_updates": "project_connection_upsert",
    "log_notification_delivery": "notification_delivery_log",
    "save_notification_routes": "notification_routes_save",
}


def _manifest_actions() -> dict[str, dict]:
    payload = yaml.safe_load((BUNDLE_ROOT / "ui/actions/actions.yaml").read_text())
    return {row["action_id"]: row for row in payload["actions"]}


def _literal_transport_error_codes() -> set[str]:
    codes: set[str] = set()
    tree = ast.parse((BUNDLE_ROOT / "main_transport.py").read_text())
    send_message = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_send_message"
    )
    for node in ast.walk(send_message):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if not (
                isinstance(key, ast.Constant)
                and key.value == "error_code"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                continue
            codes.add(value.value)
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "error_code":
                assert not isinstance(value, ast.JoinedStr), (
                    "Telegram transport error_code must use a finite canonical value"
                )
    return codes


def _declared_mutations(action: dict) -> set[str]:
    config = (action.get("handler") or {}).get("config") or {}
    return {str(kind) for kind in config.get("host_mutations") or []}


def _delegated_action_ids(node: ast.AST) -> set[str]:
    delegated: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call) or not isinstance(sub.func, ast.Name):
            continue
        if sub.func.id != "_handle_action" or not sub.args:
            continue
        payload = sub.args[0]
        if not isinstance(payload, ast.Dict):
            continue
        for key, value in zip(payload.keys, payload.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and key.value == "action_id"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                delegated.add(value.value)
    return delegated


def _subprocess_branches() -> dict[str, set[str]]:
    """action_id -> transitive mutation kinds its `_handle_action` branch can emit."""
    tree = ast.parse((BUNDLE_ROOT / "main_actions.py").read_text())
    handler = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_handle_action"
    )
    direct_mutations: dict[str, set[str]] = {}
    delegated_actions: dict[str, set[str]] = {}
    for node in handler.body:
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        if not (isinstance(node.test.left, ast.Name) and node.test.left.id == "action_id"):
            continue
        (comparator,) = node.test.comparators
        if isinstance(comparator, ast.Constant):
            ids = [str(comparator.value)]
        else:
            ids = [str(item.value) for item in comparator.elts if isinstance(item, ast.Constant)]
        emitted: set[str] = set()
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in _WRITE_HELPERS:
                emitted.add(_WRITE_HELPERS[name])
        for action_id in ids:
            direct_mutations[action_id] = emitted
            delegated_actions[action_id] = _delegated_action_ids(node)
    assert direct_mutations, "no action branches found in _handle_action"

    resolved: dict[str, set[str]] = {}

    def mutation_closure(action_id: str, visiting: frozenset[str] = frozenset()) -> set[str]:
        if action_id in resolved:
            return resolved[action_id]
        assert action_id not in visiting, f"recursive action delegation involving {action_id}"
        emitted = set(direct_mutations.get(action_id, set()))
        for delegated_action_id in delegated_actions.get(action_id, set()):
            assert delegated_action_id in direct_mutations, (
                f"{action_id} delegates to unknown subprocess action {delegated_action_id}"
            )
            emitted.update(mutation_closure(delegated_action_id, visiting | frozenset({action_id})))
        resolved[action_id] = emitted
        return emitted

    return {action_id: mutation_closure(action_id) for action_id in direct_mutations}


def test_platform_routed_actions_have_no_subprocess_branch() -> None:
    actions = _manifest_actions()
    branches = _subprocess_branches()

    platform_routed = {aid for aid, row in actions.items() if row["handler"]["mode"] == "platform"}
    assert sorted(set(branches) & platform_routed) == []
    # `deliver_notification` is the delivery entry Core invokes outside the action
    # manifest; every other branch must be a manifest action.
    assert sorted(set(branches) - set(actions) - {"deliver_notification"}) == []


def test_every_write_path_is_declared_and_marked_platform_mutating() -> None:
    actions = _manifest_actions()
    undeclared: dict[str, list[str]] = {}
    unmarked: list[str] = []
    for action_id, emitted in _subprocess_branches().items():
        row = actions.get(action_id)
        if row is None or not emitted:
            continue
        missing = sorted(emitted - _declared_mutations(row))
        if missing:
            undeclared[action_id] = missing
        if row.get("mutates_platform") is not True:
            unmarked.append(action_id)

    assert undeclared == {}
    assert unmarked == []


@pytest.mark.parametrize(
    ("action_id", "request_payload", "expected_code"),
    [
        (
            "deliver_notification",
            {
                "mode": "action",
                "action": {
                    "action_id": "deliver_notification",
                    "context": {"project_id": "proj-7"},
                },
            },
            "project_connection_required",
        ),
        (
            "test_delivery",
            {
                "mode": "action",
                "action": {
                    "action_id": "test_delivery",
                    "context": {"project_id": "proj-7"},
                },
            },
            "channel_required",
        ),
        (
            "resolve_chat",
            {
                "mode": "action",
                "action": {
                    "action_id": "resolve_chat",
                    "context": {"project_id": "proj-7"},
                },
            },
            "project_connection_storage_unavailable",
        ),
        (
            "ingress_event",
            {
                "mode": "action",
                "action": {
                    "action_id": "ingress_event",
                    "input": {"request": {"payload": {}}},
                },
            },
            "unauthorized_telegram_ingress",
        ),
    ],
)
def test_real_action_errors_use_the_declared_closed_vocabulary(
    action_id: str,
    request_payload: dict,
    expected_code: str,
) -> None:
    response, _exit_code = _invoke(request_payload)
    declared = set(_manifest_actions()[action_id].get("error_codes") or [])

    assert response["error_code"] == expected_code
    assert expected_code in declared


def test_every_fixed_transport_error_is_declared_by_each_sending_action() -> None:
    import main as telegram_main

    actions = _manifest_actions()
    transport_codes = _literal_transport_error_codes()

    assert transport_codes
    assert transport_codes <= set(telegram_main._ACTION_ERROR_MESSAGES)
    for action_id in ("deliver_notification", "test_delivery", "retry_delivery"):
        declared = set(actions[action_id].get("error_codes") or [])
        assert transport_codes <= declared, action_id


# --- real action paths -------------------------------------------------------


def _connection() -> dict:
    return {
        "connection_id": "conn-7",
        "project_id": "proj-7",
        "account_id": "acct-7",
        "extension_id": "flowsteward.telegram",
        "extension_install_id": "install-7",
        "connection_type": "telegram_bot",
        "enabled": True,
        "connection_config": {"channels": []},
        "secret_refs": {
            "provider_secrets": {
                "provider_key": "flowsteward.telegram",
                "keys": {"bot_token": "bot_token"},
            }
        },
        "ingress_config": {"inbound_mode": "webhook", "polling_interval_seconds": 60},
    }


@pytest.fixture
def telegram_api(monkeypatch):
    """The Telegram Bot API, answering `setWebhook` and `getWebhookInfo` with success."""
    import main_actions
    import main_state

    calls: list[str] = []

    def _api(token: str, method_name: str, payload: dict | None = None) -> dict:
        calls.append(method_name)
        if method_name == "setWebhook":
            return {"ok": True, "result": True}
        if method_name == "getWebhookInfo":
            return {
                "ok": True,
                "result": {"url": "https://example.test/hook", "pending_update_count": 0},
            }
        return {"ok": False, "description": f"unexpected {method_name}"}

    monkeypatch.setattr(main_actions, "_telegram_api_call", _api)
    main_state._REQUEST_HOST_MUTATIONS.clear()
    yield calls
    main_state._REQUEST_HOST_MUTATIONS.clear()


def _run(action_id: str, payload: dict) -> tuple[dict, set[str]]:
    import main_actions
    import main_state

    response, code = main_actions._handle_action(
        {
            "action_id": action_id,
            "context": {"project_id": "proj-7"},
            "input": payload,
            "runtime_context": {
                "resources": {
                    "project_connection": _connection(),
                    "provider_secrets": {"bot_token": "fake-token"},
                }
            },
        }
    )
    assert code == 0, response
    return response, {row["kind"] for row in main_state._REQUEST_HOST_MUTATIONS}


def test_configure_webhook_emits_only_declared_mutations(telegram_api) -> None:
    response, emitted = _run(
        "configure_webhook", {"callback_url": "https://example.test/hook", "callback_secret": "s"}
    )

    assert telegram_api == ["setWebhook"]
    assert response["ok"] is True
    assert emitted == {"project_connection_upsert"}
    assert emitted <= _declared_mutations(_manifest_actions()["configure_webhook"])

    import main_state

    mutation = next(
        row
        for row in main_state._REQUEST_HOST_MUTATIONS
        if row["kind"] == "project_connection_upsert"
    )
    payload = mutation["payload"]
    assert payload["account_id"] == "acct-7"
    assert payload["extension_install_id"] == "install-7"
    assert payload["connection_type"] == "telegram_bot"
    assert payload["secret_refs"] == _connection()["secret_refs"]


def test_get_webhook_status_emits_only_declared_mutations(telegram_api) -> None:
    response, emitted = _run("get_webhook_status", {})

    assert telegram_api == ["getWebhookInfo"]
    assert response["ok"] is True
    assert emitted == {"project_connection_upsert"}
    assert emitted <= _declared_mutations(_manifest_actions()["get_webhook_status"])


def test_platform_mutating_actions_route_through_the_command_bus() -> None:
    # The validator requires it; a platform-mutating action without a command
    # type is rejected at install, after the manifest tests were already green.
    missing = sorted(
        action_id
        for action_id, row in _manifest_actions().items()
        if row.get("mutates_platform") is True
        and row.get("command_type") != "invoke_extension_action"
    )
    assert missing == []

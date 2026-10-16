#!/usr/bin/env python3
"""Telegram notification provider extension entrypoint.

Subprocess contract:
  - Reads JSON from stdin
  - Writes JSON to stdout
  - Exit 0 = success, 2 = bad request, 1 = internal error
"""

from __future__ import annotations

import json
import re
import sys
from types import ModuleType

from flowsteward_extension_sdk import runtime_context_from_payload

import main_state as _state
from main_actions import (
    _handle_action as _handle_action,
)
from main_delivery import (
    _deliver as _deliver,
)
from main_ingress import (
    _handle_ingress_event_payload as _handle_ingress_event_payload,
)
from main_ingress import (
    _ingress_actor_from_user as _ingress_actor_from_user,
)
from main_ingress import (
    _ingress_event_response as _ingress_event_response,
)
from main_ingress import (
    _ingress_interaction_payload as _ingress_interaction_payload,
)
from main_ingress import (
    _telegram_ingress_authenticated as _telegram_ingress_authenticated,
)
from main_polling import (
    _poll_updates as _poll_updates,
)
from main_queries import (
    _handle_query as _handle_query,
)
from main_state import (
    _POLLING_INTERVAL_OPTIONS as _POLLING_INTERVAL_OPTIONS,
)
from main_state import (
    _REQUEST_RUNTIME_CONTEXT as _REQUEST_RUNTIME_CONTEXT,
)
from main_state import (
    _SUPPORTED_UPDATE_TYPES as _SUPPORTED_UPDATE_TYPES,
)
from main_state import (
    APPLICATION_JSON_VALUE as APPLICATION_JSON_VALUE,
)
from main_state import (
    APPROVAL_REQUESTED_VALUE as APPROVAL_REQUESTED_VALUE,
)
from main_state import (
    JOB_AWAITING_APPROVAL_VALUE as JOB_AWAITING_APPROVAL_VALUE,
)
from main_state import (
    _active_transport_for_state as _active_transport_for_state,
)
from main_state import (
    _annotate_discovered_chats as _annotate_discovered_chats,
)
from main_state import (
    _apply_grid_params as _apply_grid_params,
)
from main_state import (
    _as_dict as _as_dict,
)
from main_state import (
    _as_job_id as _as_job_id,
)
from main_state import (
    _as_list as _as_list,
)
from main_state import (
    _as_str as _as_str,
)
from main_state import (
    _build_callback_data as _build_callback_data,
)
from main_state import (
    _build_hitl_job_ref as _build_hitl_job_ref,
)
from main_state import (
    _callback_registration_for_project as _callback_registration_for_project,
)
from main_state import (
    _channel_items_for_ui as _channel_items_for_ui,
)
from main_state import (
    _channel_items_from_config as _channel_items_from_config,
)
from main_state import (
    _chat_row_from_telegram_chat as _chat_row_from_telegram_chat,
)
from main_state import (
    _coerce_bool as _coerce_bool,
)
from main_state import (
    _coerce_int as _coerce_int,
)
from main_state import (
    _coerce_positive_int as _coerce_positive_int,
)
from main_state import (
    _coerce_project_id as _coerce_project_id,
)
from main_state import (
    _consume_pending_hitl_token as _consume_pending_hitl_token,
)
from main_state import (
    _discovered_chat_row as _discovered_chat_row,
)
from main_state import (
    _event_label as _event_label,
)
from main_state import (
    _html_escape as _html_escape,
)
from main_state import (
    _humanize_token as _humanize_token,
)
from main_state import (
    _job_context as _job_context,
)
from main_state import (
    _job_url as _job_url,
)
from main_state import (
    _json_safe_value as _json_safe_value,
)
from main_state import (
    _looks_like_uuid as _looks_like_uuid,
)
from main_state import (
    _message_context as _message_context,
)
from main_state import (
    _normalize_chat_link as _normalize_chat_link,
)
from main_state import (
    _normalize_chat_username as _normalize_chat_username,
)
from main_state import (
    _normalize_inbound_state as _normalize_inbound_state,
)
from main_state import (
    _normalize_polling_interval as _normalize_polling_interval,
)
from main_state import (
    _parse_approval_command as _parse_approval_command,
)
from main_state import (
    _parse_callback_data as _parse_callback_data,
)
from main_state import (
    _parse_reject_prompt_reply as _parse_reject_prompt_reply,
)
from main_state import (
    _provider_defaults_from_config as _provider_defaults_from_config,
)
from main_state import (
    _register_pending_hitl_token as _register_pending_hitl_token,
)
from main_state import (
    _replace_channels_in_config as _replace_channels_in_config,
)
from main_state import (
    _resolve_pending_hitl_token as _resolve_pending_hitl_token,
)
from main_state import (
    _resolve_runtime_logger as _resolve_runtime_logger,
)
from main_state import (
    _runtime_logger as _runtime_logger,
)
from main_state import (
    _safe_iso_datetime as _safe_iso_datetime,
)
from main_state import (
    _save_telegram_connection as _save_telegram_connection,
)
from main_state import (
    _save_telegram_connection_config as _save_telegram_connection_config,
)
from main_state import (
    _save_telegram_connection_ingress as _save_telegram_connection_ingress,
)
from main_state import (
    _save_telegram_connection_secret_refs as _save_telegram_connection_secret_refs,
)
from main_state import (
    _slugify_channel_id as _slugify_channel_id,
)
from main_state import (
    _telegram_connection_row_for_project as _telegram_connection_row_for_project,
)
from main_state import (
    _upsert_discovered_chat as _upsert_discovered_chat,
)
from main_state import (
    _webhook_projection as _webhook_projection,
)
from main_state import (
    _workflow_display_name as _workflow_display_name,
)
from main_state import (
    _workflow_url as _workflow_url,
)
from main_transport import (
    _answer_callback_query as _answer_callback_query,
)
from main_transport import (
    _approval_reply_markup as _approval_reply_markup,
)
from main_transport import (
    _discover_chats_from_update as _discover_chats_from_update,
)
from main_transport import (
    _provider_runtime_config as _provider_runtime_config,
)
from main_transport import (
    _render_message as _render_message,
)
from main_transport import (
    _required_channel_capability as _required_channel_capability,
)
from main_transport import (
    _resolve_channel as _resolve_channel,
)
from main_transport import (
    _retry_error_message as _retry_error_message,
)
from main_transport import (
    _retryable_delivery_status as _retryable_delivery_status,
)
from main_transport import (
    _send_message as _send_message,
)
from main_transport import (
    _send_reject_reason_prompt as _send_reject_reason_prompt,
)
from main_transport import (
    _telegram_api_call as _telegram_api_call,
)
from main_transport import (
    _telegram_delete_webhook as _telegram_delete_webhook,
)
from main_transport import (
    _telegram_get_chat as _telegram_get_chat,
)
from main_transport import (
    _telegram_get_me as _telegram_get_me,
)
from main_transport import (
    _telegram_get_updates as _telegram_get_updates,
)
from main_transport import (
    _telegram_token_for_project as _telegram_token_for_project,
)

_SPLIT_MODULE_NAMES = (
    "main_actions",
    "main_delivery",
    "main_ingress",
    "main_polling",
    "main_queries",
    "main_state",
    "main_transport",
)

_ACTION_ERROR_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
_ACTION_ERROR_MESSAGES = {
    "bot_token_required": "Save a bot token before running this Telegram action.",  # nosec B105 - an error message, not a secret
    "bot_token_or_chat_id_required": "A saved bot token and Telegram channel are required.",  # nosec B105 - an error message, not a secret
    "bot_token_rejected": "Telegram rejected the saved bot token. Replace it and try again.",  # nosec B105 - an error message, not a secret
    "callback_url_required": "Enter a callback URL before configuring the webhook.",
    "channel_already_exists": "This Telegram chat is already saved as a channel.",
    "channel_id_duplicate": "Choose a unique Telegram channel ID.",
    "channel_required": "Add and enable a Telegram channel before sending a notification.",
    "chat_not_resolved": "Telegram could not resolve the selected chat.",
    "cross_project_human_decision": "This Telegram decision belongs to another project.",
    "delivery_not_found": "The selected Telegram delivery could not be found.",
    "delivery_not_retryable": "Only failed or skipped Telegram deliveries can be retried.",
    "delivery_retry_unavailable": "Telegram delivery retry is unavailable right now.",
    "invalid_payload": "The Telegram action input is invalid.",
    "invalid_reject_comment_context": "Reply to the current Telegram rejection prompt.",
    "no_matching_routes_found": "No matching notification routes were found.",
    "notification_route_storage_unavailable": "Notification route storage is unavailable right now.",
    "polling_unavailable": "Telegram polling is unavailable right now.",
    "prepared_delivery_required": "Prepare this approval delivery before sending it.",
    "project_connection_required": "Select a Telegram connection before running this action.",
    "project_connection_storage_unavailable": "Telegram project storage is unavailable right now.",
    "project_context_required": "Select a project before running this Telegram action.",
    "telegram_unreachable": "Telegram could not be reached. Try again.",
    "telegram_reply_target_invalid": "The Telegram message being replied to is no longer available.",
    "telegram_send_ambiguous": "Telegram may have accepted the message; check delivery history before retrying.",
    "telegram_send_rejected": "Telegram rejected the message. Check the bot and channel configuration.",
    "telegram_send_request_invalid": "Flow Steward could not prepare the Telegram request.",
    "toggle_route_requires_target_item_ids": "Select at least one notification route.",
    "unauthorized_human_decision_channel": "This chat is not authorized for Telegram decisions.",
    "unauthorized_telegram_ingress": "Telegram could not authenticate this inbound request.",
}


def _public_response(value: dict) -> dict:
    """Return one canonical public envelope from every Telegram boundary."""
    result = dict(value)
    if result.get("ok") is not False:
        return result
    nested = _as_dict(result.get("result"))
    declared_errors = _as_list(result.get("errors"))
    raw_error = _as_str(result.get("error"))
    candidates = [
        result.get("error_code"),
        nested.get("error_code"),
        _as_dict(declared_errors[0]).get("code") if declared_errors else "",
        raw_error,
    ]
    error_code = next(
        (
            candidate
            for raw_candidate in candidates
            if (candidate := _as_str(raw_candidate)) and _ACTION_ERROR_CODE_RE.fullmatch(candidate)
        ),
        "telegram_action_failed",
    )
    existing_message = _as_str(result.get("message"))
    if existing_message:
        user_message = existing_message
    elif raw_error and raw_error != error_code:
        user_message = raw_error
    elif error_code.startswith("unknown_action:"):
        user_message = "This Telegram action is not supported."
    else:
        user_message = _ACTION_ERROR_MESSAGES.get(
            error_code,
            "Telegram could not complete this action. Review the technical details and try again.",
        )
    result["error_code"] = error_code
    result["error"] = user_message
    remaining_errors = [dict(item) for item in declared_errors[1:] if isinstance(item, dict)]
    result["errors"] = [
        {"code": error_code, "message": user_message},
        *remaining_errors,
    ]
    return result


def _propagate_split_global(name: str, value) -> None:
    for module_name in _SPLIT_MODULE_NAMES:
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, name):
            setattr(module, name, value)


class _TelegramMainFacadeModule(ModuleType):
    def __setattr__(self, name: str, value) -> None:
        if name == "_REQUEST_RUNTIME_CONTEXT":
            _state._REQUEST_RUNTIME_CONTEXT.clear()
            _state._REQUEST_RUNTIME_CONTEXT.update(_as_dict(value))
            super().__setattr__(name, _state._REQUEST_RUNTIME_CONTEXT)
            _propagate_split_global(name, _state._REQUEST_RUNTIME_CONTEXT)
            return
        super().__setattr__(name, value)
        _propagate_split_global(name, value)


def main() -> int:
    raw = sys.stdin.read().strip()
    try:
        request = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        result = _public_response(
            {
                "ok": False,
                "error_code": "invalid_json",
                "error": f"Malformed JSON input: {exc.msg}",
            }
        )
        print(json.dumps(result))
        return 2
    if not isinstance(request, dict):
        result = _public_response(
            {
                "ok": False,
                "error_code": "invalid_payload",
                "error": "Input payload must be a JSON object.",
            }
        )
        print(json.dumps(result))
        return 2
    # The host envelope is checked before anything is read, sent or saved:
    # another contract, a missing execution-scope key or two owners is refused.
    try:
        runtime_context_from_payload(request)
    except ValueError as exc:
        refused: dict = {
            "ok": False,
            "error_code": "invalid_host_envelope",
            "error": str(exc),
            "definitely_no_external_effect": True,
        }
        if _as_str(request.get("action")) == "deliver_notification":
            refused["delivered"] = False
        print(json.dumps(_public_response(refused)))
        return 2
    _state._REQUEST_RUNTIME_CONTEXT.clear()
    _state._REQUEST_RUNTIME_CONTEXT.update(_as_dict(request.get("runtime_context")))
    _state._REQUEST_HOST_MUTATIONS.clear()

    mode = str(request.get("mode") or "").strip().lower()

    if mode == "query":
        query = _as_dict(request.get("query"))
        result, _code = _handle_query(query)
        result = _public_response(result)
        print(json.dumps(result))
        return 0 if result.get("ok") is True else 2

    if mode == "action":
        action = _as_dict(request.get("action"))
        request_action = _as_dict(_as_dict(request.get("request")).get("action"))
        if request_action:
            action = {**request_action, **action}
        result, _code = _handle_action(action)
        result = _public_response(result)
        if _state._REQUEST_HOST_MUTATIONS:
            result = {**result, "host_mutations": list(_state._REQUEST_HOST_MUTATIONS)}
        print(json.dumps(result))
        return 0 if result.get("ok") is True else 2

    request_kind = _as_str(request.get("action"))
    if request_kind == "deliver_notification":
        event = _as_dict(request.get("event"))
        route = _as_dict(request.get("route"))
        provider = _as_dict(request.get("provider"))
        action_payload = _as_dict(request.get("action_payload"))
        target = _as_dict(action_payload.get("target"))
        connection = _as_dict(target.get("connection"))
        if target:
            _state._REQUEST_RUNTIME_CONTEXT["target"] = target
        if connection:
            provider_config = {
                **_as_dict(connection.get("config")),
                **_as_dict(provider.get("provider_config")),
            }
            provider_config.pop("bot_token", None)
            provider_config.pop("token", None)
            provider = {
                **provider,
                "provider_config": provider_config,
                "provider_secrets": _as_dict(connection.get("secrets")),
                "ingress_config": _as_dict(connection.get("ingress_config"))
                or _as_dict(provider.get("ingress_config")),
            }
        result = _deliver(
            event,
            route,
            provider,
            delivery_phase=_as_str(request.get("delivery_phase")),
            prepared_delivery=_as_dict(request.get("prepared_delivery")),
        )
        result = _public_response(result)
        if _state._REQUEST_HOST_MUTATIONS:
            result = {**result, "host_mutations": list(_state._REQUEST_HOST_MUTATIONS)}
        print(json.dumps(result))
        return 0 if bool(result.get("ok")) else 2

    if request_kind == "ingress_event":
        result, _code = _handle_ingress_event_payload({"request": _as_dict(request.get("request"))})
        result = _public_response(result)
        print(json.dumps(result))
        return 0 if result.get("ok") is True else 2

    error_code = "unsupported_request"
    error = f"Unsupported Telegram request: {mode or request_kind or 'missing mode'}"
    print(json.dumps(_public_response({"ok": False, "error_code": error_code, "error": error})))
    return 2


_current_module = sys.modules.get(__name__)
if _current_module is not None:
    _current_module.__class__ = _TelegramMainFacadeModule

__all__ = [
    "APPLICATION_JSON_VALUE",
    "APPROVAL_REQUESTED_VALUE",
    "JOB_AWAITING_APPROVAL_VALUE",
    "_POLLING_INTERVAL_OPTIONS",
    "_REQUEST_RUNTIME_CONTEXT",
    "_SUPPORTED_UPDATE_TYPES",
    "_active_transport_for_state",
    "_annotate_discovered_chats",
    "_answer_callback_query",
    "_apply_grid_params",
    "_approval_reply_markup",
    "_as_dict",
    "_as_job_id",
    "_as_list",
    "_as_str",
    "_build_callback_data",
    "_build_hitl_job_ref",
    "_callback_registration_for_project",
    "_channel_items_for_ui",
    "_channel_items_from_config",
    "_chat_row_from_telegram_chat",
    "_coerce_bool",
    "_coerce_int",
    "_coerce_positive_int",
    "_coerce_project_id",
    "_consume_pending_hitl_token",
    "_deliver",
    "_discover_chats_from_update",
    "_discovered_chat_row",
    "_event_label",
    "_handle_action",
    "_handle_ingress_event_payload",
    "_handle_query",
    "_html_escape",
    "_humanize_token",
    "_ingress_actor_from_user",
    "_ingress_event_response",
    "_ingress_interaction_payload",
    "_job_context",
    "_job_url",
    "_json_safe_value",
    "_looks_like_uuid",
    "_message_context",
    "_normalize_chat_link",
    "_normalize_chat_username",
    "_normalize_inbound_state",
    "_normalize_polling_interval",
    "_parse_approval_command",
    "_parse_callback_data",
    "_parse_reject_prompt_reply",
    "_poll_updates",
    "_provider_defaults_from_config",
    "_provider_runtime_config",
    "_register_pending_hitl_token",
    "_render_message",
    "_replace_channels_in_config",
    "_required_channel_capability",
    "_resolve_channel",
    "_resolve_pending_hitl_token",
    "_resolve_runtime_logger",
    "_retry_error_message",
    "_retryable_delivery_status",
    "_runtime_logger",
    "_safe_iso_datetime",
    "_save_telegram_connection",
    "_save_telegram_connection_config",
    "_save_telegram_connection_ingress",
    "_save_telegram_connection_secret_refs",
    "_send_message",
    "_send_reject_reason_prompt",
    "_slugify_channel_id",
    "_telegram_api_call",
    "_telegram_connection_row_for_project",
    "_telegram_delete_webhook",
    "_telegram_get_chat",
    "_telegram_get_me",
    "_telegram_get_updates",
    "_telegram_ingress_authenticated",
    "_telegram_token_for_project",
    "_upsert_discovered_chat",
    "_webhook_projection",
    "_workflow_display_name",
    "_workflow_url",
    "main",
]

if __name__ == "__main__":
    raise SystemExit(main())

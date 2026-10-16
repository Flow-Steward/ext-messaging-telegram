from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any

from main_delivery import _deliver
from main_ingress import _handle_ingress_event_payload
from main_polling import _poll_updates
from main_readiness import MESSAGES, classify_bot_api_failure
from main_send import handle_send_message
from main_state import (
    _REQUEST_RUNTIME_CONTEXT,
    PROVIDER_KEY,
    _active_transport_for_state,
    _as_dict,
    _as_list,
    _as_str,
    _callback_registration_for_project,
    _channel_items_for_ui,
    _channel_items_from_config,
    _chat_row_from_telegram_chat,
    _coerce_bool,
    _coerce_int,
    _coerce_project_id,
    _normalize_chat_link,
    _normalize_inbound_state,
    _provider_defaults_from_config,
    _replace_channels_in_config,
    _resolve_runtime_logger,
    _save_telegram_connection_config,
    _save_telegram_connection_ingress,
    _slugify_channel_id,
    _telegram_connection_row_for_project,
    _upsert_discovered_chat,
    _webhook_projection,
)
from main_transport import (
    _retry_error_message,
    _retryable_delivery_status,
    _telegram_api_call,
    _telegram_get_chat,
    _telegram_get_me,
    _telegram_token_for_project,
)


def _handle_action(action: dict) -> tuple[dict, int]:
    action_id = str(action.get("action_id") or "").strip()
    context = _as_dict(action.get("context"))
    target = _as_dict(action.get("target"))
    input_payload = _as_dict(action.get("input"))
    project_id = _coerce_project_id(
        context.get("project_id"),
        target.get("project_id"),
        input_payload.get("project_id"),
    )
    runtime_context = dict(_as_dict(action.get("runtime_context")) or _REQUEST_RUNTIME_CONTEXT)
    if target and "target" not in runtime_context:
        runtime_context["target"] = target
    if action_id == "send_message":
        # A workflow step: everything it needs travels with the action.
        return handle_send_message(action, runtime_context)
    bl = _resolve_runtime_logger(runtime_context)
    connection = (
        _telegram_connection_row_for_project(bl, str(project_id))
        if (bl is not None and project_id)
        else {}
    )
    inbound_state = _normalize_inbound_state(_as_dict(connection.get("ingress_config")))

    if action_id == "deliver_notification":
        target_connection = _as_dict(target.get("connection"))
        if not target_connection:
            return {"ok": False, "error": "project_connection_required"}, 2
        connection_config = _as_dict(
            target_connection.get("connection_config") or target_connection.get("config")
        )
        connection_secrets = _as_dict(target_connection.get("secrets"))
        event = _as_dict(input_payload.get("event"))
        route = _as_dict(input_payload.get("route"))
        result = _deliver(
            event,
            route,
            {
                "provider_config": {
                    **_provider_defaults_from_config(connection_config),
                    "channels": _channel_items_from_config(connection_config),
                },
                "provider_secrets": {"bot_token": _as_str(connection_secrets.get("bot_token"))},
            },
        )
        return {"ok": bool(result.get("ok")), "result": result}, 0

    if action_id == "retry_delivery":
        if not project_id:
            return {"ok": False, "error": "project_context_required"}, 2
        delivery_id = _as_str(
            _as_list(target.get("item_ids"))[0]
            if _as_list(target.get("item_ids"))
            else target.get("delivery_id")
        )
        if not delivery_id:
            return {"ok": False, "error": "invalid_payload"}, 2
        if bl is None or not hasattr(bl, "list_notification_delivery_logs"):
            return {"ok": False, "error": "delivery_retry_unavailable"}, 2
        rows = bl.list_notification_delivery_logs(project_id=str(project_id), limit=2000) or []
        delivery_row = next(
            (
                _as_dict(row)
                for row in rows
                if _as_str(_as_dict(row).get("delivery_id")) == delivery_id
                and _as_str(_as_dict(row).get("provider_key")).lower() == PROVIDER_KEY
            ),
            {},
        )
        if not delivery_row:
            return {"ok": False, "error": "delivery_not_found"}, 2
        original_status = _as_str(delivery_row.get("status")).lower()
        if not _retryable_delivery_status(original_status):
            return {
                "ok": False,
                "error": "delivery_not_retryable",
                "message": "Only failed or skipped deliveries can be retried.",
                "reload": ["delivery_grid"],
                "result": {
                    "ok": False,
                    "delivered": False,
                    "error": "delivery_not_retryable",
                    "original_status": original_status,
                },
            }, 0
        request_payload = _as_dict(delivery_row.get("request"))
        event = _as_dict(request_payload.get("event"))
        route_snapshot = _as_dict(request_payload.get("route"))
        if not event or not route_snapshot:
            return {"ok": False, "error": "invalid_payload"}, 2
        connection = (
            _telegram_connection_row_for_project(bl, str(project_id)) if bl is not None else {}
        )
        config = _as_dict(connection.get("connection_config"))
        channels = _channel_items_from_config(config)
        result = _deliver(
            event,
            route_snapshot,
            {
                "provider_config": {
                    **_provider_defaults_from_config(config),
                    "channels": channels,
                },
                "provider_secrets": {"bot_token": _telegram_token_for_project(bl, project_id)},
            },
        )
        status = "delivered" if result.get("delivered") else "failed"
        if hasattr(bl, "log_notification_delivery"):
            with contextlib.suppress(Exception):
                bl.log_notification_delivery(
                    route_id=_as_str(delivery_row.get("route_id")) or None,
                    project_id=str(project_id),
                    workflow_id=_as_str(delivery_row.get("workflow_id")) or None,
                    job_id=_as_str(delivery_row.get("job_id")) or None,
                    provider_key=PROVIDER_KEY,
                    event_type=_as_str(delivery_row.get("event_type"))
                    or _as_str(event.get("event_type")),
                    severity=_as_str(delivery_row.get("severity"))
                    or _as_str(event.get("severity"))
                    or "info",
                    status=status,
                    request=request_payload,
                    response=result,
                    provider_message_id=_as_str(result.get("provider_message_id")) or None,
                    error_text=_as_str(result.get("error")) or None,
                )
        return {
            "ok": bool(result.get("ok")),
            "message": "Retry delivered"
            if result.get("delivered")
            else _retry_error_message(result.get("error")),
            "reload": ["delivery_grid", "health_panel"],
            "result": result,
        }, 0

    if action_id == "test_delivery":
        if not project_id:
            return {"ok": False, "error": "project_context_required"}, 2
        connection = (
            _telegram_connection_row_for_project(bl, str(project_id)) if bl is not None else {}
        )
        config = _as_dict(connection.get("connection_config"))
        channels = _channel_items_from_config(config)
        requested_channel_id = _as_str(input_payload.get("channel_id"))
        if not requested_channel_id:
            return {
                "ok": False,
                "error": "channel_required",
                "message": MESSAGES["channel_required"],
            }, 2
        test_event = {
            "event_type": "test.notification",
            "severity": "info",
            "summary": "Test notification from Flow Steward extension platform.",
            "payload": {"message": "This is a test notification."},
        }
        test_route = {
            "severity": "info",
            "route_config": {"channel_id": requested_channel_id},
        }
        result = _deliver(
            test_event,
            test_route,
            {
                "provider_config": {
                    **_provider_defaults_from_config(config),
                    "channels": channels,
                },
                "provider_secrets": {"bot_token": _telegram_token_for_project(bl, project_id)},
            },
        )
        return {
            "ok": bool(result.get("ok")),
            "message": "Test delivered" if result.get("delivered") else "Test delivery failed",
            "reload": ["health_panel"],
            "result": result,
        }, 0 if result.get("ok") else 2

    if action_id == "resolve_chat":
        if not project_id:
            return {
                "ok": False,
                "error": "project_context_required",
                "message": "Select a project before resolving Telegram chats.",
            }, 0
        if bl is None:
            return {
                "ok": False,
                "error": "project_connection_storage_unavailable",
                "message": "Telegram project storage is unavailable right now.",
            }, 0
        token = _telegram_token_for_project(bl, project_id)
        if not token:
            return {
                "ok": False,
                "error": "bot_token_required",
                "message": "Save a bot token before resolving Telegram chats.",
            }, 0
        normalized_input = _normalize_chat_link(
            input_payload.get("chat_link") or input_payload.get("chat_ref")
        )
        if normalized_input.get("kind") == "invite_link":
            me = _telegram_get_me(token)
            bot_username = _as_str(me.get("username"))
            return {
                "ok": True,
                "message": (
                    f"Direct resolve is not available for private invite links. Add @{bot_username or 'your_bot'} "
                    "to the target chat, send /connect or /ping, then click Refresh discovered chats."
                ),
                "result": {"pending_discovery": True, "bot_username": bot_username},
                "reload": ["discovered_chats_grid", "inbound_status_grid"],
            }, 0
        chat_ref = _as_str(normalized_input.get("chat_ref"))
        if not chat_ref:
            return {
                "ok": False,
                "error": "chat_not_resolved",
                "message": "Enter a Telegram @username or t.me link to resolve.",
            }, 0
        resolved_chat = _telegram_get_chat(token, chat_ref)
        if not resolved_chat:
            return {
                "ok": False,
                "error": "chat_not_resolved",
                "message": "Telegram could not resolve that chat. Check the username or use guided discovery.",
            }, 0
        next_state = dict(inbound_state)
        discovered = list(_as_list(next_state.get("discovered_chats")))
        discovered = _upsert_discovered_chat(
            discovered,
            _chat_row_from_telegram_chat(resolved_chat, source="getChat"),
        )
        next_state["discovered_chats"] = discovered
        next_state["last_inbound_sync_at"] = datetime.now(UTC).isoformat()
        next_state["active_transport"] = _active_transport_for_state(next_state)
        _save_telegram_connection_ingress(bl, str(project_id), next_state)
        return {
            "ok": True,
            "message": "Chat resolved and added to discovered chats.",
            "result": {"chat_id": _as_str(resolved_chat.get("id"))},
            "reload": ["discovered_chats_grid", "inbound_status_grid"],
        }, 0

    if action_id == "discover_chat":
        if not project_id:
            return {"ok": False, "error": "project_context_required"}, 2
        normalized_input = _normalize_chat_link(
            input_payload.get("chat_link") or input_payload.get("chat_ref")
        )
        if normalized_input.get("kind") == "public_username":
            return _handle_action(
                {
                    "action_id": "resolve_chat",
                    "context": {"project_id": project_id},
                    "input": input_payload,
                    "runtime_context": runtime_context,
                }
            )
        token = _telegram_token_for_project(bl, project_id)
        if not token:
            return {"ok": False, "error": "bot_token_required"}, 2
        me = _telegram_get_me(token)
        bot_username = _as_str(me.get("username"))
        return {
            "ok": True,
            "message": (
                f"Add @{bot_username or 'your_bot'} to the target chat, send /connect or /ping, "
                "then click Refresh discovered chats or Poll now."
            ),
            "result": {"pending_discovery": True, "bot_username": bot_username},
            "reload": ["inbound_status_grid"],
        }, 0

    if action_id in {"poll_now", "refresh_discovered_chats"}:
        if not project_id:
            return {"ok": False, "error": "project_context_required"}, 2
        if bl is None:
            return {"ok": False, "error": "polling_unavailable"}, 2
        token = _telegram_token_for_project(bl, project_id)
        result, code = _poll_updates(
            bl=bl,
            project_id=str(project_id),
            inbound_state=inbound_state,
            token=token,
            reconcile_webhook_status=action_id == "refresh_discovered_chats",
        )
        return result, code

    if action_id == "save_discovered_chat":
        if not project_id:
            return {"ok": False, "error": "project_context_required"}, 2
        if bl is None:
            return {"ok": False, "error": "project_connection_storage_unavailable"}, 2
        selected_chat_id = _as_str(
            target.get("chat_id")
            or input_payload.get("chat_id")
            or _as_dict(action.get("data")).get("chat_id")
        )
        if not selected_chat_id:
            return {"ok": False, "error": "chat_not_resolved"}, 2
        config = _as_dict(connection.get("connection_config"))
        channels = _channel_items_from_config(config)
        existing_by_chat = {
            _as_str(_as_dict(channel).get("chat_id")): _as_dict(channel) for channel in channels
        }
        if selected_chat_id in existing_by_chat:
            existing_channel = existing_by_chat[selected_chat_id]
            return {
                "ok": False,
                "error": "channel_already_exists",
                "message": f"Chat is already saved as channel '{_as_str(existing_channel.get('channel_id'))}'.",
                "result": {"existing_channel_id": _as_str(existing_channel.get("channel_id"))},
            }, 0
        data_payload = _as_dict(action.get("data")) or dict(input_payload)
        title = _as_str(
            target.get("title")
            or input_payload.get("display_name")
            or data_payload.get("title")
            or data_payload.get("username")
            or selected_chat_id
        )
        requested_channel_id = _as_str(input_payload.get("channel_id"))
        channel_id = requested_channel_id or _slugify_channel_id(
            data_payload.get("username") or title or selected_chat_id
        )
        existing_ids = {_as_str(_as_dict(channel).get("channel_id")) for channel in channels}
        if requested_channel_id and channel_id in existing_ids:
            return {"ok": False, "error": "channel_id_duplicate"}, 2
        if not requested_channel_id:
            suffix = 2
            base = channel_id
            while channel_id in existing_ids:
                channel_id = f"{base}_{suffix}"
                suffix += 1
        new_channel = {
            "channel_id": channel_id,
            "display_name": title,
            "chat_id": selected_chat_id,
            "enabled": True,
            "capabilities": {"notify": True, "human_decision": False},
        }
        next_channels = [*channels, new_channel]
        next_config = _replace_channels_in_config(config, next_channels)
        if not _save_telegram_connection_config(bl, str(project_id), next_config):
            return {"ok": False, "error": "project_connection_storage_unavailable"}, 2
        return {
            "ok": True,
            "message": f"Saved discovered chat as channel '{channel_id}'.",
            "reload": ["discovered_chats_grid", "channels_form", "health_panel"],
            "values": {"channels": _channel_items_for_ui(next_channels)},
        }, 0

    if action_id == "verify_bot_token":
        token = _telegram_token_for_project(bl, project_id, _as_dict(input_payload.get("provider")))
        if not token:
            return {
                "ok": False,
                "error": "bot_token_required",
                "message": MESSAGES["bot_token_required"],
                "result": {"verified": False},
            }, 2
        api_response = _telegram_api_call(token, "getMe")
        if _coerce_bool(api_response.get("ok"), False):
            me = _as_dict(api_response.get("result"))
            username = _as_str(me.get("username"))
            return {
                "ok": True,
                "message": f"Connected as @{username}" if username else "Connected",
                "result": {"verified": True, "bot_username": username, "bot_id": me.get("id")},
                "reload": ["health_panel"],
            }, 0
        failure_code = (
            "bot_token_rejected"
            if classify_bot_api_failure(api_response) == "unauthorized"
            else "telegram_unreachable"
        )
        return {
            "ok": False,
            "error": failure_code,
            "message": MESSAGES[failure_code],
            "result": {"verified": False},
        }, 2

    if action_id == "configure_webhook":
        callback_url = _as_str(input_payload.get("callback_url"))
        callback_secret = _as_str(input_payload.get("callback_secret"))
        runtime = _as_dict(input_payload.get("runtime"))
        fake_mode = _coerce_bool(runtime.get("fake_mode"), False)
        if not callback_url:
            return {"ok": False, "error": "callback_url_required"}, 2
        if fake_mode:
            next_state = dict(inbound_state)
            next_state["webhook_status"] = _webhook_projection(
                {
                    "status": "healthy",
                    "url_configured": True,
                    "pending_update_count": 0,
                    "last_checked_at": datetime.now(UTC).isoformat(),
                }
            )
            next_state["active_transport"] = _active_transport_for_state(next_state)
            if bl is not None and project_id:
                _save_telegram_connection_ingress(bl, str(project_id), next_state)
            return {
                "ok": True,
                "message": "Webhook configured (fake mode)",
                "result": {
                    "provider": "telegram",
                    "url_configured": True,
                    "fake_mode": True,
                    "callback_url": callback_url,
                },
                "reload": ["telegram_webhook_setup", "health_panel"],
            }, 0
        token = _telegram_token_for_project(bl, project_id, _as_dict(input_payload.get("provider")))
        if not token:
            return {"ok": False, "error": "bot_token_required"}, 2
        payload: dict[str, Any] = {"url": callback_url}
        if callback_secret:
            payload["secret_token"] = callback_secret
        api_response = _telegram_api_call(token, "setWebhook", payload)
        ok = bool(api_response.get("ok"))
        result = _as_dict(api_response.get("result"))
        next_state = dict(inbound_state)
        next_state["webhook_status"] = _webhook_projection(
            {
                "status": "healthy" if ok else "failed",
                "url_configured": ok,
                "pending_update_count": 0,
                "last_error_message": _as_str(
                    api_response.get("description") or api_response.get("error")
                ),
                "last_checked_at": datetime.now(UTC).isoformat(),
            }
        )
        next_state["active_transport"] = _active_transport_for_state(next_state)
        next_state["last_inbound_error"] = (
            "" if ok else _as_str(api_response.get("description") or api_response.get("error"))
        )
        if bl is not None and project_id:
            registration = _callback_registration_for_project(bl, str(project_id))
            if registration:
                webhook_status = _as_dict(next_state.get("webhook_status"))
                webhook_status["callback_registration_id"] = _as_str(registration.get("id"))
                next_state["webhook_status"] = webhook_status
            _save_telegram_connection_ingress(bl, str(project_id), next_state)
        return {
            "ok": ok,
            "message": "Webhook configured" if ok else "Webhook configuration failed",
            "result": {
                "provider": "telegram",
                "url_configured": ok,
                "fake_mode": False,
                "telegram_result": result,
            },
            "errors": []
            if ok
            else [
                {
                    "code": "telegram_set_webhook_failed",
                    "message": _as_str(
                        api_response.get("description") or api_response.get("error")
                    ),
                }
            ],
            "reload": [
                "telegram_webhook_setup",
                "inbound_status_grid",
                "inbound_mode_form",
                "health_panel",
            ],
        }, 0

    if action_id == "get_webhook_status":
        runtime = _as_dict(input_payload.get("runtime"))
        fake_mode = _coerce_bool(runtime.get("fake_mode"), False)
        if fake_mode:
            return {
                "ok": True,
                "result": {
                    "provider": "telegram",
                    "url_configured": True,
                    "pending_update_count": 0,
                    "last_error_date": None,
                    "last_error_message": "",
                    "max_connections": 40,
                    "allowed_updates": [],
                    "fake_mode": True,
                },
            }, 0
        token = _telegram_token_for_project(bl, project_id, _as_dict(input_payload.get("provider")))
        if not token:
            return {"ok": False, "error": "bot_token_required"}, 2
        api_response = _telegram_api_call(token, "getWebhookInfo")
        info = _as_dict(api_response.get("result"))
        ok = bool(api_response.get("ok"))
        next_state = dict(inbound_state)
        if ok and bool(_as_str(info.get("url"))):
            webhook_state = "healthy"
        elif ok:
            webhook_state = "not_configured"
        else:
            webhook_state = "failed"
        next_state["webhook_status"] = _webhook_projection(
            {
                "status": webhook_state,
                "url_configured": bool(_as_str(info.get("url"))),
                "pending_update_count": _coerce_int(info.get("pending_update_count")) or 0,
                "last_error_message": _as_str(info.get("last_error_message")),
                "last_checked_at": datetime.now(UTC).isoformat(),
            }
        )
        next_state["active_transport"] = _active_transport_for_state(next_state)
        next_state["last_inbound_error"] = (
            "" if ok else _as_str(api_response.get("description") or api_response.get("error"))
        )
        if bl is not None and project_id:
            registration = _callback_registration_for_project(bl, str(project_id))
            if registration:
                webhook_status = _as_dict(next_state.get("webhook_status"))
                webhook_status["callback_registration_id"] = _as_str(registration.get("id"))
                next_state["webhook_status"] = webhook_status
            _save_telegram_connection_ingress(bl, str(project_id), next_state)
        return {
            "ok": ok,
            "result": {
                "provider": "telegram",
                "url_configured": bool(_as_str(info.get("url"))),
                "pending_update_count": _coerce_int(info.get("pending_update_count")) or 0,
                "last_error_date": info.get("last_error_date"),
                "last_error_message": _as_str(info.get("last_error_message")),
                "max_connections": _coerce_int(info.get("max_connections")) or 0,
                "allowed_updates": _as_list(info.get("allowed_updates")),
            },
            "errors": []
            if ok
            else [
                {
                    "code": "telegram_get_webhook_info_failed",
                    "message": _as_str(
                        api_response.get("description") or api_response.get("error")
                    ),
                }
            ],
            "reload": ["telegram_webhook_setup", "inbound_status_grid", "inbound_mode_form"],
        }, 0

    if action_id == "toggle_route":
        if not project_id:
            selected_ids = [
                str(item or "").strip()
                for item in (_as_dict(action.get("target")).get("item_ids") or [])
                if str(item or "").strip()
            ]
            if not selected_ids:
                return {"ok": False, "error": "toggle_route_requires_target_item_ids"}, 2
            return {
                "ok": True,
                "message": f"Toggled {len(selected_ids)} route(s) (standalone mode)",
                "reload": ["subscription_routes"],
            }, 0
        if (
            bl is None
            or not hasattr(bl, "list_notification_routes")
            or not hasattr(bl, "save_notification_routes")
        ):
            return {"ok": False, "error": "notification_route_storage_unavailable"}, 2
        selected_ids = [
            str(item or "").strip()
            for item in (_as_dict(action.get("target")).get("item_ids") or [])
            if str(item or "").strip()
        ]
        if not selected_ids:
            return {"ok": False, "error": "toggle_route_requires_target_item_ids"}, 2
        selected_set = {item.lower() for item in selected_ids}
        routes = bl.list_notification_routes(project_id=str(project_id), enabled_only=False) or []
        prepared_routes: list[dict] = []
        toggled = 0
        for row in routes:
            item = _as_dict(row)
            route_key = str(item.get("route_key") or "").strip()
            route_id = str(item.get("route_id") or "").strip()
            if route_key.lower() in selected_set or route_id.lower() in selected_set:
                item["enabled"] = not bool(item.get("enabled", True))
                toggled += 1
            prepared_routes.append(
                {
                    "route_key": route_key,
                    "display_name": str(item.get("display_name") or "").strip(),
                    "workflow_id": str(item.get("workflow_id") or "").strip() or None,
                    "provider_key": str(item.get("provider_key") or "").strip(),
                    "event_type": str(item.get("event_type") or "").strip(),
                    "severity": str(item.get("severity") or "any").strip().lower() or "any",
                    "route_config": _as_dict(item.get("route_config")),
                    "enabled": bool(item.get("enabled", True)),
                }
            )
        if toggled <= 0:
            return {"ok": False, "error": "no_matching_routes_found"}, 2
        bl.save_notification_routes(str(project_id), routes=prepared_routes)
        return {
            "ok": True,
            "message": f"Toggled {toggled} route(s)",
            "reload": ["subscription_routes", "delivery_grid", "health_panel"],
        }, 0

    if action_id == "ingress_event":
        return _handle_ingress_event_payload(action)

    return {"ok": False, "error": f"unknown_action:{action_id}"}, 2

from __future__ import annotations

from typing import Any

from main_readiness import derive_readiness
from main_state import (
    _REQUEST_RUNTIME_CONTEXT,
    PROVIDER_KEY,
    _active_transport_for_state,
    _annotate_discovered_chats,
    _apply_grid_params,
    _as_dict,
    _as_list,
    _as_str,
    _callback_registration_for_project,
    _channel_items_for_ui,
    _channel_items_from_config,
    _coerce_int,
    _coerce_project_id,
    _event_label,
    _humanize_token,
    _json_safe_value,
    _normalize_inbound_state,
    _provider_defaults_from_config,
    _resolve_runtime_logger,
    _telegram_connection_row_for_project,
)
from main_transport import _telegram_api_call, _telegram_token_for_project


def _recent_telegram_deliveries(bl: Any, project_id: Any) -> list[dict[str, Any]]:
    if bl is None or not project_id or not hasattr(bl, "list_notification_delivery_logs"):
        return []
    try:
        rows = bl.list_notification_delivery_logs(project_id=str(project_id), limit=50) or []
    except Exception:
        return []
    return [
        _as_dict(row)
        for row in rows
        if _as_str(_as_dict(row).get("provider_key")).lower() == PROVIDER_KEY
    ]


def _handle_query(query: dict) -> tuple[dict, int]:
    query_id = str(query.get("query_id") or "").strip()
    context = _as_dict(query.get("context"))
    params = _as_dict(query.get("params"))
    project_id = _coerce_project_id(
        context.get("project_id"),
        params.get("project_id"),
    )
    runtime_context = _as_dict(query.get("runtime_context")) or _REQUEST_RUNTIME_CONTEXT
    bl = _resolve_runtime_logger(runtime_context)

    if query_id == "provider_health":
        # Overview readiness from current facts: the stored connection, whether a
        # token exists, one bounded getMe when it does, channels, inbound state
        # and recent deliveries. No historical health row: nothing writes one.
        connection = (
            _telegram_connection_row_for_project(bl, str(project_id))
            if (bl is not None and project_id)
            else {}
        )
        token = _telegram_token_for_project(bl, project_id) if project_id else ""
        identity = _telegram_api_call(token, "getMe") if token else None
        readiness = derive_readiness(
            connection=connection,
            token_present=bool(token),
            identity_response=identity,
            channels=_channel_items_from_config(_as_dict(connection.get("connection_config"))),
            inbound_state=_normalize_inbound_state(_as_dict(connection.get("ingress_config"))),
            deliveries=_recent_telegram_deliveries(bl, project_id),
        )
        return {
            "ok": True,
            "checks": readiness["checks"],
            "items": [{"status": readiness["summary"]["readiness"]}],
            "summary": {
                **readiness["summary"],
                "project_id": project_id,
                "runtime_available": True,
            },
        }, 0

    if query_id == "delivery_summary":
        if bl is not None and project_id and hasattr(bl, "list_notification_delivery_logs"):
            rows = bl.list_notification_delivery_logs(project_id=project_id, limit=2000) or []
            deliveries = [
                _as_dict(row)
                for row in rows
                if str(_as_dict(row).get("provider_key") or "").strip().lower() == PROVIDER_KEY
            ]
            delivered = sum(
                1
                for row in deliveries
                if str(row.get("status") or "").strip().lower() == "delivered"
            )
            failed = sum(
                1 for row in deliveries if str(row.get("status") or "").strip().lower() == "failed"
            )
            return {
                "ok": True,
                "items": [
                    {"label": "Total", "value": len(deliveries)},
                    {"label": "Delivered", "value": delivered},
                    {"label": "Failed", "value": failed},
                ],
                "summary": {
                    "provider": "telegram",
                    "total": len(deliveries),
                    "delivered": delivered,
                    "failed": failed,
                },
            }, 0
        return {
            "ok": True,
            "items": [],
            "summary": {"provider": "telegram", "total": 0, "delivered": 0, "failed": 0},
        }, 0

    if query_id == "list_deliveries":
        if bl is not None and project_id and hasattr(bl, "list_notification_delivery_logs"):
            rows = bl.list_notification_delivery_logs(project_id=project_id, limit=2000) or []
            deliveries = []
            for row in rows:
                payload = _as_dict(row)
                if str(payload.get("provider_key") or "").strip().lower() != PROVIDER_KEY:
                    continue
                deliveries.append(
                    {
                        "delivery_id": payload.get("delivery_id"),
                        "event_type": str(payload.get("event_type") or "").strip(),
                        "event_label": _event_label(payload.get("event_type")),
                        "severity": str(payload.get("severity") or "info").strip().lower()
                        or "info",
                        "status": str(payload.get("status") or "").strip().lower() or "unknown",
                        "delivered_at": _json_safe_value(payload.get("created_at")),
                        "error_text": str(payload.get("error_text") or "").strip(),
                        "workflow_id": str(payload.get("workflow_id") or "").strip(),
                        "workflow_label": _humanize_token(payload.get("workflow_id")),
                        "job_id": payload.get("job_id"),
                        "provider_message_id": str(
                            payload.get("provider_message_id") or ""
                        ).strip(),
                        "route_id": payload.get("route_id"),
                    }
                )
            effective_params = dict(params)
            effective_params.setdefault("page_size", 20)
            paged_items, page, page_size, total = _apply_grid_params(deliveries, effective_params)
            return {
                "ok": True,
                "items": paged_items,
                "page": page,
                "page_size": page_size,
                "total": total,
                "has_more": (page * page_size) < total,
                "summary": {"provider": "telegram", "project_id": project_id, "total": total},
            }, 0
        return {"ok": True, "items": [], "summary": {"provider": "telegram"}}, 0

    if query_id == "list_routes":
        if bl is not None and project_id and hasattr(bl, "list_notification_routes"):
            rows = bl.list_notification_routes(project_id=str(project_id), enabled_only=False) or []
            routes: list[dict[str, Any]] = []
            for row in rows:
                payload = _as_dict(row)
                if str(payload.get("provider_key") or "").strip().lower() != PROVIDER_KEY:
                    continue
                route_config = _as_dict(payload.get("route_config"))
                routes.append(
                    {
                        "route_id": payload.get("route_id"),
                        "route_key": str(payload.get("route_key") or "").strip(),
                        "event_type": str(payload.get("event_type") or "").strip(),
                        "severity": str(payload.get("severity") or "").strip().lower() or "any",
                        "workflow_id": str(payload.get("workflow_id") or "").strip(),
                        "channel_id": str(route_config.get("channel_id") or "").strip(),
                        "enabled": bool(payload.get("enabled", True)),
                    }
                )
            paged_items, page, page_size, total = _apply_grid_params(routes, params)
            return {
                "ok": True,
                "items": paged_items,
                "page": page,
                "page_size": page_size,
                "total": total,
                "has_more": (page * page_size) < total,
                "summary": {"provider": "telegram", "project_id": project_id, "total": total},
            }, 0
        return {"ok": True, "items": [], "summary": {"provider": "telegram"}}, 0

    if query_id == "get_config":
        if not project_id:
            return {"ok": False, "error": "project_context_required"}, 2
        connection = (
            _telegram_connection_row_for_project(bl, str(project_id or ""))
            if bl is not None and project_id
            else {}
        )
        config = _as_dict(connection.get("connection_config"))
        return {
            "ok": True,
            "values": _provider_defaults_from_config(config),
            "summary": {"provider": "telegram", "project_id": project_id},
        }, 0

    if query_id == "get_secrets":
        if not project_id:
            return {"ok": False, "error": "project_context_required"}, 2
        token = ""  # nosec B105 - empty until the stored token is read
        if bl is not None and project_id and hasattr(bl, "get_provider_secret"):
            try:
                token = str(
                    bl.get_provider_secret(
                        provider_key=PROVIDER_KEY,
                        secret_key="bot_token",  # nosec B106 - names the stored secret, not its value
                        project_id=str(project_id),
                        include_global=False,
                    )
                    or ""
                ).strip()
            except Exception:
                token = ""  # nosec B105 - empty when the stored token cannot be read
        return {
            "ok": True,
            "values": {
                "bot_token": "",  # nosec B105 - a secret is never echoed back to the form
            },
            "summary": {
                "provider": "telegram",
                "project_id": project_id,
                "bot_token_configured": bool(token),
            },
        }, 0

    if query_id == "list_channels":
        if not project_id:
            return {"ok": False, "error": "project_context_required"}, 2
        connection = (
            _telegram_connection_row_for_project(bl, str(project_id)) if bl is not None else {}
        )
        config = _as_dict(connection.get("connection_config"))
        channels = _channel_items_for_ui(_channel_items_from_config(config))
        paged_items, page, page_size, total = _apply_grid_params(channels, params)
        return {
            "ok": True,
            "items": paged_items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "values": {"channels": channels},
            "summary": {"provider": "telegram", "project_id": project_id, "total": total},
        }, 0

    if query_id == "get_inbound_status":
        if not project_id:
            return {"ok": False, "error": "project_context_required"}, 2
        connection = (
            _telegram_connection_row_for_project(bl, str(project_id)) if bl is not None else {}
        )
        inbound_state = _normalize_inbound_state(_as_dict(connection.get("ingress_config")))
        registration = (
            _callback_registration_for_project(bl, str(project_id)) if bl is not None else {}
        )
        webhook_status = _as_dict(inbound_state.get("webhook_status"))
        if registration and not webhook_status.get("callback_registration_id"):
            webhook_status["callback_registration_id"] = _as_str(registration.get("id"))
        inbound_state["webhook_status"] = webhook_status
        inbound_state["active_transport"] = _active_transport_for_state(inbound_state)
        item = {
            "inbound_mode": _as_str(inbound_state.get("inbound_mode")).title(),
            "active_transport": _as_str(inbound_state.get("active_transport")) or "unknown",
            "webhook_status": _as_str(webhook_status.get("status")) or "unknown",
            "polling_status": _as_str(inbound_state.get("polling_status")) or "idle",
            "last_inbound_sync_at": _as_str(inbound_state.get("last_inbound_sync_at")),
            "last_update_id": inbound_state.get("last_update_id"),
            "pending_update_count": _coerce_int(inbound_state.get("pending_update_count")) or 0,
            "last_inbound_error": _as_str(inbound_state.get("last_inbound_error")),
            "callback_registration_id": _as_str(webhook_status.get("callback_registration_id")),
        }
        return {
            "ok": True,
            "items": [item],
            "values": {
                "inbound_mode": inbound_state.get("inbound_mode"),
                "polling_interval_seconds": inbound_state.get("polling_interval_seconds"),
            },
            "summary": {
                "provider": "telegram",
                "project_id": project_id,
                "active_transport": item["active_transport"],
                "webhook_status": item["webhook_status"],
            },
        }, 0

    if query_id == "list_discovered_chats":
        if not project_id:
            return {"ok": False, "error": "project_context_required"}, 2
        connection = (
            _telegram_connection_row_for_project(bl, str(project_id)) if bl is not None else {}
        )
        inbound_state = _normalize_inbound_state(_as_dict(connection.get("ingress_config")))
        config = _as_dict(connection.get("connection_config"))
        channels = _channel_items_from_config(config)
        discovered = _annotate_discovered_chats(
            list(_as_list(inbound_state.get("discovered_chats"))),
            channels,
        )
        paged_items, page, page_size, total = _apply_grid_params(discovered, params)
        return {
            "ok": True,
            "items": paged_items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "values": {"discovered_chats": discovered},
            "summary": {"provider": "telegram", "project_id": project_id, "total": total},
        }, 0

    return {"ok": False, "error": f"unknown_query:{query_id}"}, 2

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from main_ingress import _ingress_event_response
from main_state import (
    _as_dict,
    _as_list,
    _as_str,
    _coerce_bool,
    _coerce_int,
    _save_telegram_connection_ingress,
    _upsert_discovered_chat,
    _webhook_projection,
)
from main_transport import (
    _discover_chats_from_update,
    _telegram_delete_webhook,
    _telegram_get_updates,
    _telegram_get_webhook_info,
)


def _polling_failure_message(response: dict[str, Any], fallback: str) -> str:
    description = _as_str(response.get("description") or response.get("error"))
    if _is_competing_get_updates_conflict(response):
        return (
            "Telegram rejected polling because another bot instance is already polling. "
            "Stop the other poller and try again."
        )
    return description or fallback


def _is_competing_get_updates_conflict(response: dict[str, Any]) -> bool:
    description = _as_str(response.get("description") or response.get("error"))
    normalized = description.lower()
    return (
        _coerce_int(response.get("error_code")) == 409
        and "getupdates" in normalized
        and "webhook" not in normalized
        and ("terminated by other" in normalized or "only one bot instance" in normalized)
    )


def _is_webhook_active_get_updates_conflict(response: dict[str, Any]) -> bool:
    description = _as_str(response.get("description") or response.get("error")).lower()
    return (
        _coerce_int(response.get("error_code")) == 409
        and "getupdates" in description
        and "webhook" in description
    )


def _poll_updates(
    *,
    bl: Any,
    project_id: str,
    inbound_state: dict[str, Any],
    token: str,
    reconcile_webhook_status: bool = False,
) -> tuple[dict[str, Any], int]:
    inbound_state = dict(inbound_state)
    if not token:
        next_state = dict(inbound_state)
        next_state["polling_status"] = "failed"
        next_state["last_inbound_error"] = "bot_token_required"
        next_state["last_inbound_sync_at"] = datetime.now(UTC).isoformat()
        next_state["active_transport"] = "polling"
        _save_telegram_connection_ingress(bl, project_id, next_state)
        return {
            "ok": False,
            "error": "bot_token_required",
            "message": "Add a bot token before polling can run.",
        }, 0
    if reconcile_webhook_status:
        webhook_response = _telegram_get_webhook_info(token)
        webhook_info = _as_dict(webhook_response.get("result"))
        if not bool(webhook_response.get("ok")):
            message = (
                _as_str(webhook_response.get("description") or webhook_response.get("error"))
                or "Telegram webhook status is unavailable."
            )
            next_state = dict(inbound_state)
            next_state["webhook_status"] = _webhook_projection(
                {
                    **_as_dict(next_state.get("webhook_status")),
                    "status": "failed",
                    "last_error_message": message,
                    "last_checked_at": datetime.now(UTC).isoformat(),
                }
            )
            next_state["polling_status"] = "failed"
            next_state["last_inbound_error"] = message
            next_state["last_inbound_sync_at"] = datetime.now(UTC).isoformat()
            _save_telegram_connection_ingress(bl, project_id, next_state)
            return {
                "ok": False,
                "error": "webhook_unavailable",
                "message": message,
                "definitely_no_external_effect": True,
            }, 0
        webhook_url_configured = bool(_as_str(webhook_info.get("url")))
        inbound_state["webhook_status"] = _webhook_projection(
            {
                "status": "healthy" if webhook_url_configured else "not_configured",
                "url_configured": webhook_url_configured,
                "pending_update_count": _coerce_int(webhook_info.get("pending_update_count")) or 0,
                "last_error_message": _as_str(webhook_info.get("last_error_message")),
                "last_checked_at": datetime.now(UTC).isoformat(),
            }
        )
    webhook_status = _as_dict(inbound_state.get("webhook_status"))
    mode = _as_str(inbound_state.get("inbound_mode")).lower() or "auto"
    webhook_url_configured = _coerce_bool(webhook_status.get("url_configured"), False)
    webhook_healthy = webhook_url_configured and _as_str(webhook_status.get("status")).lower() in {
        "healthy",
        "configured",
    }
    if mode == "webhook" and webhook_healthy:
        return {
            "ok": False,
            "error": "polling_unavailable",
            "message": "Polling is unavailable while webhook mode is active.",
        }, 0
    provider_mutated = False
    if mode in {"polling", "auto"} and webhook_url_configured:
        delete_response = _telegram_delete_webhook(token)
        if not bool(delete_response.get("ok")):
            next_state = dict(inbound_state)
            next_state["last_inbound_error"] = (
                _as_str(delete_response.get("description") or delete_response.get("error"))
                or "webhook_unavailable"
            )
            next_state["polling_status"] = "failed"
            _save_telegram_connection_ingress(bl, project_id, next_state)
            return {
                "ok": False,
                "error": "webhook_unavailable",
                "message": _as_str(
                    delete_response.get("description") or delete_response.get("error")
                )
                or "Webhook must be disabled before polling can run.",
            }, 0
        provider_mutated = True
        inbound_state["webhook_status"] = _webhook_projection(
            {"status": "not_configured", "url_configured": False}
        )
    offset = None
    last_update_id = _coerce_int(inbound_state.get("last_update_id"))
    if last_update_id is not None:
        offset = last_update_id + 1
    response = _telegram_get_updates(token, offset=offset)
    if not bool(response.get("ok")):
        next_state = dict(inbound_state)
        next_state["polling_status"] = "failed"
        next_state["last_inbound_error"] = _polling_failure_message(response, "polling_unavailable")
        next_state["last_inbound_sync_at"] = datetime.now(UTC).isoformat()
        next_state["active_transport"] = "polling"
        _save_telegram_connection_ingress(bl, project_id, next_state)
        return {
            "ok": False,
            "error": "polling_unavailable",
            "message": next_state["last_inbound_error"],
            "definitely_no_external_effect": (
                not provider_mutated
                and (
                    _is_competing_get_updates_conflict(response)
                    or _is_webhook_active_get_updates_conflict(response)
                )
            ),
        }, 0
    updates = _as_list(response.get("result"))
    next_state = dict(inbound_state)
    discovered = list(_as_list(next_state.get("discovered_chats")))
    events: list[dict[str, Any]] = []
    highest_update_id = _coerce_int(next_state.get("last_update_id"))
    processed = 0
    connection_config: dict[str, Any] = {}
    connection_id = ""
    if bl is not None and hasattr(bl, "get_project_connection"):
        try:
            connection_row = _as_dict(
                bl.get_project_connection(
                    project_id=str(project_id),
                    extension_id="flowsteward.telegram",
                    enabled_only=True,
                )
            )
            connection_config = _as_dict(connection_row.get("connection_config"))
            connection_id = _as_str(connection_row.get("connection_id"))
        except Exception:
            connection_config = {}
    for raw_update in updates:
        update = _as_dict(raw_update)
        update_id = _coerce_int(update.get("update_id"))
        if update_id is not None and (highest_update_id is None or update_id > highest_update_id):
            highest_update_id = update_id
        for row in _discover_chats_from_update(update):
            discovered = _upsert_discovered_chat(discovered, row)
        ingress_response = _ingress_event_response(
            {
                "callback": {
                    "extension_id": "flowsteward.telegram",
                    "callback_id": "updates",
                    "project_id": project_id,
                    "authenticated": True,
                },
                "payload": update,
                "provider": {
                    "provider_config": {
                        **connection_config,
                        "project_id": project_id,
                    },
                    "provider_secrets": {"bot_token": token},
                    "project_id": project_id,
                    "connection_id": connection_id,
                    "ingress_config": next_state,
                },
                "query": {"project_id": project_id},
            },
            mutable_inbound_state=next_state,
        )
        events.extend(_as_list(ingress_response.get("events")))
        processed += 1
    next_state["discovered_chats"] = discovered
    next_state["last_update_id"] = highest_update_id
    next_state["last_inbound_sync_at"] = datetime.now(UTC).isoformat()
    next_state["last_inbound_error"] = ""
    next_state["polling_status"] = "healthy"
    next_state["pending_update_count"] = len(updates)
    next_state["active_transport"] = "polling"
    _save_telegram_connection_ingress(bl, project_id, next_state)
    return {
        "ok": True,
        "message": f"Processed {processed} update(s)." if processed else "No new updates found.",
        "result": {
            "processed_updates": processed,
            "last_update_id": highest_update_id,
            "active_transport": "polling",
            "pending_update_count": len(updates),
        },
        "events": events,
        "reload": [
            "inbound_mode_form",
            "inbound_status_grid",
            "discovered_chats_grid",
            "channels_form",
            "delivery_grid",
        ],
    }, 0

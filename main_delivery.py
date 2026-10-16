from __future__ import annotations

import html
import re

from main_state import (
    _REQUEST_RUNTIME_CONTEXT,
    APPROVAL_REQUESTED_VALUE,
    JOB_AWAITING_APPROVAL_VALUE,
    _as_dict,
    _as_list,
    _as_str,
    _coerce_bool,
    _coerce_int,
    _consume_pending_hitl_approval,
    _html_escape,
    _normalize_inbound_state,
    _runtime_logger,
)
from main_transport import (
    _approval_reply_markup,
    _edit_message_reply_markup,
    _provider_runtime_config,
    _render_message,
    _required_channel_capability,
    _resolve_channel,
    _send_message,
)

_TOKEN_APPROVAL_RESOLVED = "approval.resolved"  # nosec B105 - an event type, not a secret

_TELEGRAM_MESSAGE_LIMIT = 4096
_HTML_TAG_RE = re.compile(r"<[^>]*>")


def _pre_send_failure(error: str, *, event_type: str, severity: str) -> dict:
    return {
        "ok": False,
        "delivered": False,
        "error": error,
        "error_code": error,
        "external_effect_status": "failed",
        "definitely_no_external_effect": True,
        "provider_message_id": "",
        "summary": {"event_type": event_type, "severity": severity, "delivered": False},
    }


def _bounded_telegram_html(message: str) -> str:
    if len(message) <= _TELEGRAM_MESSAGE_LIMIT:
        return message
    plain_text = html.unescape(_HTML_TAG_RE.sub("", message))
    suffix = "…"
    low, high = 0, len(plain_text)
    while low < high:
        midpoint = (low + high + 1) // 2
        candidate = html.escape(plain_text[:midpoint], quote=False) + suffix
        if len(candidate) <= _TELEGRAM_MESSAGE_LIMIT:
            low = midpoint
        else:
            high = midpoint - 1
    return html.escape(plain_text[:low], quote=False) + suffix


def _deliver(
    event: dict,
    route: dict,
    provider: dict | None = None,
    *,
    delivery_phase: str = "",
    prepared_delivery: dict | None = None,
) -> dict:
    """Deliver a notification via the Telegram Bot API."""
    event_type = _as_str(event.get("event_type") or "unknown")
    severity = _as_str(route.get("severity") or "info")
    route_config = _as_dict(route.get("route_config"))

    provider_runtime = _provider_runtime_config(provider)
    token = _as_str(provider_runtime.get("token"))
    normalized_phase = _as_str(delivery_phase).lower()
    channels = [
        item for item in _as_list(provider_runtime.get("channels")) if isinstance(item, dict)
    ]
    channel_id = _as_str(route_config.get("channel_id"))
    selected_channel = _resolve_channel(channels, channel_id) if channel_id else {}
    required_capability = _as_str(
        route_config.get("required_capability")
    ) or _required_channel_capability(event)
    if channel_id:
        if not selected_channel:
            return _pre_send_failure("unknown_channel_id", event_type=event_type, severity=severity)
        if not _coerce_bool(selected_channel.get("enabled"), True):
            return _pre_send_failure("channel_disabled", event_type=event_type, severity=severity)
        capabilities = _as_dict(selected_channel.get("capabilities"))
        if not _coerce_bool(capabilities.get(required_capability), False):
            return _pre_send_failure(
                f"channel_missing_{required_capability}",
                event_type=event_type,
                severity=severity,
            )
    if not channel_id:
        return _pre_send_failure("channel_id_required", event_type=event_type, severity=severity)
    target_chat = _as_str(selected_channel.get("chat_id"))
    message = _render_message(event, route, provider)
    message_prefix = _as_str(provider_runtime.get("message_prefix"))
    if message_prefix:
        message = f"{_html_escape(message_prefix)}\n{message}"
    message = _bounded_telegram_html(message)
    parse_mode = "HTML"
    disable_preview = _coerce_bool(
        route_config.get("disable_web_page_preview")
        if "disable_web_page_preview" in route_config
        else provider_runtime.get("disable_web_page_preview"),
        True,
    )
    is_hitl = event_type in {
        APPROVAL_REQUESTED_VALUE,
        JOB_AWAITING_APPROVAL_VALUE,
    }
    if normalized_phase == "prepare":
        reply_markup = {}
        if is_hitl:
            reply_markup = _approval_reply_markup(
                event,
                provider,
                chat_id=target_chat,
                channel_id=_as_str(selected_channel.get("channel_id")) or channel_id,
            )
            if not reply_markup:
                return _pre_send_failure(
                    "hitl_token_persistence_required",
                    event_type=event_type,
                    severity=severity,
                )
        elif event_type == _TOKEN_APPROVAL_RESOLVED:
            reply_target = _as_dict(_as_dict(event.get("payload")).get("reply_target"))
            approval_id = _as_str(_as_dict(event.get("payload")).get("approval_id"))
            if approval_id:
                _consume_pending_hitl_approval(
                    _runtime_logger(_REQUEST_RUNTIME_CONTEXT),
                    project_id=_as_str(
                        event.get("project_id") or provider_runtime.get("project_id")
                    ),
                    approval_id=approval_id,
                    job_id=_as_str(event.get("job_id")),
                    inbound_state=_normalize_inbound_state(
                        _as_dict(provider_runtime.get("ingress_config"))
                    ),
                    chat_id=_as_str(reply_target.get("external_conversation_ref")),
                    channel_id=channel_id,
                )
        return {
            "ok": True,
            "delivered": False,
            "error": "",
            "prepared_delivery": {"reply_markup": reply_markup},
            "summary": {
                "event_type": event_type,
                "severity": severity,
                "delivered": False,
            },
        }

    reply_markup = {}
    if is_hitl:
        prepared = _as_dict(prepared_delivery)
        reply_markup = _as_dict(prepared.get("reply_markup"))
        if not isinstance(reply_markup.get("inline_keyboard"), list):
            return _pre_send_failure(
                "prepared_delivery_required",
                event_type=event_type,
                severity=severity,
            )
    if event_type == _TOKEN_APPROVAL_RESOLVED:
        reply_target = _as_dict(_as_dict(event.get("payload")).get("reply_target"))
        origin_chat_id = _as_str(reply_target.get("external_conversation_ref"))
        origin_message_id = _coerce_int(reply_target.get("external_message_ref"))
        if (
            origin_chat_id
            and origin_chat_id == target_chat
            and origin_message_id is not None
            and origin_message_id > 0
        ):
            _edit_message_reply_markup(token, target_chat, origin_message_id)
    response = _send_message(
        token,
        target_chat,
        message,
        parse_mode=parse_mode,
        disable_web_page_preview=disable_preview,
        reply_markup=reply_markup,
        reply_to_message_id=(origin_message_id if event_type == _TOKEN_APPROVAL_RESOLVED else None),
    )
    delivered = bool(response.get("ok"))
    message_id = _coerce_int(response.get("message_id"))

    result = {
        "ok": delivered,
        "delivered": delivered,
        "error": _as_str(response.get("error")),
        "provider_message_id": str(message_id) if message_id is not None else "",
        "summary": {"event_type": event_type, "severity": severity, "delivered": delivered},
    }
    for field in (
        "error_code",
        "external_effect_status",
        "definitely_no_external_effect",
    ):
        if field in response:
            result[field] = response[field]
    return result

"""Send Message: a workflow step that posts a text to one of the bot's channels.

Unlike ``deliver_notification``, which renders a platform event, this sends
exactly the text the workflow gives it: a Template step's result, a literal, any
workflow variable. The step is bound to a Telegram connection (its bot token and
named channels) and picks one channel by its ID; a project with a single channel
enabled for notifications may leave the channel out.
"""

from __future__ import annotations

import html
from typing import Any

from main_state import _as_dict, _as_list, _as_str, _channel_items_from_config, _coerce_bool
from main_transport import _send_message

TELEGRAM_MESSAGE_LIMIT = 4096
FORMATS = ("text", "html")


def _failure(code: str, message: str) -> tuple[dict[str, Any], int]:
    return (
        {
            "ok": False,
            "delivered": False,
            "error_code": code,
            "error": message,
            "external_effect_status": "failed",
            "definitely_no_external_effect": True,
            "provider_message_id": "",
        },
        2,
    )


def _test_mode(action: dict[str, Any], runtime_context: dict[str, Any]) -> bool:
    context = _as_dict(action.get("context"))
    return _coerce_bool(runtime_context.get("test_mode"), False) or _coerce_bool(
        context.get("test_mode"), False
    )


def _notify_channels(channels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        channel
        for channel in channels
        if _coerce_bool(channel.get("enabled"), True)
        and _coerce_bool(_as_dict(channel.get("capabilities")).get("notify"), False)
    ]


def _select_channel(
    channels: list[dict[str, Any]], channel_id: str
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int] | None]:
    """The channel to send to, or the failure that explains why there is none."""
    usable = _notify_channels(channels)
    if not channel_id:
        if len(usable) == 1:
            return usable[0], None
        if not usable:
            return None, _failure(
                "no_notify_channel",
                "This Telegram connection has no channel enabled for notifications. "
                "Add one on the Chats & Channels page.",
            )
        ids = ", ".join(_as_str(channel.get("channel_id")) for channel in usable)
        return None, _failure(
            "channel_id_required",
            f"This connection has several channels; choose one by its ID: {ids}.",
        )
    channel = next(
        (item for item in channels if _as_str(item.get("channel_id")) == channel_id), None
    )
    if channel is None:
        ids = ", ".join(_as_str(item.get("channel_id")) for item in channels) or "none"
        return None, _failure(
            "unknown_channel_id",
            f"No channel '{channel_id}' on this Telegram connection. Channel IDs: {ids}.",
        )
    if not _coerce_bool(channel.get("enabled"), True):
        return None, _failure("channel_disabled", f"Channel '{channel_id}' is disabled.")
    if not _coerce_bool(_as_dict(channel.get("capabilities")).get("notify"), False):
        return None, _failure(
            "channel_missing_notify",
            f"Channel '{channel_id}' is not allowed to receive notifications.",
        )
    return channel, None


def handle_send_message(
    action: dict[str, Any], runtime_context: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Run the ``send_message`` workflow operation."""
    input_payload = _as_dict(action.get("input"))
    text = input_payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return _failure("text_required", "Map the message text before running this step.")
    message_format = _as_str(input_payload.get("format")).lower() or "text"
    if message_format not in FORMATS:
        return _failure(
            "format_invalid",
            f"Format must be one of: {', '.join(FORMATS)}; got '{message_format}'.",
        )
    if _test_mode(action, runtime_context):
        # Test Run never sends: the step reports what it would have done.
        return (
            {
                "ok": True,
                "delivered": False,
                "external_effect_status": "suppressed",
                "provider_message_id": "",
                "channel_id": _as_str(input_payload.get("channel_id")),
            },
            0,
        )

    target = _as_dict(action.get("target"))
    connection = _as_dict(target.get("connection"))
    if not connection:
        return _failure(
            "project_connection_required", "Choose the Telegram connection this step sends with."
        )
    config = _as_dict(connection.get("connection_config") or connection.get("config"))
    token = _as_str(_as_dict(connection.get("secrets")).get("bot_token"))
    channels = [
        item for item in _as_list(_channel_items_from_config(config)) if isinstance(item, dict)
    ]
    channel, failure = _select_channel(channels, _as_str(input_payload.get("channel_id")))
    if failure is not None or channel is None:
        return failure or _failure("channel_id_required", "Choose a Telegram channel by its ID.")

    body = html.escape(text, quote=False) if message_format == "text" else text
    prefix = _as_str(config.get("message_prefix"))
    message = f"{html.escape(prefix, quote=False)}\n{body}" if prefix else body
    if len(message) > TELEGRAM_MESSAGE_LIMIT:
        return _failure(
            "message_too_long",
            f"Telegram accepts at most {TELEGRAM_MESSAGE_LIMIT} characters per message; "
            f"this one has {len(message)}.",
        )
    disable_preview = (
        _coerce_bool(input_payload.get("disable_web_page_preview"), True)
        if "disable_web_page_preview" in input_payload
        else _coerce_bool(config.get("disable_web_page_preview"), True)
    )
    response = _send_message(
        token,
        _as_str(channel.get("chat_id")),
        message,
        parse_mode="HTML",
        disable_web_page_preview=disable_preview,
    )
    delivered = bool(response.get("ok"))
    message_id = response.get("message_id")
    result: dict[str, Any] = {
        "ok": delivered,
        "delivered": delivered,
        "provider_message_id": str(message_id) if message_id is not None else "",
        "channel_id": _as_str(channel.get("channel_id")),
        "external_effect_status": "succeeded" if delivered else "failed",
    }
    if not delivered:
        result["error"] = _as_str(response.get("error"))
        result["error_code"] = _as_str(response.get("error_code")) or "telegram_send_failed"
    for field in ("external_effect_status", "definitely_no_external_effect"):
        if field in response:
            result[field] = response[field]
    return result, 0 if delivered else 2


__all__ = ["FORMATS", "TELEGRAM_MESSAGE_LIMIT", "handle_send_message"]

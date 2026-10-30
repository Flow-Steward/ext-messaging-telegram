"""Workflow steps beyond ``send_message``: files, edit, delete, pin and download.

Every message step is bound to a Telegram connection and addresses a chat the
same way ``send_message`` does: by the ID of one of the connection's channels
(``main_send._select_channel``). A step that acts on an existing message also
takes its ``message_id``. Each one reports ``provider_message_id`` and
``channel_id``; Test Run suppresses it (``external_effect_status: suppressed``).
``get_file`` only reads from Telegram and writes a Flow Steward artifact.
"""

from __future__ import annotations

import html
from typing import Any

from flowsteward_extension_sdk import (
    ArtifactAccessError,
    find_artifact_descriptor,
    stream_artifact_bytes,
    write_artifact_stream,
)

from main_send import (
    FORMATS,
    _failure,
    _select_channel,
    _test_mode,
    parse_inline_keyboard,
    parse_send_options,
    positive_int,
)
from main_state import _as_dict, _as_list, _as_str, _channel_items_from_config, _coerce_bool
from main_transport_steps import (
    FILE_DOWNLOAD_LIMIT_BYTES,
    FILE_UPLOAD_LIMIT_BYTES,
    PHOTO_UPLOAD_LIMIT_BYTES,
    TelegramDownloadTooLarge,
    telegram_file_chunks,
    telegram_get_file_info,
    telegram_open_file,
    telegram_step_call,
)

TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024
UPLOAD_ARTIFACT_BINDING_KEY = "source_artifact"
DOWNLOAD_ARTIFACT_BINDING_KEY = "downloaded_file"
OCTET_STREAM = "application/octet-stream"
UPLOAD_CHUNK_BYTES = 1024 * 1024

# send_file kind -> (Bot API method, multipart field)
FILE_KINDS: dict[str, tuple[str, str]] = {
    "photo": ("sendPhoto", "photo"),
    "document": ("sendDocument", "document"),
    "video": ("sendVideo", "video"),
    "audio": ("sendAudio", "audio"),
    "voice": ("sendVoice", "voice"),
    "animation": ("sendAnimation", "animation"),
}


def _suppressed(channel_id: str) -> tuple[dict[str, Any], int]:
    # Test Run never calls Telegram: the step reports what it would have done.
    return (
        {
            "ok": True,
            "delivered": False,
            "external_effect_status": "suppressed",
            "provider_message_id": "",
            "channel_id": channel_id,
        },
        0,
    )


def _connection(action: dict[str, Any]) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """``(connection, bot token, connection config)`` of the step's target."""
    connection = _as_dict(_as_dict(action.get("target")).get("connection"))
    config = _as_dict(connection.get("connection_config") or connection.get("config"))
    token = _as_str(_as_dict(connection.get("secrets")).get("bot_token"))
    return connection, token, config


def _resolve_channel(
    action: dict[str, Any], channel_id: str
) -> tuple[tuple[str, dict[str, Any], dict[str, Any]] | None, tuple[dict[str, Any], int] | None]:
    """``((token, channel, config), None)`` or ``(None, failure)``."""
    connection, token, config = _connection(action)
    if not connection:
        return None, _failure(
            "project_connection_required", "Choose the Telegram connection this step uses."
        )
    channels = [
        item for item in _as_list(_channel_items_from_config(config)) if isinstance(item, dict)
    ]
    channel, failure = _select_channel(channels, channel_id)
    if failure is not None or channel is None:
        return None, failure or _failure("channel_id_required", "Choose a Telegram channel by ID.")
    return (token, channel, config), None


def _step_result(response: dict[str, Any], channel: dict[str, Any]) -> tuple[dict[str, Any], int]:
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
        result["error_code"] = _as_str(response.get("error_code")) or "telegram_request_failed"
    for field in ("external_effect_status", "definitely_no_external_effect", "retry_after_seconds"):
        if field in response:
            result[field] = response[field]
    return result, 0 if delivered else 2


def _format_of(input_payload: dict[str, Any]) -> tuple[str, tuple[dict[str, Any], int] | None]:
    message_format = _as_str(input_payload.get("format")).lower() or "text"
    if message_format not in FORMATS:
        return "", _failure(
            "format_invalid",
            f"Format must be one of: {', '.join(FORMATS)}; got '{message_format}'.",
        )
    return message_format, None


def _render(text: str, message_format: str) -> str:
    return html.escape(text, quote=False) if message_format == "text" else text


def _counted_length(parts: list[str]) -> int:
    """Characters as the author typed them; Telegram counts the text after markup."""
    return len("\n".join(part for part in parts if part))


# --- send_file ---------------------------------------------------------------


def _artifact_handle(input_payload: dict[str, Any]) -> str:
    value = input_payload.get("artifact_handle") or input_payload.get("artifact_id")
    if isinstance(value, dict):
        value = value.get("artifact_handle") or value.get("artifact_id")
    return _as_str(value)


def _read_artifact(
    action: dict[str, Any], handle: str, limit: int
) -> tuple[bytes | None, str, tuple[dict[str, Any], int] | None]:
    """``(bytes, filename, None)`` of the input artifact, never more than ``limit`` bytes."""
    too_large = _failure(
        "file_too_large",
        f"Telegram accepts at most {limit // (1024 * 1024)} MB for this kind of file; "
        "the file is larger and is not cut.",
    )
    try:
        descriptor = find_artifact_descriptor(
            action, artifact_id=handle, binding_key=UPLOAD_ARTIFACT_BINDING_KEY, role="input"
        )
        size = descriptor.get("size_bytes")
        if isinstance(size, int) and not isinstance(size, bool) and size > limit:
            return None, "", too_large
        data = bytearray()
        for chunk in stream_artifact_bytes(
            action,
            artifact_id=handle,
            binding_key=UPLOAD_ARTIFACT_BINDING_KEY,
            chunk_size=UPLOAD_CHUNK_BYTES,
            timeout_seconds=60,
        ):
            data += chunk
            if len(data) > limit:
                return None, "", too_large
    except ArtifactAccessError as exc:
        return None, "", _failure("artifact_read_failed", f"The file could not be read: {exc}")
    filename = _as_str(descriptor.get("filename") or descriptor.get("artifact_filename"))
    return bytes(data), filename, None


def handle_send_file(
    action: dict[str, Any], runtime_context: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Run the ``send_file`` workflow operation."""
    input_payload = _as_dict(action.get("input"))
    kind = _as_str(input_payload.get("kind")).lower()
    if kind not in FILE_KINDS:
        return _failure(
            "kind_invalid",
            f"Kind must be one of: {', '.join(FILE_KINDS)}; got '{kind or 'nothing'}'.",
        )
    handle = _artifact_handle(input_payload)
    if not handle:
        return _failure("artifact_required", "Map the file (an artifact) before running this step.")
    caption = _as_str(input_payload.get("caption"))
    message_format, failure = _format_of(input_payload)
    if failure is not None:
        return failure
    if _counted_length([caption]) > TELEGRAM_CAPTION_LIMIT:
        return _failure(
            "caption_too_long",
            f"Telegram accepts at most {TELEGRAM_CAPTION_LIMIT} characters in a caption; "
            f"this one has {len(caption)}.",
        )
    options, failure = parse_send_options({**input_payload, "buttons": None})
    if failure is not None:
        return failure
    if _test_mode(action, runtime_context):
        return _suppressed(_as_str(input_payload.get("channel_id")))
    resolved, failure = _resolve_channel(action, _as_str(input_payload.get("channel_id")))
    if failure is not None or resolved is None:
        return failure or _failure("channel_id_required", "Choose a Telegram channel by ID.")
    token, channel, _config = resolved

    limit = PHOTO_UPLOAD_LIMIT_BYTES if kind == "photo" else FILE_UPLOAD_LIMIT_BYTES
    data, artifact_name, failure = _read_artifact(action, handle, limit)
    if failure is not None or data is None:
        return failure or _failure("artifact_read_failed", "The file could not be read.")
    if not data:
        return _failure("artifact_empty", "The file is empty; Telegram does not accept it.")

    method, field = FILE_KINDS[kind]
    fields: dict[str, Any] = {"chat_id": _as_str(channel.get("chat_id")), **options}
    if caption:
        fields["caption"] = _render(caption, message_format)
        fields["parse_mode"] = "HTML"
    if "reply_to_message_id" in fields:
        fields["allow_sending_without_reply"] = True
    filename = _as_str(input_payload.get("file_name")) or artifact_name or f"{kind}.bin"
    response = telegram_step_call(
        token,
        method,
        fields,
        upload={
            "file_field": field,
            "filename": filename,
            "content_type": OCTET_STREAM,
            "data": data,
        },
        retry_without_parse_mode=True,
        needs_message_id=True,
    )
    return _step_result(response, channel)


# --- edit / delete / pin / unpin ---------------------------------------------


def _message_id(
    input_payload: dict[str, Any], *, required: bool
) -> tuple[int | None, tuple[dict[str, Any], int] | None]:
    raw = input_payload.get("message_id")
    if raw in (None, ""):
        if required:
            return None, _failure(
                "message_id_required", "Map the ID of the Telegram message this step acts on."
            )
        return None, None
    message_id = positive_int(raw)
    if message_id is None:
        return None, _failure(
            "message_id_invalid", "message_id must be a Telegram message ID (a positive number)."
        )
    return message_id, None


def handle_edit_message(
    action: dict[str, Any], runtime_context: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Run the ``edit_message`` workflow operation: replace the text of a sent message."""
    input_payload = _as_dict(action.get("input"))
    message_id, failure = _message_id(input_payload, required=True)
    if failure is not None or message_id is None:
        return failure or _failure("message_id_required", "Map the message ID.")
    text = input_payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return _failure("text_required", "Map the new message text before running this step.")
    message_format, failure = _format_of(input_payload)
    if failure is not None:
        return failure
    markup, failure = parse_inline_keyboard(input_payload.get("buttons"))
    if failure is not None:
        return failure
    channel_id = _as_str(input_payload.get("channel_id"))
    _connection_row, _token, config = _connection(action)
    prefix = _as_str(config.get("message_prefix"))
    if _counted_length([prefix, text]) > TELEGRAM_TEXT_LIMIT:
        return _failure(
            "message_too_long",
            f"Telegram accepts at most {TELEGRAM_TEXT_LIMIT} characters per message; "
            f"this one has {_counted_length([prefix, text])}.",
        )
    if _test_mode(action, runtime_context):
        return _suppressed(channel_id)
    resolved, failure = _resolve_channel(action, channel_id)
    if failure is not None or resolved is None:
        return failure or _failure("channel_id_required", "Choose a Telegram channel by ID.")
    token, channel, config = resolved

    body = _render(text, message_format)
    message = f"{html.escape(prefix, quote=False)}\n{body}" if prefix else body
    fields: dict[str, Any] = {
        "chat_id": _as_str(channel.get("chat_id")),
        "message_id": message_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": (
            _coerce_bool(input_payload.get("disable_web_page_preview"), True)
            if "disable_web_page_preview" in input_payload
            else _coerce_bool(config.get("disable_web_page_preview"), True)
        ),
    }
    if markup is not None:
        fields["reply_markup"] = markup
    response = telegram_step_call(token, "editMessageText", fields, retry_without_parse_mode=True)
    if response.get("ok") and response.get("message_id") is None:
        # Telegram answers an edit of a message the bot sent with the message;
        # the id the step was given is the one that was edited.
        response = {**response, "message_id": message_id}
    return _step_result(response, channel)


def _simple_message_call(
    action: dict[str, Any],
    runtime_context: dict[str, Any],
    *,
    method: str,
    message_id_required: bool,
    extra_fields: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    input_payload = _as_dict(action.get("input"))
    message_id, failure = _message_id(input_payload, required=message_id_required)
    if failure is not None:
        return failure
    channel_id = _as_str(input_payload.get("channel_id"))
    if _test_mode(action, runtime_context):
        return _suppressed(channel_id)
    resolved, failure = _resolve_channel(action, channel_id)
    if failure is not None or resolved is None:
        return failure or _failure("channel_id_required", "Choose a Telegram channel by ID.")
    token, channel, _config = resolved
    fields: dict[str, Any] = {"chat_id": _as_str(channel.get("chat_id")), **(extra_fields or {})}
    if message_id is not None:
        fields["message_id"] = message_id
    response = telegram_step_call(token, method, fields)
    if response.get("ok"):
        # Telegram answers these calls with ``true``; the id the step was given is
        # the one it acted on, and a step given none (unpin) acted on no one id.
        response = {**response, "message_id": message_id}
    return _step_result(response, channel)


def handle_delete_message(
    action: dict[str, Any], runtime_context: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Run ``delete_message``. Telegram deletes only messages younger than 48 hours."""
    return _simple_message_call(
        action, runtime_context, method="deleteMessage", message_id_required=True
    )


def handle_pin_message(
    action: dict[str, Any], runtime_context: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Run ``pin_message``; ``disable_notification`` pins without telling the chat."""
    input_payload = _as_dict(action.get("input"))
    extra: dict[str, Any] = {}
    if _coerce_bool(input_payload.get("disable_notification"), False):
        extra["disable_notification"] = True
    return _simple_message_call(
        action,
        runtime_context,
        method="pinChatMessage",
        message_id_required=True,
        extra_fields=extra,
    )


def handle_unpin_message(
    action: dict[str, Any], runtime_context: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Run ``unpin_message``; without a ``message_id`` Telegram unpins the most recent pin."""
    return _simple_message_call(
        action, runtime_context, method="unpinChatMessage", message_id_required=False
    )


# --- get_file ----------------------------------------------------------------


def _download_too_large() -> tuple[dict[str, Any], int]:
    return _failure(
        "file_too_large",
        f"Telegram lets a bot download at most {FILE_DOWNLOAD_LIMIT_BYTES // (1024 * 1024)} MB; "
        "this file is larger and is not cut.",
    )


def handle_get_file(
    action: dict[str, Any], runtime_context: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Run ``get_file``: download a Telegram file by ``file_id`` into an artifact.

    Telegram keeps the download link valid for about an hour; the step downloads
    at once, so the link is never stored.
    """
    _ = runtime_context
    input_payload = _as_dict(action.get("input"))
    file_id = _as_str(input_payload.get("file_id"))
    if not file_id:
        return _failure("file_id_required", "Map the Telegram file_id before running this step.")
    connection, token, _config = _connection(action)
    if not connection:
        return _failure(
            "project_connection_required", "Choose the Telegram connection this step uses."
        )
    if not token:
        return _failure("bot_token_or_chat_id_required", "The connection has no bot token.")

    info = telegram_get_file_info(token, file_id)
    if not info.get("ok"):
        return _failure_from_transport(info)
    declared_size = info.get("file_size")
    if isinstance(declared_size, int) and declared_size > FILE_DOWNLOAD_LIMIT_BYTES:
        return _download_too_large()
    file_path = _as_str(info.get("file_path"))
    file_name = file_path.rsplit("/", 1)[-1] or "file.bin"

    if token.startswith(("fake", "test")):
        chunks: Any = iter(())
        response = None
    else:
        try:
            response = telegram_open_file(token, file_path)
        except ValueError as exc:
            return _failure("telegram_file_not_found", str(exc))
        except Exception as exc:
            return _failure(
                "telegram_download_failed",
                f"Telegram did not serve the file: {type(exc).__name__}.",
            )
        chunks = telegram_file_chunks(response, max_bytes=FILE_DOWNLOAD_LIMIT_BYTES)
    try:
        write_result = write_artifact_stream(
            action,
            chunks,
            binding_key=DOWNLOAD_ARTIFACT_BINDING_KEY,
            content_type=OCTET_STREAM,
            timeout_seconds=60,
        )
    except TelegramDownloadTooLarge:
        return _download_too_large()
    except ArtifactAccessError as exc:
        return _failure("artifact_write_failed", f"The file could not be stored: {exc}")
    handle = _as_str(write_result.get("artifact_handle") or write_result.get("artifact_id"))
    if not handle:
        return _failure("artifact_write_failed", "The artifact store returned no handle.")
    return (
        {
            "ok": True,
            "delivered": True,
            "artifact_handle": handle,
            "artifact_filename": file_name,
            "size_bytes": write_result.get("size_bytes"),
            "sha256": _as_str(write_result.get("sha256")),
            "external_effect_status": "succeeded",
        },
        0,
    )


def _failure_from_transport(response: dict[str, Any]) -> tuple[dict[str, Any], int]:
    result, code = _failure(
        _as_str(response.get("error_code")) or "telegram_request_failed",
        _as_str(response.get("error")) or "Telegram refused the request.",
    )
    for field in ("external_effect_status", "definitely_no_external_effect", "retry_after_seconds"):
        if field in response:
            result[field] = response[field]
    return result, code


__all__ = [
    "FILE_KINDS",
    "handle_delete_message",
    "handle_edit_message",
    "handle_get_file",
    "handle_pin_message",
    "handle_send_file",
    "handle_unpin_message",
]

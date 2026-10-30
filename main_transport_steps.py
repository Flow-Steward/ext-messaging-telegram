"""Telegram Bot API calls behind the workflow steps beyond ``send_message``.

``send_file`` uploads multipart/form-data; ``edit_message``, ``delete_message``,
``pin_message`` and ``unpin_message`` are JSON calls; ``get_file`` resolves a
``file_id`` and streams the download. Every call here returns the same dict
shape as ``main_transport._send_message``: ``ok`` plus either the Telegram
``result`` or an ``error_code`` that says whether Telegram definitely did not
act (a 4xx answer) or the outcome is unknown (timeout, 5xx).
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from typing import Any
from urllib import error as urlerror
from urllib import parse
from urllib import request as urlrequest

from main_state import APPLICATION_JSON_VALUE, _as_dict, _as_str, _coerce_int
from main_transport import (
    _redact_telegram_bot_token_from_error,
    _telegram_api_call,
    _telegram_retry_after_seconds,
)

TELEGRAM_API_HOST = "https://api.telegram.org"
JSON_TIMEOUT_SECONDS = 15
UPLOAD_TIMEOUT_SECONDS = 120
DOWNLOAD_TIMEOUT_SECONDS = 60
DOWNLOAD_CHUNK_BYTES = 64 * 1024

# Bot API limits (core.telegram.org/bots/api): upload 10 MB for a photo and
# 50 MB for any other file; getFile downloads at most 20 MB.
PHOTO_UPLOAD_LIMIT_BYTES = 10 * 1024 * 1024
FILE_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024
FILE_DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024

# Description fragments of a 4xx answer, lowercase, mapped to a stable error code.
_REJECTIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("message can't be deleted",), "telegram_delete_not_allowed"),
    (
        (
            "message to delete not found",
            "message to edit not found",
            "message to pin not found",
            "message to unpin not found",
            "message_id_invalid",
            "message identifier is not specified",
        ),
        "telegram_message_not_found",
    ),
    (("message is not modified",), "telegram_message_not_modified"),
    (
        ("message can't be edited", "there is no text in the message to edit"),
        "telegram_edit_not_allowed",
    ),
    (
        (
            "not enough rights",
            "need administrator rights",
            "have no rights",
            "chat_admin_required",
            "not enough permissions",
        ),
        "telegram_not_enough_rights",
    ),
    (("chat not found",), "telegram_chat_not_found"),
    (
        ("bot was blocked", "bot was kicked", "bot is not a member", "user is deactivated"),
        "telegram_bot_not_in_chat",
    ),
    (("file is too big", "request entity too large"), "telegram_file_too_large"),
    (
        ("wrong file identifier", "wrong remote file identifier", "invalid file_id"),
        "telegram_file_not_found",
    ),
    (
        ("reply message not found", "message to be replied not found", "replied message not found"),
        "telegram_reply_target_invalid",
    ),
    (
        ("button_url_invalid", "button_data_invalid", "can't parse inline keyboard"),
        "telegram_keyboard_rejected",
    ),
)
_PARSE_REJECTION_MARKERS = (
    "can't parse entities",
    "can't find end of the entity",
    "unsupported parse_mode",
    "unsupported parse mode",
)


def _method_url(token: str, method: str) -> str:
    return f"{TELEGRAM_API_HOST}/bot{parse.quote(token, safe='')}/{method}"


def _definite_failure(
    code: str, error: Any, *, status: int = 0, parsed: dict[str, Any] | None = None
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "ok": False,
        "error": _redact_telegram_bot_token_from_error(error),
        "error_code": code,
        "external_effect_status": "failed",
        "definitely_no_external_effect": True,
        "failure_class": "provider",
        "retryable": status == 429,
    }
    if status:
        response["http_status"] = status
    retry_after = _telegram_retry_after_seconds(parsed or {})
    if retry_after is not None:
        response["retry_after_seconds"] = retry_after
    return response


def _ambiguous_failure(error: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "error": _redact_telegram_bot_token_from_error(error),
        "error_code": "telegram_send_ambiguous",
        "external_effect_status": "timeout_unknown",
        "definitely_no_external_effect": False,
        "failure_class": "transient",
        "retryable": False,
    }


def _description(parsed: dict[str, Any], fallback: Any = "") -> str:
    return _as_str(parsed.get("description") or parsed.get("error") or fallback)


def _rejection_code(description: str, status: int) -> str:
    lowered = description.lower()
    if status == 429:
        return "telegram_rate_limited"
    if status == 413:
        return "telegram_file_too_large"
    if any(marker in lowered for marker in _PARSE_REJECTION_MARKERS):
        return "telegram_markup_invalid"
    for markers, code in _REJECTIONS:
        if any(marker in lowered for marker in markers):
            return code
    return "telegram_request_rejected"


def _failure_from_answer(status: int, parsed: dict[str, Any], fallback: Any = "") -> dict[str, Any]:
    description = _description(parsed, fallback)
    if 400 <= status < 500:
        return _definite_failure(
            _rejection_code(description, status), description, status=status, parsed=parsed
        )
    return _ambiguous_failure(description or "telegram_request_failed")


def _parsed_http_error(exc: urlerror.HTTPError) -> dict[str, Any]:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
        return _as_dict(json.loads(raw) if raw else {})
    except Exception:
        return {}


def _safe_filename(name: str, fallback: str) -> str:
    cleaned = re.sub(r'[\r\n"\\/]+', "_", _as_str(name)).strip(" .")
    return cleaned[:150] or fallback


def _multipart_body(
    fields: dict[str, Any],
    *,
    file_field: str,
    filename: str,
    content_type: str,
    data: bytes,
) -> tuple[bytes, str]:
    boundary = f"fsboundary{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        if value is None:
            continue
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, (dict, list)):
            text = json.dumps(value, separators=(",", ":"))
        else:
            text = str(value)
        parts.append(
            (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n').encode()
            + text.encode("utf-8")
            + b"\r\n"
        )
    safe_name = _safe_filename(filename, "file")
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
            f'filename="{safe_name}"\r\nContent-Type: {content_type}\r\n\r\n'
        ).encode()
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _request_for(
    token: str,
    method: str,
    fields: dict[str, Any],
    upload: dict[str, Any] | None,
) -> urlrequest.Request:
    url = _method_url(token, method)
    if upload:
        body, content_type = _multipart_body(fields, **upload)
        headers = {"Content-Type": content_type, "Accept": APPLICATION_JSON_VALUE}
    else:
        body = json.dumps(fields).encode("utf-8")
        headers = {"Content-Type": APPLICATION_JSON_VALUE, "Accept": APPLICATION_JSON_VALUE}
    return urlrequest.Request(url, data=body, headers=headers, method="POST")


def _once(token: str, method: str, fields: dict[str, Any], upload: dict[str, Any] | None):
    """One request: ``(response, parse_rejected)``; the flag allows one retry."""
    timeout = UPLOAD_TIMEOUT_SECONDS if upload else JSON_TIMEOUT_SECONDS
    try:
        request = _request_for(token, method, fields, upload)
    except Exception as exc:
        return _definite_failure("telegram_send_request_invalid", exc), False
    try:
        with urlrequest.urlopen(request, timeout=timeout) as response:  # nosec B310 - a fixed Telegram Bot API https URL
            raw = response.read().decode("utf-8", errors="replace")
            parsed = _as_dict(json.loads(raw) if raw else {})
            status = int(getattr(response, "status", 200) or 200)
    except urlerror.HTTPError as exc:
        status = int(exc.code or 0)
        parsed = _parsed_http_error(exc)
        failure = _failure_from_answer(status, parsed, exc)
        return failure, failure.get("error_code") == "telegram_markup_invalid"
    except Exception as exc:
        return _ambiguous_failure(exc), False
    if status == 200 and parsed.get("ok") is True:
        result = parsed.get("result")
        message_id = _coerce_int(_as_dict(result).get("message_id"))
        return {"ok": True, "message_id": message_id, "result": result}, False
    failure = _failure_from_answer(status, parsed)
    return failure, failure.get("error_code") == "telegram_markup_invalid"


def telegram_step_call(
    token: str,
    method: str,
    fields: dict[str, Any],
    *,
    upload: dict[str, Any] | None = None,
    retry_without_parse_mode: bool = False,
    needs_message_id: bool = False,
) -> dict[str, Any]:
    """Call one Telegram method; JSON body, or multipart when ``upload`` is given.

    ``upload`` is ``{"file_field", "filename", "content_type", "data"}``. With
    ``retry_without_parse_mode`` a rejected markup is sent once more as plain
    text, as ``send_message`` does. ``needs_message_id`` turns a success without a
    message id into an ambiguous outcome rather than a silent success.
    """
    if not token:
        return {
            "ok": False,
            "error": "bot_token_or_chat_id_required",
            "error_code": "bot_token_or_chat_id_required",
            "external_effect_status": "failed",
            "definitely_no_external_effect": True,
        }
    if token.startswith(("fake", "test")):
        message_id = _coerce_int(fields.get("message_id")) or 1
        return {"ok": True, "message_id": message_id, "result": {"message_id": message_id}}
    payload = dict(fields)
    response, parse_rejected = _once(token, method, payload, upload)
    if parse_rejected and retry_without_parse_mode and payload.get("parse_mode"):
        payload.pop("parse_mode", None)
        response, _ = _once(token, method, payload, upload)
    if response.get("ok") and needs_message_id and response.get("message_id") is None:
        return _ambiguous_failure("telegram_success_message_id_missing")
    return response


def telegram_get_file_info(token: str, file_id: str) -> dict[str, Any]:
    """``getFile``: ``{"ok": True, "file_path", "file_size"}`` or a failure."""
    if token.startswith(("fake", "test")):
        return {"ok": True, "file_path": "documents/file_1.bin", "file_size": 0}
    response = _telegram_api_call(token, "getFile", {"file_id": file_id})
    if response.get("ok") is True:
        result = _as_dict(response.get("result"))
        file_path = _as_str(result.get("file_path"))
        if not file_path:
            return _definite_failure("telegram_file_not_found", "Telegram gave no file path")
        return {
            "ok": True,
            "file_path": file_path,
            "file_size": _coerce_int(result.get("file_size")),
        }
    status = _coerce_int(response.get("error_code")) or 0
    description = _description(response)
    if status == 0 and description:
        return _ambiguous_failure(description)
    return _failure_from_answer(status, response)


class TelegramDownloadTooLarge(Exception):
    """The download passed the byte limit; nothing is truncated, the step fails."""


def telegram_open_file(token: str, file_path: str) -> Any:
    """Open the download stream of a ``getFile`` path; the caller closes it."""
    if (
        not file_path
        or file_path.startswith("/")
        or "\\" in file_path
        or any(part in {"", ".", ".."} for part in file_path.split("/"))
    ):
        raise ValueError("Telegram returned an unusable file path")
    url = f"{TELEGRAM_API_HOST}/file/bot{parse.quote(token, safe='')}/{parse.quote(file_path, safe='/')}"
    request = urlrequest.Request(url, headers={"Accept": "*/*"}, method="GET")
    return urlrequest.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS)  # nosec B310 - a fixed Telegram Bot API https URL


def telegram_file_chunks(response: Any, *, max_bytes: int) -> Iterator[bytes]:
    """The download in chunks; raises :class:`TelegramDownloadTooLarge` past the limit."""
    total = 0
    try:
        while True:
            chunk = response.read(DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                return
            total += len(chunk)
            if total > max_bytes:
                raise TelegramDownloadTooLarge(f"The file is larger than {max_bytes} bytes")
            yield chunk
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()

from __future__ import annotations

import json
import re
from typing import Any
from urllib import error as urlerror
from urllib import parse
from urllib import request as urlrequest

from main_state import (
    _REQUEST_RUNTIME_CONTEXT,
    _SUPPORTED_UPDATE_TYPES,
    APPLICATION_JSON_VALUE,
    APPROVAL_REQUESTED_VALUE,
    JOB_AWAITING_APPROVAL_VALUE,
    PROVIDER_KEY,
    _approval_job_id,
    _as_dict,
    _as_str,
    _build_callback_data,
    _channel_items_from_config,
    _chat_row_from_telegram_chat,
    _coerce_bool,
    _coerce_int,
    _coerce_project_id,
    _event_label,
    _html_escape,
    _job_url,
    _message_context,
    _normalize_allowed_actions,
    _normalize_inbound_state,
    _register_pending_hitl_token,
    _runtime_logger,
    _workflow_url,
)

_TELEGRAM_BOT_URL_TOKEN_RE = re.compile(r"(/bot)[^/\s]+")


def _redact_telegram_bot_token_from_error(value: Any) -> str:
    return _TELEGRAM_BOT_URL_TOKEN_RE.sub(r"\1<redacted>", _as_str(value))


def _telegram_http_error_response(
    exc: urlerror.HTTPError,
    *,
    request_semantics: str = "mutation",
) -> dict[str, Any]:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
        parsed = _as_dict(json.loads(raw) if raw else {})
    except Exception:
        parsed = {}
    description = _redact_telegram_bot_token_from_error(
        parsed.get("description") or parsed.get("error") or exc.reason or exc
    )
    try:
        error_code = int(parsed.get("error_code") or exc.code)
    except (TypeError, ValueError):
        error_code = int(exc.code)
    retryable_status = error_code in {408, 429, 500, 502, 503, 504}
    definitely_no_effect = request_semantics == "safe_read" or error_code == 429
    response: dict[str, Any] = {
        "ok": False,
        "error_code": error_code,
        "description": description,
        "error": description,
        "failure_class": "provider",
        "retryable": retryable_status and definitely_no_effect,
        "definitely_no_external_effect": definitely_no_effect,
        "external_effect_status": "failed" if definitely_no_effect else "timeout_unknown",
        "http_status": error_code,
    }
    retry_after = _telegram_retry_after_seconds(parsed)
    if retry_after is not None:
        response["retry_after_seconds"] = retry_after
    return response


def _telegram_retry_after_seconds(parsed: dict[str, Any]) -> float | None:
    parameters = _as_dict(parsed.get("parameters"))
    try:
        value = float(parameters.get("retry_after") or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _telegram_get_me(token: str) -> dict[str, Any]:
    response = _telegram_api_call(token, "getMe")
    return _as_dict(response.get("result")) if bool(response.get("ok")) else {}


def _telegram_get_chat(token: str, chat_ref: str) -> dict[str, Any]:
    response = _telegram_api_call(token, "getChat", {"chat_id": chat_ref})
    return _as_dict(response.get("result")) if bool(response.get("ok")) else {}


def _telegram_get_updates(token: str, *, offset: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"allowed_updates": _SUPPORTED_UPDATE_TYPES, "timeout": 0}
    if offset is not None:
        payload["offset"] = int(offset)
    return _telegram_api_call(token, "getUpdates", payload)


def _telegram_get_webhook_info(token: str) -> dict[str, Any]:
    return _telegram_api_call(token, "getWebhookInfo")


def _telegram_delete_webhook(token: str) -> dict[str, Any]:
    return _telegram_api_call(token, "deleteWebhook", {"drop_pending_updates": False})


def _discover_chats_from_update(update: dict[str, Any]) -> list[dict[str, Any]]:
    payload = _as_dict(update)
    discovered: list[dict[str, Any]] = []
    message = _as_dict(payload.get("message"))
    if message:
        chat = _as_dict(message.get("chat"))
        chat_row = _chat_row_from_telegram_chat(chat, source="message")
        if _as_str(chat_row.get("chat_id")):
            discovered.append(chat_row)
    callback_query = _as_dict(payload.get("callback_query"))
    if callback_query:
        message = _as_dict(callback_query.get("message"))
        chat = _as_dict(message.get("chat"))
        chat_row = _chat_row_from_telegram_chat(chat, source="callback_query")
        if _as_str(chat_row.get("chat_id")):
            discovered.append(chat_row)
    chat_member = _as_dict(payload.get("my_chat_member"))
    if chat_member:
        chat = _as_dict(chat_member.get("chat"))
        chat_row = _chat_row_from_telegram_chat(chat, source="my_chat_member")
        if _as_str(chat_row.get("chat_id")):
            discovered.append(chat_row)
    return discovered


def _render_message(event: dict, route: dict, provider: dict | None = None) -> str:
    """Render a notification message from event + route data."""
    severity = _as_str(event.get("severity") or route.get("severity") or "info").upper()
    event_type = _as_str(event.get("event_type") or "unknown")
    payload = _as_dict(event.get("payload"))
    if event_type == "approval.resolved":
        applied = bool(payload.get("applied"))
        action = _as_str(payload.get("action")).lower()
        context = _message_context(event, provider)
        context_parts = []
        if context["task_summary"]:
            context_parts.append(f"Task: {_html_escape(context['task_summary'])}")
        if context["workflow_label"]:
            context_parts.append(f"Workflow: {_html_escape(context['workflow_label'])}")
        if context["job_id"]:
            short_job_id = (
                f"{context['job_id'][:8]}…" if len(context["job_id"]) > 8 else context["job_id"]
            )
            context_parts.append(f"Job: {_html_escape(short_job_id)}")
        if not applied:
            parts = ["⚠️ <b>Decision was not applied.</b>"]
            reason = {
                "stale": "This approval is no longer current.",
                "already_handled": "This approval was already handled.",
                "not_allowed": "This action is no longer allowed for this approval.",
            }.get(
                _as_str(payload.get("outcome_code")).lower(),
                "Flow Steward could not apply this decision.",
            )
            parts.append(reason)
            parts.append("Refresh the approval state in Flow Steward and try again if needed.")
        elif action == "approve":
            parts = ["✅ <b>Approval applied.</b>", "The job will continue."]
        elif action == "reject":
            parts = ["↩️ <b>Rejection applied.</b>"]
            comment = _as_str(payload.get("comment"))
            if comment:
                parts.append(f"Reason: {_html_escape(comment)}")
            parts.extend(
                [
                    "The job was sent back for rework.",
                    "Flow Steward will continue through the rework path using this reason.",
                ]
            )
        elif action == "abort":
            parts = ["⛔ <b>Abort applied.</b>", "The job was stopped."]
        else:
            parts = ["✅ <b>Decision applied.</b>"]
        parts[1:1] = context_parts
        parts.append("This decision is final; any older approval buttons no longer apply.")
        return "\n".join(parts)
    context = _message_context(event, provider)
    workflow_line = ""
    if context["workflow_label"]:
        workflow_url = _workflow_url(context["project_id"], context["workflow_id"])
        workflow_label = _html_escape(context["workflow_label"])
        workflow_value = (
            f'<a href="{_html_escape(workflow_url)}">{workflow_label}</a>'
            if workflow_url
            else workflow_label
        )
        workflow_line = f"Workflow: {workflow_value}"
    job_line = ""
    if context["job_id"]:
        job_url = _job_url(context["project_id"], context["job_id"])
        short_job_id = (
            f"{context['job_id'][:8]}…" if len(context["job_id"]) > 8 else context["job_id"]
        )
        escaped_job_id = _html_escape(short_job_id)
        if job_url:
            job_line = f'Job: <a href="{_html_escape(job_url)}">{escaped_job_id}</a>'
        else:
            job_line = f"Job: {escaped_job_id}"

    parts = [f"[{severity}] {_event_label(event_type) or event_type}"]
    if context["task_summary"]:
        parts.append(f"Task: {_html_escape(context['task_summary'])}")
    if workflow_line:
        parts.append(workflow_line)
    if job_line:
        parts.append(job_line)
    if context["group_label"]:
        parts.append(f"Group: {_html_escape(context['group_label'])}")
    if context["for_each_label"]:
        parts.append(f"For each: {_html_escape(context['for_each_label'])}")
    if context["step_label"]:
        parts.append(f"Step: {_html_escape(context['step_label'])}")
    if context["summary"]:
        parts.append(_html_escape(context["summary"]))

    route_config = _as_dict(route.get("route_config"))
    suffix = str(route_config.get("message_suffix") or "").strip()
    if suffix:
        parts.append(_html_escape(suffix))

    if event_type in {APPROVAL_REQUESTED_VALUE, JOB_AWAITING_APPROVAL_VALUE}:
        allowed_actions = _normalize_allowed_actions(payload.get("allowed_actions"))
        action_labels = [_humanize_approval_action(action) for action in allowed_actions]
        if not action_labels:
            parts.append(
                "No approval actions are currently available. Open Flow Steward to refresh "
                "this approval."
            )
        elif len(action_labels) == 1:
            action_text = action_labels[0]
            parts.append(f"Use the buttons below to {action_text} this request.")
        else:
            action_text = ", ".join(action_labels[:-1]) + f" or {action_labels[-1]}"
            parts.append(f"Use the buttons below to {action_text} this request.")

    return "\n".join(parts)


def _provider_runtime_config(provider: dict | None) -> dict[str, Any]:
    provider_payload = _as_dict(provider)
    provider_config = _as_dict(provider_payload.get("provider_config"))
    route_binding = _as_dict(provider_payload.get("binding_config"))
    provider_secrets = _as_dict(provider_payload.get("provider_secrets"))
    token = _as_str(provider_secrets.get("bot_token"))
    parse_mode = "HTML"
    disable_preview = _coerce_bool(
        provider_config.get("disable_web_page_preview")
        if "disable_web_page_preview" in provider_config
        else route_binding.get("disable_web_page_preview"),
        True,
    )
    message_prefix = _as_str(
        provider_config.get("message_prefix") or route_binding.get("message_prefix") or ""
    )
    return {
        "token": token,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,
        "message_prefix": message_prefix,
        "channels": _channel_items_from_config(provider_config or route_binding),
        "project_id": _coerce_project_id(
            provider_payload.get("project_id"),
            provider_config.get("project_id"),
            route_binding.get("project_id"),
        ),
        "ingress_config": _as_dict(provider_payload.get("ingress_config")),
    }


def _send_message(
    token: str,
    chat_id: str,
    message: str,
    *,
    parse_mode: str = "HTML",
    disable_web_page_preview: bool = True,
    reply_markup: dict[str, Any] | None = None,
    reply_to_message_id: int | None = None,
    disable_notification: bool = False,
) -> dict[str, Any]:
    if not token or not chat_id:
        return {
            "ok": False,
            "error": "bot_token_or_chat_id_required",
            "error_code": "bot_token_or_chat_id_required",
            "external_effect_status": "failed",
            "definitely_no_external_effect": True,
        }
    if token.startswith(("fake", "test")):
        return {"ok": True, "message_id": 1, "result": {"message_id": 1, "chat": {"id": chat_id}}}
    url = f"https://api.telegram.org/bot{parse.quote(token, safe='')}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": bool(disable_web_page_preview),
    }
    normalized_parse_mode = _as_str(parse_mode)
    if normalized_parse_mode:
        payload["parse_mode"] = normalized_parse_mode
    if isinstance(reply_markup, dict) and reply_markup:
        payload["reply_markup"] = reply_markup
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = int(reply_to_message_id)
        payload["allow_sending_without_reply"] = True
    if disable_notification:
        payload["disable_notification"] = True

    def ambiguous_failure(error: Any, *, failure_class: str = "transient") -> dict[str, Any]:
        return {
            "ok": False,
            "error": _redact_telegram_bot_token_from_error(error),
            "error_code": "telegram_send_ambiguous",
            "external_effect_status": "timeout_unknown",
            "definitely_no_external_effect": False,
            "failure_class": failure_class,
            "retryable": False,
            "provider_error_code": "telegram_send_ambiguous",
        }

    def parsed_http_error(exc: urlerror.HTTPError) -> dict[str, Any]:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            return _as_dict(json.loads(raw) if raw else {})
        except Exception:
            return {}

    def is_safe_parse_rejection(status: int, parsed: dict[str, Any]) -> bool:
        try:
            error_code = int(parsed.get("error_code") or 400)
        except (TypeError, ValueError):
            return False
        if status != 400 or error_code != 400:
            return False
        description = _as_str(parsed.get("description") or parsed.get("error")).lower()
        return any(
            marker in description
            for marker in (
                "can't parse entities",
                "can't find end of the entity",
                "unsupported parse_mode",
                "unsupported parse mode",
            )
        )

    def is_missing_reply_target(status: int, parsed: dict[str, Any]) -> bool:
        if reply_to_message_id is None or status != 400:
            return False
        description = _as_str(parsed.get("description") or parsed.get("error")).lower()
        return any(
            marker in description
            for marker in (
                "reply message not found",
                "message to be replied not found",
                "replied message not found",
            )
        )

    def missing_reply_target_failure(error: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "error": _redact_telegram_bot_token_from_error(error),
            "error_code": "telegram_reply_target_invalid",
            "external_effect_status": "failed",
            "definitely_no_external_effect": True,
        }

    def definitive_provider_failure(
        error: Any,
        status: int,
        parsed: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response: dict[str, Any] = {
            "ok": False,
            "error": _redact_telegram_bot_token_from_error(error),
            "error_code": "telegram_send_rejected",
            "provider_status": status,
            "external_effect_status": "failed",
            "definitely_no_external_effect": True,
            "failure_class": "provider",
            "retryable": status == 429,
            "provider_error_code": "telegram_send_rejected",
            "http_status": status,
        }
        retry_after = _telegram_retry_after_seconds(parsed or {})
        if retry_after is not None:
            response["retry_after_seconds"] = retry_after
        return response

    def request_for(send_payload: dict[str, Any]):
        return urlrequest.Request(
            url,
            data=json.dumps(send_payload).encode("utf-8"),
            headers={
                "Content-Type": APPLICATION_JSON_VALUE,
                "Accept": APPLICATION_JSON_VALUE,
            },
            method="POST",
        )

    try:
        req = request_for(payload)
    except Exception as exc:
        return {
            "ok": False,
            "error": _redact_telegram_bot_token_from_error(exc),
            "error_code": "telegram_send_request_invalid",
            "external_effect_status": "failed",
            "definitely_no_external_effect": True,
        }

    retry_without_parse_mode = False
    try:
        with urlrequest.urlopen(req, timeout=10) as response:  # nosec B310 - a fixed Telegram Bot API https URL
            raw = response.read().decode("utf-8", errors="replace")
            parsed = _as_dict(json.loads(raw) if raw else {})
            status = int(getattr(response, "status", 200) or 200)
            if status == 200 and parsed.get("ok") is True:
                result = _as_dict(parsed.get("result"))
                message_id = _coerce_int(result.get("message_id"))
                if message_id is None:
                    return ambiguous_failure("telegram_success_message_id_missing")
                return {
                    "ok": True,
                    "message_id": message_id,
                    "result": result,
                }
            if is_missing_reply_target(status, parsed):
                return missing_reply_target_failure(
                    parsed.get("description") or parsed.get("error")
                )
            retry_without_parse_mode = is_safe_parse_rejection(status, parsed)
            if not retry_without_parse_mode:
                if 400 <= status < 500:
                    return definitive_provider_failure(
                        parsed.get("description") or parsed.get("error"), status, parsed
                    )
                return ambiguous_failure(
                    parsed.get("description") or parsed.get("error"),
                    failure_class="provider",
                )
    except urlerror.HTTPError as exc:
        parsed = parsed_http_error(exc)
        if is_missing_reply_target(int(exc.code or 0), parsed):
            return missing_reply_target_failure(
                parsed.get("description") or parsed.get("error") or exc
            )
        retry_without_parse_mode = is_safe_parse_rejection(int(exc.code or 0), parsed)
        if not retry_without_parse_mode:
            if 400 <= int(exc.code or 0) < 500:
                return definitive_provider_failure(
                    parsed.get("description") or parsed.get("error") or exc,
                    int(exc.code),
                    parsed,
                )
            return ambiguous_failure(
                parsed.get("description") or parsed.get("error") or exc,
                failure_class="provider",
            )
    except Exception as exc:
        return ambiguous_failure(exc)

    if not retry_without_parse_mode:
        return ambiguous_failure("telegram_send_failed")
    payload.pop("parse_mode", None)
    try:
        retry_req = request_for(payload)
    except Exception as exc:
        return {
            "ok": False,
            "error": _redact_telegram_bot_token_from_error(exc),
            "error_code": "telegram_send_request_invalid",
            "external_effect_status": "failed",
            "definitely_no_external_effect": True,
        }
    try:
        with urlrequest.urlopen(retry_req, timeout=10) as response:  # nosec B310 - a fixed Telegram Bot API https URL
            raw = response.read().decode("utf-8", errors="replace")
            parsed = _as_dict(json.loads(raw) if raw else {})
            if int(getattr(response, "status", 200) or 200) == 200 and parsed.get("ok") is True:
                result = _as_dict(parsed.get("result"))
                message_id = _coerce_int(result.get("message_id"))
                if message_id is None:
                    return ambiguous_failure("telegram_success_message_id_missing")
                return {
                    "ok": True,
                    "message_id": message_id,
                    "result": result,
                }
            status = int(getattr(response, "status", 200) or 200)
            if 400 <= status < 500:
                return definitive_provider_failure(
                    parsed.get("description") or parsed.get("error"), status, parsed
                )
            return ambiguous_failure(
                parsed.get("description") or parsed.get("error"),
                failure_class="provider",
            )
    except urlerror.HTTPError as exc:
        parsed = parsed_http_error(exc)
        if 400 <= int(exc.code or 0) < 500:
            return definitive_provider_failure(
                parsed.get("description") or parsed.get("error") or exc,
                int(exc.code),
                parsed,
            )
        return ambiguous_failure(
            parsed.get("description") or parsed.get("error") or exc,
            failure_class="provider",
        )
    except Exception as exc:
        return ambiguous_failure(exc)


def _answer_callback_query(
    token: str,
    callback_query_id: str,
    *,
    text: str = "",
    show_alert: bool = False,
) -> bool:
    if not token or not callback_query_id:
        return False
    url = f"https://api.telegram.org/bot{parse.quote(token, safe='')}/answerCallbackQuery"
    payload: dict[str, Any] = {"callback_query_id": callback_query_id}
    normalized_text = _as_str(text)
    if normalized_text:
        payload["text"] = normalized_text[:200]
    if show_alert:
        payload["show_alert"] = True
    body = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        url,
        data=body,
        headers={"Content-Type": APPLICATION_JSON_VALUE, "Accept": APPLICATION_JSON_VALUE},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=10) as response:  # nosec B310 - a fixed Telegram Bot API https URL
            return int(getattr(response, "status", 200) or 200) == 200
    except Exception:
        return False


def _edit_message_reply_markup(token: str, chat_id: str, message_id: int) -> dict[str, Any]:
    if not token or not chat_id or message_id <= 0:
        return {"ok": False, "error": "telegram_message_reference_required"}
    if token.startswith(("fake", "test")):
        return {"ok": True}
    return _telegram_api_call(
        token,
        "editMessageReplyMarkup",
        {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "reply_markup": {"inline_keyboard": []},
        },
    )


def _send_reject_reason_prompt(
    token: str,
    chat_id: int | None,
    *,
    project_id: str,
    job_id: str,
    job_ref: str,
    context_label: str = "",
    reply_to_message_id: int | None = None,
) -> dict[str, Any]:
    _ = job_id
    if chat_id is None:
        return {
            "ok": False,
            "error": "reject_prompt_chat_required",
            "error_code": "reject_prompt_chat_required",
            "external_effect_status": "failed",
            "definitely_no_external_effect": True,
        }
    lead = (
        f"Reply to this message with a rejection reason for {context_label}.\n"
        if context_label
        else "Reply to this message with a rejection reason.\n"
    )
    prompt = (
        lead
        + f"Flow Steward reject context: project_id={project_id} job_ref={job_ref}\n"
        + f"Fallback: /reject {job_ref} <comment>"
    )
    result = _send_message(
        token,
        str(chat_id),
        prompt,
        parse_mode="",
        reply_to_message_id=reply_to_message_id,
    )
    if bool(result.get("ok")):
        return result
    if _as_str(result.get("error_code")) != "telegram_reply_target_invalid":
        return result
    return _send_message(
        token,
        str(chat_id),
        prompt,
        parse_mode="",
    )


def _telegram_api_call(token: str, method_name: str, payload: dict | None = None) -> dict[str, Any]:
    if not token or not method_name:
        return {"ok": False, "error": "telegram_token_required"}
    url = f"https://api.telegram.org/bot{parse.quote(token, safe='')}/{method_name}"
    body = json.dumps(_as_dict(payload)).encode("utf-8") if payload is not None else None
    req = urlrequest.Request(
        url,
        data=body,
        headers={"Content-Type": APPLICATION_JSON_VALUE, "Accept": APPLICATION_JSON_VALUE},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urlrequest.urlopen(req, timeout=10) as response:  # nosec B310 - a fixed Telegram Bot API https URL
            raw = response.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw) if raw else {}
            return _as_dict(parsed)
    except urlerror.HTTPError as exc:
        request_semantics = (
            "safe_read"
            if method_name in {"getMe", "getChat", "getUpdates", "getWebhookInfo", "getFile"}
            else "mutation"
        )
        return _telegram_http_error_response(exc, request_semantics=request_semantics)
    except Exception as exc:
        return {"ok": False, "error": _redact_telegram_bot_token_from_error(exc)}


def _telegram_token_for_project(
    bl: Any, project_id: str | None, provider: dict | None = None
) -> str:
    provider_runtime = _provider_runtime_config(provider)
    token = _as_str(provider_runtime.get("token"))
    if token:
        return token
    if bl is None or not project_id or not hasattr(bl, "get_provider_secret"):
        return ""
    try:
        return _as_str(
            bl.get_provider_secret(
                provider_key=PROVIDER_KEY,
                secret_key="bot_token",  # nosec B106 - names the stored secret, not its value
                project_id=str(project_id),
                include_global=False,
            )
        )
    except Exception:
        return ""


def _retryable_delivery_status(status: Any) -> bool:
    return _as_str(status).lower() in {"failed", "skipped"}


def _retry_error_message(error_code: Any) -> str:
    code = _as_str(error_code).lower()
    mapping = {
        "unknown_channel_id": "Retry failed: the configured Telegram channel no longer exists.",
        "channel_disabled": "Retry failed: the configured Telegram channel is disabled.",
        "channel_missing_notify": "Retry failed: the configured Telegram channel cannot send notifications.",
        "channel_missing_human_decision": "Retry failed: the configured Telegram channel cannot handle human decisions.",
        "channel_id_required": "Retry failed: the target Telegram chat is missing.",
        "bot_token_or_chat_id_required": "Retry failed: Telegram bot token or chat id is missing.",  # nosec B105 - an error message, not a secret
    }
    return mapping.get(code) or f"Retry failed: {code or 'unknown error'}."


def _resolve_channel(
    channels: list[dict[str, Any]],
    channel_id: str,
) -> dict[str, Any]:
    normalized_channel_id = _as_str(channel_id)
    for item in channels:
        if _as_str(_as_dict(item).get("channel_id")) == normalized_channel_id:
            return _as_dict(item)
    return {}


def _required_channel_capability(event: dict[str, Any]) -> str:
    event_type = _as_str(event.get("event_type")).lower()
    if event_type in {APPROVAL_REQUESTED_VALUE, JOB_AWAITING_APPROVAL_VALUE}:
        return "human_decision"
    return "notify"


def _humanize_approval_action(action: str) -> str:
    return {"approve": "approve", "reject": "reject", "abort": "abort"}[action]


def _approval_reply_markup(
    event: dict,
    provider: dict | None = None,
    *,
    chat_id: int | str | None = None,
    channel_id: str | None = None,
) -> dict[str, Any]:
    payload = _as_dict(event.get("payload"))
    project_id = _coerce_project_id(
        event.get("project_id"),
        payload.get("project_id"),
    )
    # A propagated child approval is decided on its own job (approval_job_id).
    job_id = _approval_job_id(event)
    approval_id = _as_str(payload.get("approval_id"))
    if not project_id or not job_id or not approval_id:
        return {}
    runtime = _provider_runtime_config(provider)
    allowed_actions = _normalize_allowed_actions(payload.get("allowed_actions"))
    if not allowed_actions:
        return {}
    inbound_state = _normalize_inbound_state(_as_dict(runtime.get("ingress_config")))
    bl = _runtime_logger(_REQUEST_RUNTIME_CONTEXT)
    if bl is None:
        return {}
    job_ref = _register_pending_hitl_token(
        bl,
        project_id=str(project_id),
        inbound_state=inbound_state,
        job_id=str(job_id),
        approval_id=approval_id,
        allowed_actions=allowed_actions,
        chat_id=chat_id,
        channel_id=channel_id,
    )
    if not job_ref:
        return {}
    labels = {"approve": "Approve", "reject": "Reject", "abort": "Abort"}
    return {
        "inline_keyboard": [
            [
                {
                    "text": labels[action],
                    "callback_data": _build_callback_data(action=action, job_ref=job_ref),
                }
                for action in allowed_actions
            ]
        ]
    }

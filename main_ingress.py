from __future__ import annotations

import hmac
from typing import Any

from main_state import (
    _REQUEST_RUNTIME_CONTEXT,
    _as_dict,
    _as_job_id,
    _as_list,
    _as_str,
    _bind_pending_hitl_message_ref,
    _coerce_bool,
    _coerce_int,
    _coerce_positive_int,
    _coerce_project_id,
    _normalize_allowed_actions,
    _normalize_inbound_state,
    _parse_approval_command,
    _parse_callback_data,
    _parse_reject_prompt_reply,
    _resolve_pending_hitl_token,
    _runtime_logger,
)
from main_transport import (
    _answer_callback_query,
    _provider_runtime_config,
    _send_reject_reason_prompt,
)

_MESSAGE_THE_APPROVAL_CONTEXT_COULD_NOT_BE_RESOLVED = "The approval context could not be resolved."

MESSAGE_RECEIVED_EVENT = "telegram.message.received"
_FILE_KINDS = ("photo", "document", "video", "audio", "voice", "animation", "sticker")


def _workflow_trigger_channel_for_chat(
    provider_runtime: dict[str, Any],
    chat_id: int | None,
) -> dict[str, Any]:
    """The enabled channel of this chat that may start workflows, else ``{}``.

    Closed by default: a chat that is not a saved channel, a disabled channel and
    a channel without ``capabilities.workflow_trigger`` all start nothing.
    """
    if chat_id is None:
        return {}
    normalized_chat_id = _as_str(chat_id)
    for raw_channel in _as_list(provider_runtime.get("channels")):
        channel = _as_dict(raw_channel)
        if _as_str(channel.get("chat_id")) != normalized_chat_id:
            continue
        if not _coerce_bool(channel.get("enabled"), True):
            continue
        if _coerce_bool(_as_dict(channel.get("capabilities")).get("workflow_trigger"), False):
            return channel
    return {}


def _message_file(message: dict[str, Any]) -> tuple[str, str, str]:
    """``(kind, file_id, file_name)`` of the file a message carries, or empty strings.

    Only metadata: the file is never downloaded here. A Telegram animation also
    carries a ``document`` copy, so it is looked at before the document.
    """
    for kind in ("animation", "document", "video", "audio", "voice", "sticker"):
        item = _as_dict(message.get(kind))
        file_id = _as_str(item.get("file_id"))
        if file_id:
            return kind, file_id, _as_str(item.get("file_name"))
    sizes = [_as_dict(item) for item in _as_list(message.get("photo"))]
    sizes = [item for item in sizes if _as_str(item.get("file_id"))]
    if sizes:
        # Telegram lists the sizes of one photo smallest first.
        return "photo", _as_str(sizes[-1].get("file_id")), ""
    return "", "", ""


def _message_received_event(
    update: dict[str, Any],
    message: dict[str, Any],
    *,
    project_id: str | None,
    connection_id: str,
) -> dict[str, Any]:
    chat = _as_dict(message.get("chat"))
    sender = _as_dict(message.get("from"))
    chat_id = _as_str(chat.get("id"))
    message_id = _coerce_int(message.get("message_id")) or 0
    update_id = _coerce_int(update.get("update_id"))
    file_kind, file_id, file_name = _message_file(message)
    username = _as_str(sender.get("username"))
    user_id = _as_str(sender.get("id"))
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "user_id": user_id,
        "message_id": message_id,
        "text": _as_str(message.get("text")),
        "username": username,
        "chat_type": _as_str(chat.get("type")),
        "date": _coerce_int(message.get("date")) or 0,
        "has_file": bool(file_id),
        "file_id": file_id,
        "file_kind": file_kind,
        "file_name": file_name,
        "caption": _as_str(message.get("caption")),
        "reply_to_message_id": _coerce_int(
            _as_dict(message.get("reply_to_message")).get("message_id")
        )
        or 0,
        "update_id": update_id if update_id is not None else 0,
    }
    if connection_id:
        payload["connection_id"] = connection_id
    sender_label = f"@{username}" if username else (f"user {user_id}" if user_id else "someone")
    event: dict[str, Any] = {
        "event_type": MESSAGE_RECEIVED_EVENT,
        "severity": "info",
        "summary": f"Telegram message from {sender_label} in chat {chat_id}",
        # Stable per update, so a Telegram retry never starts a second run.
        "correlation_id": (str(update_id) if update_id is not None else f"{chat_id}:{message_id}"),
        "source": "telegram.message",
        "payload": payload,
    }
    if project_id:
        event["project_id"] = project_id
    return event


def _ingress_interaction_payload(
    *,
    approved: bool,
    action: str,
    text: str,
    comment: str,
    job_id: str,
    approval_id: str,
    project_id: str | None,
    source_id: str,
    event_key: str,
    actor: dict[str, Any],
    meta: dict[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "approved": bool(approved),
        "interaction_kind": "approval_decision",
        "action": action,
        "text": text,
        "comment": comment,
        "job_id": _as_str(job_id),
        "approval_id": _as_str(approval_id),
        "source_id": source_id,
        "event_key": event_key,
        "dedupe_key": source_id or event_key,
        "actor": actor,
        "telegram": meta,
        "reply_target": {
            "provider_key": "flowsteward.telegram",
            "channel_id": _as_str(meta.get("channel_id")),
            "external_conversation_ref": _as_str(meta.get("chat_id")),
            "external_message_ref": _as_str(meta.get("message_id")),
        },
    }
    if project_id:
        result["project_id"] = str(project_id)
    return result


def _ingress_actor_from_user(user_payload: dict[str, Any]) -> dict[str, Any]:
    user = _as_dict(user_payload)
    username = _as_str(user.get("username"))
    first_name = _as_str(user.get("first_name"))
    last_name = _as_str(user.get("last_name"))
    display_name = " ".join(part for part in (first_name, last_name) if part).strip()
    if not display_name:
        display_name = username
    user_id = _coerce_positive_int(user.get("id"))
    return {
        "id": user_id,
        "username": username,
        "display_name": display_name,
    }


def _human_decision_channel_for_chat(
    provider_runtime: dict[str, Any],
    chat_id: int | None,
) -> dict[str, Any]:
    if chat_id is None:
        return {}
    normalized_chat_id = _as_str(chat_id)
    for raw_channel in _as_list(provider_runtime.get("channels")):
        channel = _as_dict(raw_channel)
        if _as_str(channel.get("chat_id")) != normalized_chat_id:
            continue
        capabilities = _as_dict(channel.get("capabilities"))
        if not _coerce_bool(channel.get("enabled"), True):
            return {}
        if not _coerce_bool(capabilities.get("human_decision"), False):
            return {}
        return channel
    return {}


def _unauthorized_human_decision_response(
    *,
    ingress_type: str,
    project_id: str | None,
    job_id: str | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "provider": "telegram",
        "ingress_type": ingress_type,
        "ignored": True,
        "reason": "unauthorized_human_decision_channel",
    }
    if project_id:
        result["project_id"] = project_id
    if job_id:
        result["job_id"] = job_id
    return {
        "ok": False,
        "error": "unauthorized_human_decision_channel",
        "result": result,
        "events": [],
    }


def _cross_project_human_decision_response(
    *,
    ingress_type: str,
    project_id: str | None,
    job_id: str | None,
) -> dict[str, Any]:
    """Reject a Telegram human-decision that targets a project other than the
    one its callback was registered for (FS-DEEP-CAND-039 / FS-DEEP-CAND-048)."""
    result: dict[str, Any] = {
        "provider": "telegram",
        "ingress_type": ingress_type,
        "ignored": True,
        "reason": "cross_project_human_decision",
    }
    if project_id:
        result["project_id"] = project_id
    if job_id:
        result["job_id"] = job_id
    return {
        "ok": False,
        "error": "cross_project_human_decision",
        "result": result,
        "events": [],
    }


def _registration_project_mismatch(
    *,
    registration_project_id: Any | None,
    resolved_project_id: Any | None,
) -> bool:
    """True when a callback registration pins a project and the resolved decision
    project does not match it. Account-level checks alone permit cross-project
    job targeting, so this binds the decision to the registration project."""
    registration = str(registration_project_id or "").strip()
    resolved = str(resolved_project_id or "").strip()
    if not registration:
        return False
    return bool(resolved) and resolved != registration


def _telegram_ingress_authenticated(
    request_payload: dict[str, Any],
    callback_info: dict[str, Any],
) -> bool:
    if _coerce_bool(callback_info.get("authenticated"), False):
        return True

    expected_secret = _as_str(
        callback_info.get("callback_secret")
        or request_payload.get("callback_secret")
        or _as_dict(request_payload.get("provider")).get("callback_secret")
    )
    if not expected_secret:
        return False

    headers = {
        str(key).strip().lower(): _as_str(value)
        for key, value in _as_dict(request_payload.get("headers")).items()
    }
    provided_secret = headers.get("x-telegram-bot-api-secret-token", "")
    return bool(
        provided_secret
        and hmac.compare_digest(provided_secret.encode("utf-8"), expected_secret.encode("utf-8"))
    )


def _ingress_event_response(
    request_payload: dict[str, Any],
    *,
    mutable_inbound_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = _as_dict(request_payload.get("payload") or request_payload.get("json"))
    query = _as_dict(request_payload.get("query"))
    provider = _as_dict(request_payload.get("provider"))
    provider_config = _as_dict(provider.get("provider_config"))
    callback_info = _as_dict(request_payload.get("callback"))
    if not _telegram_ingress_authenticated(request_payload, callback_info):
        return {
            "ok": False,
            "error": "unauthorized_telegram_ingress",
            "result": {
                "provider": "telegram",
                "ingress_type": "unauthorized",
            },
            "events": [],
        }
    project_id_hint = _coerce_project_id(
        payload.get("project_id"),
        payload.get("platform_project_id"),
        query.get("project_id"),
        callback_info.get("project_id"),
        provider_config.get("project_id"),
    )

    callback_query = _as_dict(payload.get("callback_query"))
    if callback_query:
        callback_runtime = _provider_runtime_config(provider)
        callback_token = _as_str(callback_runtime.get("token"))
        callback_query_id = _as_str(callback_query.get("id"))
        parsed = _parse_callback_data(_as_str(callback_query.get("data")))
        if not parsed:
            _answer_callback_query(
                callback_token,
                callback_query_id,
                text="Unsupported action.",
                show_alert=False,
            )
            return {
                "ok": True,
                "result": {
                    "provider": "telegram",
                    "ingress_type": "callback_query",
                    "ignored": True,
                    "reason": "unsupported_callback_data",
                },
                "events": [],
            }
        inbound_state = _normalize_inbound_state(_as_dict(callback_runtime.get("ingress_config")))
        if mutable_inbound_state is not None:
            mutable_inbound_state.clear()
            mutable_inbound_state.update(inbound_state)
            inbound_state = mutable_inbound_state
        resolved_ref = _resolve_pending_hitl_token(
            _runtime_logger(_REQUEST_RUNTIME_CONTEXT),
            project_id=_coerce_project_id(
                parsed.get("project_id"),
                callback_info.get("project_id"),
                project_id_hint,
                callback_runtime.get("project_id"),
            ),
            job_ref=_as_str(parsed.get("job_ref")),
            inbound_state=inbound_state,
            chat_id=_coerce_int(
                _as_dict(_as_dict(callback_query.get("message")).get("chat")).get("id")
            ),
        )
        project_id = _coerce_project_id(
            parsed.get("project_id"),
            resolved_ref.get("project_id"),
            callback_info.get("project_id"),
            project_id_hint,
            callback_runtime.get("project_id"),
        )
        job_id = _as_job_id(parsed.get("job_id") or resolved_ref.get("job_id"))
        approved = bool(parsed["approved"])
        action = _as_str(parsed["action"]) or ("approve" if approved else "reject")
        if not project_id or not job_id:
            _answer_callback_query(
                callback_token,
                callback_query_id,
                text="Approval context expired.",
                show_alert=True,
            )
            return {
                "ok": False,
                "error": "invalid_reject_comment_context",
                "message": _MESSAGE_THE_APPROVAL_CONTEXT_COULD_NOT_BE_RESOLVED,
                "events": [],
            }
        if _registration_project_mismatch(
            registration_project_id=callback_info.get("project_id"),
            resolved_project_id=project_id,
        ):
            _answer_callback_query(
                callback_token,
                callback_query_id,
                text="This approval is not valid for this project.",
                show_alert=True,
            )
            return _cross_project_human_decision_response(
                ingress_type="callback_query",
                project_id=_as_str(callback_info.get("project_id")) or None,
                job_id=str(job_id),
            )
        message_payload = _as_dict(callback_query.get("message"))
        callback_chat_id = _coerce_int(_as_dict(message_payload.get("chat")).get("id"))
        callback_message_id = _coerce_positive_int(message_payload.get("message_id"))
        if not _human_decision_channel_for_chat(callback_runtime, callback_chat_id):
            _answer_callback_query(
                callback_token,
                callback_query_id,
                text="This chat is not authorized for approvals.",
                show_alert=True,
            )
            return _unauthorized_human_decision_response(
                ingress_type="callback_query",
                project_id=project_id,
                job_id=str(job_id),
            )
        allowed_actions = _normalize_allowed_actions(resolved_ref.get("allowed_actions"))
        if action not in allowed_actions:
            _answer_callback_query(
                callback_token,
                callback_query_id,
                text="This action is no longer available for the current approval.",
                show_alert=True,
            )
            return {
                "ok": False,
                "error": "approval_action_not_allowed",
                "message": "The selected action is not allowed by this approval gate.",
                "result": {
                    "provider": "telegram",
                    "ingress_type": "callback_query",
                    "project_id": project_id,
                    "job_id": job_id,
                    "action": action,
                    "allowed_actions": allowed_actions,
                },
                "events": [],
            }
        actor = _ingress_actor_from_user(_as_dict(callback_query.get("from")))
        source_id = _as_str(callback_query.get("id")) or (
            f"callback:{callback_chat_id}:{callback_message_id}:{action}"
            if callback_chat_id is not None and callback_message_id
            else f"callback:{job_id}:{action}"
        )
        if action == "reject":
            _bind_pending_hitl_message_ref(
                _runtime_logger(_REQUEST_RUNTIME_CONTEXT),
                project_id=project_id,
                job_ref=_as_str(parsed.get("job_ref")),
                inbound_state=inbound_state,
                chat_id=callback_chat_id,
                message_id=callback_message_id,
            )
            prompt_result = _send_reject_reason_prompt(
                callback_token,
                callback_chat_id,
                project_id=project_id,
                job_id=str(job_id),
                job_ref=_as_str(parsed.get("job_ref")),
                context_label="this request",
                reply_to_message_id=callback_message_id,
            )
            prompt_sent = bool(prompt_result.get("ok"))
            prompt_status = _as_str(prompt_result.get("external_effect_status")) or (
                "succeeded" if prompt_sent else "failed"
            )
            callback_answered = _answer_callback_query(
                callback_token,
                callback_query_id,
                text=(
                    "Reply to the prompt with a rejection reason."
                    if prompt_sent
                    else "The rejection prompt could not be confirmed. Please try again."
                ),
                show_alert=True,
            )
            return {
                "ok": True,
                "result": {
                    "provider": "telegram",
                    "ingress_type": "callback_query",
                    "project_id": project_id,
                    "job_id": job_id,
                    "action": action,
                    "approved": False,
                    "pending_reason": prompt_sent,
                    "callback_answered": callback_answered,
                    "prompt_sent": prompt_sent,
                    "prompt_status": prompt_status,
                    "prompt_error": _as_str(prompt_result.get("error")),
                },
                "events": [],
            }
        consumed_ref = _resolve_pending_hitl_token(
            _runtime_logger(_REQUEST_RUNTIME_CONTEXT),
            project_id=project_id,
            job_ref=_as_str(parsed.get("job_ref")),
            inbound_state=inbound_state,
            chat_id=callback_chat_id,
        )
        if not consumed_ref:
            _answer_callback_query(
                callback_token,
                callback_query_id,
                text="This approval action has expired or was already used.",
                show_alert=True,
            )
            return {
                "ok": False,
                "error": "invalid_reject_comment_context",
                "message": "The approval context was expired or already used.",
                "events": [],
            }
        event_key = f"telegram.callback_query.{action}"
        interaction_payload = _ingress_interaction_payload(
            approved=approved,
            action=action,
            text=_as_str(message_payload.get("text")),
            comment="",
            job_id=job_id,
            approval_id=_as_str(consumed_ref.get("approval_id")),
            project_id=project_id,
            source_id=source_id,
            event_key=event_key,
            actor=actor,
            meta={
                "channel_id": _as_str(consumed_ref.get("channel_id")),
                "chat_id": callback_chat_id,
                "message_id": callback_message_id,
            },
        )
        _answer_callback_query(
            callback_token,
            callback_query_id,
            text="Decision received.",
            show_alert=False,
        )
        events: list[dict[str, Any]] = [
            {
                "event_type": "notification.interaction_received",
                "severity": "info",
                "summary": f"Telegram interaction received for job {job_id}",
                "project_id": project_id,
                "job_id": job_id,
                "source": "telegram.callback_query",
                "payload": interaction_payload,
            }
        ]
        return {
            "ok": True,
            "result": {
                "provider": "telegram",
                "ingress_type": "callback_query",
                "project_id": project_id,
                "job_id": job_id,
                "action": action,
                "approved": approved,
            },
            "events": events,
        }

    message = _as_dict(payload.get("message"))
    if message:
        runtime_logger = _runtime_logger(_REQUEST_RUNTIME_CONTEXT)
        message_inbound_state = _normalize_inbound_state(
            _as_dict(_provider_runtime_config(provider).get("ingress_config"))
        )
        if mutable_inbound_state is not None:
            mutable_inbound_state.clear()
            mutable_inbound_state.update(message_inbound_state)
            message_inbound_state = mutable_inbound_state
        parsed_command = _parse_approval_command(_as_str(message.get("text")))
        if not parsed_command:
            reject_prompt_candidate = "Flow Steward reject context:" in _as_str(
                _as_dict(message.get("reply_to_message")).get("text")
            )
            parsed_command = _parse_reject_prompt_reply(message)
            if reject_prompt_candidate and not parsed_command:
                return {
                    "ok": False,
                    "error": "invalid_reject_comment_context",
                    "message": _MESSAGE_THE_APPROVAL_CONTEXT_COULD_NOT_BE_RESOLVED,
                    "events": [],
                }
        if not parsed_command:
            message_runtime = _provider_runtime_config(provider)
            if _workflow_trigger_channel_for_chat(
                message_runtime, _coerce_int(_as_dict(message.get("chat")).get("id"))
            ):
                event = _message_received_event(
                    payload,
                    message,
                    project_id=project_id_hint,
                    connection_id=_as_str(
                        callback_info.get("connection_id") or provider.get("connection_id")
                    ),
                )
                return {
                    "ok": True,
                    "result": {
                        "provider": "telegram",
                        "ingress_type": "message",
                        "event_type": MESSAGE_RECEIVED_EVENT,
                    },
                    "events": [event],
                }
            return {
                "ok": True,
                "result": {
                    "provider": "telegram",
                    "ingress_type": "message",
                    "ignored": True,
                    "reason": "unsupported_message",
                },
                "events": [],
            }
        approved = bool(parsed_command["approved"])
        action = _as_str(parsed_command["action"]) or ("approve" if approved else "reject")
        comment = _as_str(parsed_command.get("comment"))
        resolved_project_id = _coerce_project_id(parsed_command.get("project_id"), project_id_hint)
        resolved_ref = _resolve_pending_hitl_token(
            runtime_logger,
            project_id=resolved_project_id or callback_info.get("project_id") or project_id_hint,
            job_ref=_as_str(parsed_command.get("job_ref")),
            inbound_state=message_inbound_state,
        )
        resolved_project_id = _coerce_project_id(
            parsed_command.get("project_id"),
            resolved_ref.get("project_id"),
            callback_info.get("project_id"),
            project_id_hint,
        )
        # A text /approve|/reject must present the per-job pending HITL token, which
        # _resolve_pending_hitl_token validates against the stored token map.
        # ``direct_job_id`` is only set by trusted server-minted inline-button
        # callbacks (_parse_callback_data). A raw job UUID typed into chat is NOT
        # accepted as a fallback — possession of the pending token is required.
        job_id = _as_job_id(resolved_ref.get("job_id"))
        if not job_id:
            return {
                "ok": False,
                "error": "invalid_reject_comment_context",
                "message": _MESSAGE_THE_APPROVAL_CONTEXT_COULD_NOT_BE_RESOLVED,
                "events": [],
            }
        if _registration_project_mismatch(
            registration_project_id=callback_info.get("project_id"),
            resolved_project_id=resolved_project_id,
        ):
            return _cross_project_human_decision_response(
                ingress_type="message",
                project_id=_as_str(callback_info.get("project_id")) or None,
                job_id=str(job_id),
            )
        actor = _ingress_actor_from_user(_as_dict(message.get("from")))
        message_chat_id = _coerce_int(_as_dict(message.get("chat")).get("id"))
        if not _human_decision_channel_for_chat(
            _provider_runtime_config(provider),
            message_chat_id,
        ):
            return _unauthorized_human_decision_response(
                ingress_type="message",
                project_id=resolved_project_id,
                job_id=str(job_id),
            )
        allowed_actions = _normalize_allowed_actions(resolved_ref.get("allowed_actions"))
        if action not in allowed_actions:
            return {
                "ok": False,
                "error": "approval_action_not_allowed",
                "message": "The selected action is not allowed by this approval gate.",
                "result": {
                    "provider": "telegram",
                    "ingress_type": "message",
                    "project_id": resolved_project_id,
                    "job_id": job_id,
                    "action": action,
                    "allowed_actions": allowed_actions,
                },
                "events": [],
            }
        job_ref = _as_str(parsed_command.get("job_ref"))
        if job_ref and resolved_ref:
            consumed_ref = _resolve_pending_hitl_token(
                runtime_logger,
                project_id=str(resolved_project_id),
                job_ref=job_ref,
                inbound_state=message_inbound_state,
                chat_id=message_chat_id,
            )
            if not consumed_ref:
                return {
                    "ok": False,
                    "error": "invalid_reject_comment_context",
                    "message": "The approval context was expired or already used.",
                    "events": [],
                }
        decision_ref = consumed_ref if job_ref else resolved_ref
        message_id = _coerce_positive_int(message.get("message_id"))
        approval_message_ref = _coerce_positive_int(
            decision_ref.get("provider_message_id") or decision_ref.get("approval_message_ref")
        )
        source_id = (
            f"message:{message_chat_id}:{message_id}:{action}"
            if message_chat_id is not None and message_id
            else _as_str(message.get("message_id"))
        ) or f"message:{job_id}:{action}:{_as_str(actor.get('username'))}"
        event_key = f"telegram.message.{action}"
        interaction_payload = _ingress_interaction_payload(
            approved=approved,
            action=action,
            text=comment or action,
            comment=comment,
            job_id=job_id,
            approval_id=_as_str(decision_ref.get("approval_id")),
            project_id=resolved_project_id,
            source_id=source_id,
            event_key=event_key,
            actor=actor,
            meta={
                "channel_id": _as_str(decision_ref.get("channel_id")),
                "chat_id": message_chat_id,
                "message_id": approval_message_ref or message_id,
            },
        )
        result: dict[str, Any] = {
            "provider": "telegram",
            "ingress_type": "message",
            "job_id": job_id,
            "action": action,
            "approved": approved,
        }
        if resolved_project_id:
            result["project_id"] = resolved_project_id
        elif project_id_hint:
            result["project_id"] = project_id_hint
        events = [
            {
                "event_type": "notification.interaction_received",
                "severity": "info",
                "summary": f"Telegram command received for job {job_id}",
                "project_id": resolved_project_id,
                "job_id": job_id,
                "source": "telegram.message",
                "payload": interaction_payload,
            }
        ]
        if comment:
            events.append(
                {
                    "event_type": "external.comment.received",
                    "severity": "info",
                    "summary": f"Telegram {action} for job {job_id}",
                    "project_id": resolved_project_id,
                    "job_id": job_id,
                    "source_id": source_id,
                    "event_key": event_key,
                    "source": "telegram.message",
                    "payload": interaction_payload,
                }
            )
        return {
            "ok": True,
            "result": result,
            "events": events,
        }

    return {
        "ok": True,
        "result": {
            "provider": "telegram",
            "ingress_type": "ignored",
            "ignored": True,
        },
        "events": [],
    }


def _handle_ingress_event_payload(action_payload: dict[str, Any]) -> tuple[dict, int]:
    request_payload = _as_dict(action_payload.get("request"))
    if not request_payload:
        input_payload = _as_dict(action_payload.get("input"))
        request_payload = _as_dict(input_payload.get("request")) if input_payload else {}
        callback_payload = _as_dict(input_payload.get("callback")) if input_payload else {}
        if callback_payload and "callback" not in request_payload:
            request_payload = {**request_payload, "callback": callback_payload}
    elif "callback" not in request_payload:
        callback_payload = _as_dict(action_payload.get("callback"))
        if callback_payload:
            request_payload = {**request_payload, "callback": callback_payload}
    runtime_context = _as_dict(action_payload.get("runtime_context")) or _REQUEST_RUNTIME_CONTEXT
    runtime_resources = _as_dict(runtime_context.get("resources"))
    project_connection = _as_dict(runtime_resources.get("project_connection"))
    provider_secrets = _as_dict(runtime_resources.get("provider_secrets"))
    if project_connection or provider_secrets:
        provider = _as_dict(request_payload.get("provider"))
        connection_config = _as_dict(
            project_connection.get("connection_config") or project_connection.get("config")
        )
        request_payload = {
            **request_payload,
            "provider": {
                **provider,
                "provider_config": connection_config or _as_dict(provider.get("provider_config")),
                "provider_secrets": provider_secrets,
                "connection_id": _as_str(project_connection.get("connection_id")),
                "project_id": _coerce_project_id(
                    project_connection.get("project_id"),
                    provider.get("project_id"),
                ),
                "ingress_config": _as_dict(project_connection.get("ingress_config"))
                or _as_dict(provider.get("ingress_config")),
            },
        }
    result = _ingress_event_response(request_payload)
    return result, 0

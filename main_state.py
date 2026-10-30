from __future__ import annotations

import copy
import html
import os
import re
import secrets
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib import parse

from flowsteward_extension_sdk import RuntimeResources, host_mutation

APPLICATION_JSON_VALUE = "application/json"
APPROVAL_REQUESTED_VALUE = "approval.requested"
JOB_AWAITING_APPROVAL_VALUE = "job.awaiting_approval"
PROVIDER_KEY = "flowsteward.telegram"

_REQUEST_RUNTIME_CONTEXT: dict[str, Any] = {}
_REQUEST_HOST_MUTATIONS: list[dict[str, Any]] = []
_POLLING_INTERVAL_OPTIONS = {15, 30, 60, 120, 300}
_SUPPORTED_UPDATE_TYPES = ["message", "callback_query", "my_chat_member"]
_HITL_TOKEN_TTL = timedelta(hours=1)
_SUPPORTED_HITL_ACTIONS = frozenset({"approve", "reject", "abort"})


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _secret_refs_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        payload = copy.deepcopy(value)
        refs = value.get("refs")
        if isinstance(refs, dict):
            payload["refs"] = {
                _as_str(key): _as_str(ref)
                for key, ref in refs.items()
                if _as_str(key) and _as_str(ref)
            }
        return payload
    if isinstance(value, list):
        refs = {_as_str(item): _as_str(item) for item in value if _as_str(item)}
        return {"refs": refs} if refs else {}
    return {}


def _as_str(value: Any) -> str:
    return str(value or "").strip()


def _coerce_positive_int(value: Any) -> int | None:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _humanize_token(value: Any) -> str:
    text = _as_str(value)
    if not text:
        return ""
    return " ".join(part.capitalize() for part in text.replace("-", " ").replace("_", " ").split())


def _event_label(value: Any) -> str:
    text = _as_str(value)
    if not text:
        return ""
    event_map = {
        "workflow.automation.notify": "Workflow notify",
        APPROVAL_REQUESTED_VALUE: "Approval requested",
        "job.failed": "Job failed",
        "job.completed": "Job completed",
        "test.notification": "Test delivery",
    }
    return event_map.get(text, _humanize_token(text.replace(".", " ")))


def _safe_iso_datetime(value: Any) -> str:
    normalized = _json_safe_value(value)
    return _as_str(normalized)


def _html_escape(value: Any) -> str:
    return html.escape(_as_str(value), quote=True)


def _workflow_display_name(bl: Any, workflow_id: str) -> str:
    normalized_workflow_id = _as_str(workflow_id)
    if not normalized_workflow_id or bl is None or not hasattr(bl, "get_workflow"):
        return ""
    try:
        workflow = bl.get_workflow(normalized_workflow_id) or {}
    except Exception:
        workflow = {}
    return _as_str(_as_dict(workflow).get("display_name"))


def _job_context(bl: Any, job_id: str) -> dict[str, Any]:
    normalized_job_id = _as_str(job_id)
    if not normalized_job_id or bl is None or not hasattr(bl, "get_job"):
        return {}
    try:
        return _as_dict(bl.get_job(normalized_job_id) or {})
    except Exception:
        return {}


def _scope_value(event: dict[str, Any], payload: dict[str, Any], key: str) -> str:
    return _as_str(event.get(key)) or _as_str(payload.get(key))


def _approval_job_id(event: dict[str, Any]) -> str:
    """The job that holds the approval a notification asks to decide.

    A child's approval delivered to a waiting ancestor (``propagated: true``)
    is decided on ``approval_job_id``, never on the ancestor's ``job_id``;
    without it there is no job to decide on.
    """
    payload = _as_dict(event.get("payload"))
    if payload.get("propagated") is True:
        return _as_str(payload.get("approval_job_id"))
    return _as_job_id(
        event.get("job_id") if event.get("job_id") is not None else payload.get("job_id")
    )


def _message_context(event: dict[str, Any], provider: dict | None = None) -> dict[str, str]:
    _ = provider
    payload = _as_dict(event.get("payload"))
    bl = _runtime_logger(_REQUEST_RUNTIME_CONTEXT)
    propagated = payload.get("propagated") is True
    job_id = (
        _approval_job_id(event)
        if propagated
        else _as_str(event.get("job_id") or payload.get("job_id"))
    )
    job = _job_context(bl, job_id)
    workflow_id = _as_str(
        event.get("workflow_id")
        or payload.get("workflow_id")
        or payload.get("selected_workflow_id")
        or job.get("selected_workflow")
    )
    workflow_label = _as_str(
        payload.get("workflow_label")
        or payload.get("workflow_name")
        or payload.get("workflow_display_name")
        or _workflow_display_name(bl, workflow_id)
    ) or _humanize_token(workflow_id)
    # The final execution scope: a Group line only for a real group, a For each
    # line only for a For each, a Step line only for a step. The root of a
    # workflow has no label of its own.
    group_id = _scope_value(event, payload, "group_id")
    for_each_id = _scope_value(event, payload, "for_each_id")
    step_id = _scope_value(event, payload, "step_id")
    group_label = (_as_str(payload.get("group_label")) or group_id) if group_id else ""
    for_each_label = (_as_str(payload.get("for_each_label")) or for_each_id) if for_each_id else ""
    step_label = (
        (_as_str(payload.get("step_label") or payload.get("step_name")) or step_id)
        if step_id
        else ""
    )
    summary = _as_str(
        payload.get("message")
        or payload.get("summary")
        or event.get("summary")
        or payload.get("text")
        or payload.get("detail")
        or ""
    )
    task_summary = _as_str(payload.get("task_summary") or job.get("task_summary"))
    return {
        "project_id": _as_str(event.get("project_id") or payload.get("project_id")),
        "job_id": job_id,
        "workflow_id": workflow_id,
        "workflow_label": workflow_label,
        "group_id": group_id,
        "group_label": group_label,
        "for_each_id": for_each_id,
        "for_each_label": for_each_label,
        "step_id": step_id,
        "step_execution_id": _scope_value(event, payload, "step_execution_id"),
        "step_label": step_label,
        "task_summary": task_summary,
        "summary": summary,
    }


def _workflow_url(project_id: str, workflow_id: str) -> str:
    base_url = _as_str(os.getenv("FS_PUBLIC_BASE_URL"))
    normalized_workflow_id = _as_str(workflow_id)
    if not base_url or not normalized_workflow_id:
        return ""
    path = f"/registry/workflows/{parse.quote(normalized_workflow_id, safe='')}/builder"
    query = f"?project_id={parse.quote(_as_str(project_id), safe='')}" if project_id else ""
    return f"{base_url.rstrip('/')}{path}{query}"


def _job_url(project_id: str, job_id: str) -> str:
    base_url = _as_str(os.getenv("FS_PUBLIC_BASE_URL"))
    normalized_job_id = _as_str(job_id)
    if not base_url or not normalized_job_id:
        return ""
    path = f"/jobs/{parse.quote(normalized_job_id, safe='')}"
    query = f"?project_id={parse.quote(_as_str(project_id), safe='')}" if project_id else ""
    return f"{base_url.rstrip('/')}{path}{query}"


_APPROVAL_CMD = re.compile(
    r"^/?(?P<cmd>approve|reject|abort)(?:@(?a:\w)+)?(?:\s+(?P<job_ref>(?a:[\w-])+))?(?:\s+(?P<comment>.+))?$",
    flags=re.IGNORECASE,
)
_REJECT_PROMPT_CONTEXT = re.compile(
    r"Flow Steward reject context:\s*project_id=(?P<project_id>[^\s]+)\s+(?:job_ref|job_id)=(?P<job_ref>(?a:[\w-])+)",
    flags=re.IGNORECASE,
)


def _as_job_id(value: Any) -> str:
    return _as_str(value)


def _looks_like_uuid(value: Any) -> bool:
    normalized = _as_str(value)
    if not normalized:
        return False
    try:
        uuid.UUID(normalized)
    except Exception:
        return False
    return True


def _normalize_allowed_actions(value: Any) -> list[str]:
    """Normalize Core's explicit gate allow-list and fail closed when malformed."""
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for item in value:
        action = _as_str(item).lower()
        if action in _SUPPORTED_HITL_ACTIONS and action not in normalized:
            normalized.append(action)
    return normalized


def _build_callback_data(*, action: str, job_ref: str) -> str:
    decision = {"approve": "a", "reject": "r", "abort": "b"}.get(_as_str(action).lower(), "")
    if not decision:
        return ""
    return f"tgx:{decision}:{_as_str(job_ref)}"[:64]


def _parse_callback_data(raw_value: str) -> dict[str, Any]:
    raw = _as_str(raw_value)
    if not raw.startswith("tgx:"):
        return {}
    parts = raw.split(":")
    if len(parts) != 3:
        return {}
    prefix, decision, job_ref = parts
    actions = {"a": "approve", "r": "reject", "b": "abort"}
    if prefix != "tgx" or decision not in actions or not _as_str(job_ref):
        return {}
    action = actions[decision]
    return {
        "approved": action == "approve",
        "action": action,
        "job_ref": _as_str(job_ref),
    }


def _parse_approval_command(text: str) -> dict[str, Any]:
    raw = _as_str(text)
    if not raw:
        return {}
    match = _APPROVAL_CMD.match(raw)
    if not match:
        return {}
    command = _as_str(match.group("cmd")).lower()
    approved = command == "approve"
    job_ref = _as_str(match.group("job_ref"))
    if not job_ref:
        return {}
    comment = _as_str(match.group("comment"))
    return {
        "approved": approved,
        "action": command,
        "job_ref": job_ref,
        "comment": comment,
        "text": raw,
    }


def _parse_reject_prompt_reply(message: dict[str, Any]) -> dict[str, Any]:
    reply_to = _as_dict(message.get("reply_to_message"))
    if not _coerce_bool(_as_dict(reply_to.get("from")).get("is_bot"), False):
        return {}
    reply_text = _as_str(reply_to.get("text"))
    if not reply_text:
        return {}
    match = _REJECT_PROMPT_CONTEXT.search(reply_text)
    if not match:
        return {}
    project_id = _coerce_project_id(match.group("project_id"))
    job_ref = _as_str(match.group("job_ref"))
    comment = _as_str(message.get("text"))
    if not project_id or not job_ref or not comment:
        return {}
    return {
        "approved": False,
        "action": "reject",
        "project_id": project_id,
        "job_ref": job_ref,
        "comment": comment,
        "text": comment,
    }


class RuntimePayloadStore:
    """Adapter over host-provided runtime payload data, not host services."""

    def __init__(self, runtime_context: dict[str, Any] | None = None):
        self.context = _as_dict(runtime_context)
        self.runtime_resources = RuntimeResources.from_context(self.context)
        self.resources = self.runtime_resources.values
        self.host_mutations: list[dict[str, Any]] = []

    def _connection_payload(self) -> dict[str, Any]:
        for key in ("connection", "project_connection"):
            payload = _as_dict(self.context.get(key))
            if payload:
                return dict(payload)
        payload = _as_dict(self.resources.get("project_connection"))
        if payload:
            return dict(payload)
        target = _as_dict(self.context.get("target"))
        payload = _as_dict(target.get("connection"))
        return dict(payload) if payload else {}

    def get_project_connection(
        self, project_id: str, extension_id: str, enabled_only: bool = False
    ) -> dict[str, Any]:
        row = self._connection_payload()
        if not row:
            return {}
        if _as_str(row.get("extension_id") or extension_id) != extension_id:
            return {}
        if enabled_only and row.get("enabled") is False:
            return {}
        row.setdefault("project_id", project_id)
        row.setdefault("extension_id", extension_id)
        if "connection_config" not in row and isinstance(row.get("config"), dict):
            row["connection_config"] = dict(row["config"])
        if "secret_refs" not in row and isinstance(row.get("secrets"), dict):
            row["secret_refs"] = {"refs": {key: key for key in row["secrets"]}}
        return row

    def list_project_connections(self, project_id: str, enabled_only: bool = False) -> list[dict]:
        row = self.get_project_connection(project_id, PROVIDER_KEY, enabled_only)
        return [row] if row else []

    def upsert_project_connection(self, **payload: Any) -> None:
        update = dict(payload)
        self.resources["project_connection"] = update
        self.host_mutations.append(host_mutation("project_connection_upsert", update))
        _REQUEST_HOST_MUTATIONS.append(self.host_mutations[-1])

    def consume_project_connection_ingress_token(self, **payload: Any) -> bool:
        token = _as_str(payload.get("token"))
        row = self._connection_payload()
        token_map = dict(_as_dict(_as_dict(row.get("ingress_config")).get("pending_hitl_tokens")))
        if not token or token not in token_map:
            return False
        token_map.pop(token, None)
        next_ingress = _normalize_inbound_state(_as_dict(row.get("ingress_config")))
        next_ingress["pending_hitl_tokens"] = token_map
        row["ingress_config"] = next_ingress
        mutation_payload = {
            "connection_id": _as_str(row.get("connection_id")),
            "extension_install_id": _as_str(row.get("extension_install_id")),
            "connection_type": _as_str(row.get("connection_type")) or "telegram_bot",
            "display_name": _as_str(row.get("display_name")),
            "consume_ingress_token": token,
        }
        mutation = host_mutation("project_connection_upsert", mutation_payload)
        self.host_mutations.append(mutation)
        _REQUEST_HOST_MUTATIONS.append(mutation)
        return True

    def get_provider_secret(self, **payload: Any) -> str:
        secret_key = _as_str(payload.get("secret_key"))
        return _as_str(_as_dict(self.resources.get("provider_secrets")).get(secret_key))

    def list_notification_delivery_logs(self, **payload: Any) -> list[dict[str, Any]]:
        _ = payload
        return [
            dict(row)
            for row in _as_list(self.resources.get("notification_deliveries"))
            if isinstance(row, dict)
        ]

    def log_notification_delivery(self, **payload: Any) -> dict[str, Any]:
        mutation = host_mutation("notification_delivery_log", payload)
        self.host_mutations.append(mutation)
        _REQUEST_HOST_MUTATIONS.append(mutation)
        return dict(payload)

    def list_notification_routes(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        _ = (args, kwargs)
        return [
            dict(row)
            for row in _as_list(self.resources.get("notification_routes"))
            if isinstance(row, dict)
        ]

    def save_notification_routes(self, project_id: str, routes: list[dict] | None = None) -> None:
        _ = project_id
        normalized = [dict(row) for row in routes or [] if isinstance(row, dict)]
        self.resources["notification_routes"] = normalized
        mutation = host_mutation("notification_routes_save", {"routes": normalized})
        self.host_mutations.append(mutation)
        _REQUEST_HOST_MUTATIONS.append(mutation)

    def list_extension_callback_registrations(self, **payload: Any) -> list[dict[str, Any]]:
        _ = payload
        return [
            dict(row)
            for row in _as_list(self.resources.get("callback_registrations"))
            if isinstance(row, dict)
        ]

    def list_latest_provider_health(self, **payload: Any) -> list[dict[str, Any]]:
        _ = payload
        return [
            dict(row)
            for row in _as_list(self.resources.get("provider_health"))
            if isinstance(row, dict)
        ]


def _runtime_logger(runtime_context: dict[str, Any] | None = None):
    context = _as_dict(runtime_context)
    if any(key in context for key in ("connection", "project_connection", "target", "resources")):
        return RuntimePayloadStore(context)
    return None


def _resolve_runtime_logger(runtime_context: dict[str, Any] | None = None):
    try:
        return _runtime_logger(runtime_context)
    except TypeError:
        return _runtime_logger()


def _coerce_project_id(*values: Any) -> str | None:
    for value in values:
        if value in (None, ""):
            continue
        if isinstance(value, (dict, list, tuple, set)):
            continue
        normalized = str(value).strip()
        if normalized:
            return normalized
    return None


def _apply_grid_params(items: list[dict], params: dict | None) -> tuple[list[dict], int, int, int]:
    payload = _as_dict(params)
    try:
        page = max(1, int(payload.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = max(1, min(200, int(payload.get("page_size") or payload.get("limit") or 50)))
    except (TypeError, ValueError):
        page_size = 50
    total = len(items)
    start = (page - 1) * page_size
    end = start + page_size
    return items[start:end], page, page_size, total


def _telegram_connection_row_for_project(bl, project_id: str) -> dict[str, Any]:
    if bl is None:
        return {}
    if hasattr(bl, "get_project_connection"):
        try:
            row = bl.get_project_connection(
                project_id=str(project_id), extension_id=PROVIDER_KEY, enabled_only=False
            )
        except Exception:
            row = None
        if isinstance(row, dict):
            return dict(row)
    if hasattr(bl, "list_project_connections"):
        try:
            rows = bl.list_project_connections(str(project_id), enabled_only=False) or []
        except Exception:
            rows = []
        for row in rows:
            payload = _as_dict(row)
            if str(payload.get("extension_id") or "").strip().lower() == PROVIDER_KEY:
                return dict(payload)
    return {}


def _save_telegram_connection(
    bl,
    project_id: str,
    *,
    connection_config: dict[str, Any] | None = None,
    ingress_config: dict[str, Any] | None = None,
    secret_refs: list[str] | dict[str, Any] | None = None,
) -> bool:
    if bl is None or not hasattr(bl, "upsert_project_connection"):
        return False
    existing = _telegram_connection_row_for_project(bl, project_id)
    try:
        bl.upsert_project_connection(
            connection_id=_as_str(existing.get("connection_id")),
            project_id=str(project_id),
            extension_id=PROVIDER_KEY,
            account_id=_as_str(existing.get("account_id"))
            or _as_str(_REQUEST_RUNTIME_CONTEXT.get("account_id"))
            or None,
            connection_kind=str(existing.get("connection_kind") or "extension").strip()
            or "extension",
            connection_type=_as_str(existing.get("connection_type")) or "telegram_bot",
            extension_install_id=_as_str(existing.get("extension_install_id"))
            or _as_str(_REQUEST_RUNTIME_CONTEXT.get("extension_install_id")),
            display_name=_as_str(existing.get("display_name")),
            enabled=_coerce_bool(existing.get("enabled"), True),
            connection_config=dict(
                connection_config
                if connection_config is not None
                else _as_dict(existing.get("connection_config"))
            ),
            secret_refs=_secret_refs_payload(
                secret_refs if secret_refs is not None else existing.get("secret_refs")
            ),
            connection_routes=list(existing.get("connection_routes") or []),
            ingress_config=dict(
                ingress_config
                if ingress_config is not None
                else _as_dict(existing.get("ingress_config"))
            ),
            workflow_mappings=_as_dict(existing.get("workflow_mappings")),
        )
        return True
    except Exception:
        return False


def _save_telegram_connection_config(bl, project_id: str, payload: dict[str, Any]) -> bool:
    return _save_telegram_connection(bl, project_id, connection_config=payload)


def _save_telegram_connection_ingress(bl, project_id: str, payload: dict[str, Any]) -> bool:
    return _save_telegram_connection(bl, project_id, ingress_config=payload)


def _save_telegram_connection_secret_refs(
    bl, project_id: str, secret_refs: list[str] | dict[str, Any]
) -> bool:
    return _save_telegram_connection(bl, project_id, secret_refs=secret_refs)


def _provider_defaults_from_config(config: dict[str, Any]) -> dict[str, Any]:
    payload = _as_dict(config)
    return {
        "disable_web_page_preview": _coerce_bool(
            payload.get("disable_web_page_preview"),
            True,
        ),
        "message_prefix": _as_str(payload.get("message_prefix")),
    }


def _channel_items_from_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    channels: list[dict[str, Any]] = []
    for raw_item in _as_list(_as_dict(config).get("channels")):
        item = _as_dict(raw_item)
        channel_id = _as_str(item.get("channel_id"))
        if not channel_id:
            continue
        capabilities = _as_dict(item.get("capabilities"))
        channels.append(
            {
                "channel_id": channel_id,
                "display_name": _as_str(item.get("display_name")) or channel_id,
                "chat_id": _as_str(item.get("chat_id")),
                "enabled": _coerce_bool(item.get("enabled"), True),
                "capabilities": {
                    "notify": _coerce_bool(capabilities.get("notify"), False),
                    "human_decision": _coerce_bool(
                        capabilities.get("human_decision"),
                        False,
                    ),
                    # Closed by default: only a chat the owner switched on may
                    # start workflows with its incoming messages.
                    "workflow_trigger": _coerce_bool(
                        capabilities.get("workflow_trigger"),
                        False,
                    ),
                },
            }
        )

    return channels


def _channel_items_for_ui(channels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **dict(channel),
            "_meta": {
                "existing": True,
                "immutable_fields": ["channel_id"],
                "original_channel_id": _as_str(channel.get("channel_id")),
            },
        }
        for channel in channels
    ]


def _replace_channels_in_config(
    config: dict[str, Any], channels: list[dict[str, Any]]
) -> dict[str, Any]:
    payload = {**_as_dict(config), "channels": channels}
    payload.pop("chat_id", None)
    return payload


def _normalize_polling_interval(value: Any) -> int:
    normalized = _coerce_positive_int(value) or 60
    if normalized not in _POLLING_INTERVAL_OPTIONS:
        return 60
    return normalized


def _normalize_chat_username(value: Any) -> str:
    raw = _as_str(value)
    if not raw:
        return ""
    if raw.startswith("@"):
        raw = raw[1:]
    if raw.startswith(("https://t.me/", "http://t.me/")):
        parsed = parse.urlparse(raw)
        raw = parsed.path.strip("/").split("/", 1)[0]
    return raw.strip()


def _normalize_chat_link(value: Any) -> dict[str, Any]:
    raw = _as_str(value)
    if not raw:
        return {}
    if raw.startswith("@"):
        username = _normalize_chat_username(raw)
        return {"kind": "public_username", "username": username, "chat_ref": f"@{username}"}
    if raw.startswith(("https://t.me/", "http://t.me/")):
        parsed = parse.urlparse(raw)
        path = parsed.path.strip("/")
        if path.startswith(("+", "joinchat/")):
            return {"kind": "invite_link", "invite_link": raw}
        username = _normalize_chat_username(raw)
        if username:
            return {"kind": "public_username", "username": username, "chat_ref": f"@{username}"}
    return {}


def _webhook_projection(status_payload: dict[str, Any]) -> dict[str, Any]:
    payload = _as_dict(status_payload)
    webhook_state = _as_str(payload.get("status")).lower()
    if not webhook_state:
        if _coerce_bool(payload.get("url_configured"), False):
            webhook_state = "healthy"
        elif payload:
            webhook_state = "not_configured"
        else:
            webhook_state = "unknown"
    return {
        "status": webhook_state,
        "url_configured": _coerce_bool(payload.get("url_configured"), False),
        "pending_update_count": _coerce_int(payload.get("pending_update_count")) or 0,
        "last_error_message": _as_str(payload.get("last_error_message") or payload.get("error")),
        "last_checked_at": _safe_iso_datetime(payload.get("last_checked_at") or datetime.now(UTC)),
        "callback_registration_id": _as_str(payload.get("callback_registration_id")),
    }


def _discovered_chat_row(
    *,
    chat_id: str,
    title: str,
    chat_type: str,
    username: str = "",
    source: str = "",
    invite_link: str = "",
    last_seen_at: str = "",
) -> dict[str, Any]:
    return {
        "chat_id": _as_str(chat_id),
        "title": _as_str(title),
        "type": _as_str(chat_type) or "unknown",
        "username": _as_str(username),
        "source": _as_str(source),
        "invite_link": _as_str(invite_link),
        "last_seen_at": _as_str(last_seen_at) or datetime.now(UTC).isoformat(),
    }


def _slugify_channel_id(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", _as_str(value).lower()).strip("_")
    return text[:48] or "telegram_channel"


def _upsert_discovered_chat(
    discovered_chats: list[dict[str, Any]],
    row: dict[str, Any],
) -> list[dict[str, Any]]:
    normalized_chat_id = _as_str(_as_dict(row).get("chat_id"))
    if not normalized_chat_id:
        return discovered_chats
    next_rows: list[dict[str, Any]] = []
    matched = False
    for item in discovered_chats:
        payload = _as_dict(item)
        if _as_str(payload.get("chat_id")) != normalized_chat_id:
            next_rows.append(payload)
            continue
        merged = dict(payload)
        merged.update(
            {key: value for key, value in _as_dict(row).items() if value not in (None, "")}
        )
        merged["last_seen_at"] = (
            _as_str(_as_dict(row).get("last_seen_at"))
            or _as_str(payload.get("last_seen_at"))
            or datetime.now(UTC).isoformat()
        )
        next_rows.append(merged)
        matched = True
    if not matched:
        next_rows.append(_as_dict(row))
    next_rows.sort(key=lambda item: _as_str(_as_dict(item).get("last_seen_at")), reverse=True)
    return next_rows


def _annotate_discovered_chats(
    discovered_chats: list[dict[str, Any]],
    channels: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_chat_id = {
        _as_str(_as_dict(channel).get("chat_id")): _as_dict(channel)
        for channel in channels
        if _as_str(_as_dict(channel).get("chat_id"))
    }
    annotated: list[dict[str, Any]] = []
    for row in discovered_chats:
        payload = dict(_as_dict(row))
        existing = by_chat_id.get(_as_str(payload.get("chat_id")))
        if existing:
            payload["existing_channel_id"] = _as_str(existing.get("channel_id"))
            payload["existing_channel_display_name"] = _as_str(existing.get("display_name"))
            payload["already_saved"] = True
        else:
            payload["existing_channel_id"] = ""
            payload["existing_channel_display_name"] = ""
            payload["already_saved"] = False
        payload["suggested_channel_id"] = _slugify_channel_id(
            payload.get("username") or payload.get("title") or payload.get("chat_id")
        )
        annotated.append(payload)
    return annotated


def _chat_row_from_telegram_chat(
    chat: dict[str, Any], *, source: str, invite_link: str = ""
) -> dict[str, Any]:
    payload = _as_dict(chat)
    chat_id = _as_str(payload.get("id"))
    username = _as_str(payload.get("username"))
    title = _as_str(
        payload.get("title")
        or payload.get("full_name")
        or payload.get("first_name")
        or username
        or chat_id
    )
    chat_type = _as_str(payload.get("type")) or "unknown"
    return _discovered_chat_row(
        chat_id=chat_id,
        title=title,
        chat_type=chat_type,
        username=username,
        source=source,
        invite_link=invite_link,
        last_seen_at=datetime.now(UTC).isoformat(),
    )


def _normalize_inbound_state(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = _as_dict(payload)
    webhook_status = _webhook_projection(_as_dict(source.get("webhook_status")))
    discovered: list[dict[str, Any]] = []
    for row in _as_list(source.get("discovered_chats")):
        payload_row = _as_dict(row)
        chat_id = _as_str(payload_row.get("chat_id"))
        if not chat_id:
            continue
        discovered = _upsert_discovered_chat(
            discovered,
            _discovered_chat_row(
                chat_id=chat_id,
                title=_as_str(payload_row.get("title")),
                chat_type=_as_str(payload_row.get("type")),
                username=_as_str(payload_row.get("username")),
                source=_as_str(payload_row.get("source")),
                invite_link=_as_str(payload_row.get("invite_link")),
                last_seen_at=_as_str(payload_row.get("last_seen_at")),
            ),
        )
    polling_status = _as_str(source.get("polling_status")).lower() or "idle"
    inbound_mode = _as_str(source.get("inbound_mode")).lower() or "auto"
    if inbound_mode not in {"auto", "webhook", "polling"}:
        inbound_mode = "auto"
    last_update_id = _coerce_int(source.get("last_update_id"))
    return {
        "inbound_mode": inbound_mode,
        "polling_interval_seconds": _normalize_polling_interval(
            source.get("polling_interval_seconds")
        ),
        "webhook_status": webhook_status,
        "polling_status": polling_status,
        "active_transport": _as_str(source.get("active_transport")),
        "last_update_id": last_update_id,
        "last_inbound_sync_at": _as_str(source.get("last_inbound_sync_at")),
        "last_inbound_error": _as_str(source.get("last_inbound_error")),
        "pending_update_count": _coerce_int(source.get("pending_update_count")) or 0,
        "discovered_chats": discovered,
        "pending_hitl_tokens": _as_dict(source.get("pending_hitl_tokens")),
    }


def _build_hitl_job_ref(project_id: str, job_id: str) -> str:
    _ = project_id, job_id
    return secrets.token_urlsafe(16)


def _register_pending_hitl_token(
    bl: Any,
    *,
    project_id: str,
    inbound_state: dict[str, Any],
    job_id: str,
    approval_id: str,
    allowed_actions: list[str] | None = None,
    chat_id: int | str | None = None,
    channel_id: str | None = None,
) -> str:
    next_state = dict(inbound_state)
    token_map = _as_dict(next_state.get("pending_hitl_tokens"))
    normalized_job_id = _as_str(job_id)
    token_map = {
        key: value
        for key, value in token_map.items()
        if _as_str(_as_dict(value).get("job_id")) != normalized_job_id
    }
    token = _build_hitl_job_ref(project_id, job_id)
    for _ in range(3):
        if token not in token_map:
            break
        token = _build_hitl_job_ref(project_id, job_id)
    token_map[token] = {
        "job_id": normalized_job_id,
        "approval_id": _as_str(approval_id),
        "allowed_actions": _normalize_allowed_actions(allowed_actions),
        "project_id": _as_str(project_id),
        "created_at": datetime.now(UTC).isoformat(),
        "chat_id": _as_str(chat_id),
        "channel_id": _as_str(channel_id),
    }
    # Keep the newest 100 refs to stay well-bounded.
    ordered = sorted(
        ((key, _as_dict(value)) for key, value in token_map.items() if _as_str(key)),
        key=lambda item: _as_str(item[1].get("created_at")),
        reverse=True,
    )[:100]
    next_state["pending_hitl_tokens"] = dict(ordered)
    if not _save_telegram_connection_ingress(bl, project_id, next_state):
        return ""
    inbound_state.clear()
    inbound_state.update(next_state)
    return token


def _resolve_pending_hitl_token(
    bl: Any,
    *,
    project_id: str | None,
    job_ref: str,
    inbound_state: dict[str, Any] | None = None,
    chat_id: int | str | None = None,
) -> dict[str, Any]:
    token = _as_str(job_ref)
    if not token:
        return {}
    state = _normalize_inbound_state(inbound_state) if inbound_state is not None else {}
    token_map = _as_dict(state.get("pending_hitl_tokens"))
    resolved = _as_dict(token_map.get(token))
    if resolved:
        return (
            resolved
            if _pending_hitl_token_is_valid(
                resolved,
                project_id=project_id,
                chat_id=chat_id,
            )
            else {}
        )
    normalized_project_id = _coerce_project_id(project_id)
    if not normalized_project_id or bl is None:
        return {}
    connection = _telegram_connection_row_for_project(bl, normalized_project_id)
    loaded_state = _normalize_inbound_state(_as_dict(connection.get("ingress_config")))
    resolved = _as_dict(_as_dict(loaded_state.get("pending_hitl_tokens")).get(token))
    return (
        resolved
        if _pending_hitl_token_is_valid(
            resolved,
            project_id=project_id,
            chat_id=chat_id,
        )
        else {}
    )


def _pending_hitl_token_is_valid(
    token_record: dict[str, Any],
    *,
    project_id: str | None,
    chat_id: int | str | None,
) -> bool:
    if not token_record:
        return False
    if not _as_str(token_record.get("approval_id")):
        return False
    expected_project_id = _as_str(project_id)
    bound_project_id = _as_str(token_record.get("project_id"))
    if expected_project_id and bound_project_id != expected_project_id:
        return False
    expected_chat_id = _as_str(chat_id)
    bound_chat_id = _as_str(token_record.get("chat_id"))
    if expected_chat_id and bound_chat_id and bound_chat_id != expected_chat_id:
        return False
    created_at = _as_str(token_record.get("created_at"))
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return False
    now = datetime.now(UTC)
    return created <= now and now - created <= _HITL_TOKEN_TTL


def _bind_pending_hitl_message_ref(
    bl: Any,
    *,
    project_id: str,
    job_ref: str,
    inbound_state: dict[str, Any],
    chat_id: int | str | None,
    message_id: int | str | None,
) -> dict[str, Any]:
    resolved = _resolve_pending_hitl_token(
        bl,
        project_id=project_id,
        job_ref=job_ref,
        inbound_state=inbound_state,
        chat_id=chat_id,
    )
    normalized_message_id = _as_str(message_id)
    if not resolved or not normalized_message_id:
        return resolved
    next_state = dict(inbound_state)
    token_map = dict(_as_dict(next_state.get("pending_hitl_tokens")))
    token = _as_str(job_ref)
    record = dict(_as_dict(token_map.get(token)))
    if not record:
        return resolved
    record["provider_message_id"] = normalized_message_id
    token_map[token] = record
    next_state["pending_hitl_tokens"] = token_map
    if _save_telegram_connection_ingress(bl, project_id, next_state):
        inbound_state.clear()
        inbound_state.update(next_state)
        return record
    return resolved


def _consume_pending_hitl_token(
    bl: Any,
    *,
    project_id: str,
    job_ref: str,
    inbound_state: dict[str, Any],
    chat_id: int | str | None = None,
) -> dict[str, Any]:
    token = _as_str(job_ref)
    resolved = _resolve_pending_hitl_token(
        bl,
        project_id=project_id,
        job_ref=token,
        inbound_state=inbound_state,
        chat_id=chat_id,
    )
    if not resolved:
        return {}
    consume = getattr(bl, "consume_project_connection_ingress_token", None)
    if callable(consume):
        if not consume(
            project_id=project_id,
            extension_id=PROVIDER_KEY,
            token=token,
        ):
            return {}
        next_state = dict(inbound_state)
        token_map = dict(_as_dict(next_state.get("pending_hitl_tokens")))
        token_map.pop(token, None)
        next_state["pending_hitl_tokens"] = token_map
        inbound_state.clear()
        inbound_state.update(next_state)
        return resolved
    next_state = dict(inbound_state)
    token_map = dict(_as_dict(next_state.get("pending_hitl_tokens")))
    if token not in token_map and bl is not None:
        connection = _telegram_connection_row_for_project(bl, project_id)
        next_state = _normalize_inbound_state(_as_dict(connection.get("ingress_config")))
        token_map = dict(_as_dict(next_state.get("pending_hitl_tokens")))
    if token not in token_map:
        return {}
    token_map.pop(token, None)
    next_state["pending_hitl_tokens"] = token_map
    _save_telegram_connection_ingress(bl, project_id, next_state)
    inbound_state.clear()
    inbound_state.update(next_state)
    return resolved


def _consume_pending_hitl_approval(
    bl: Any,
    *,
    project_id: str,
    approval_id: str,
    job_id: str,
    inbound_state: dict[str, Any],
    chat_id: int | str | None = None,
    channel_id: str | None = None,
) -> dict[str, Any]:
    normalized_approval_id = _as_str(approval_id)
    normalized_job_id = _as_str(job_id)
    normalized_chat_id = _as_str(chat_id)
    normalized_channel_id = _as_str(channel_id)
    matches = []
    for token, raw_record in _as_dict(inbound_state.get("pending_hitl_tokens")).items():
        record = _as_dict(raw_record)
        if _as_str(record.get("approval_id")) != normalized_approval_id:
            continue
        if _as_str(record.get("job_id")) != normalized_job_id:
            continue
        if normalized_chat_id and _as_str(record.get("chat_id")) != normalized_chat_id:
            continue
        if normalized_channel_id and _as_str(record.get("channel_id")) != normalized_channel_id:
            continue
        matches.append((_as_str(token), record))
    if len(matches) != 1:
        return {}
    token, _record = matches[0]
    return _consume_pending_hitl_token(
        bl,
        project_id=project_id,
        job_ref=token,
        inbound_state=inbound_state,
        chat_id=chat_id,
    )


def _callback_registration_for_project(bl: Any, project_id: str) -> dict[str, Any]:
    if bl is None or not hasattr(bl, "list_extension_callback_registrations"):
        return {}
    try:
        rows = bl.list_extension_callback_registrations(
            extension_id=PROVIDER_KEY,
            callback_id="updates",
            project_id=str(project_id),
        )
    except Exception:
        rows = []
    if not rows:
        return {}
    return _as_dict(rows[0])


def _active_transport_for_state(inbound_state: dict[str, Any]) -> str:
    mode = _as_str(inbound_state.get("inbound_mode")).lower() or "auto"
    webhook_status = _as_dict(inbound_state.get("webhook_status"))
    webhook_ok = _coerce_bool(webhook_status.get("url_configured"), False) and _as_str(
        webhook_status.get("status")
    ).lower() in {"healthy", "configured"}
    if mode == "webhook":
        return "webhook" if webhook_ok else "webhook_unavailable"
    if mode == "polling":
        return "polling"
    return "webhook" if webhook_ok else "polling"

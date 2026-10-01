"""Overview readiness, derived from current facts.

Four sections — Bot, Channels, Inbound, Deliveries — each with a readiness word
from a closed vocabulary, a human detail, and (when something is incomplete) one
contextual action pointing at the page and component that fixes it.

`setup_incomplete` means required configuration is missing; `ready` means the
token is valid and an enabled notification channel exists; `needs_attention`
means a configured optional or operational part is degraded; `failed` is
reserved for a confirmed failure. Inbound is optional: `off` never reduces
outbound readiness. A failed delivery marks Deliveries failed and rolls the
summary to `needs_attention` without claiming the bot is unavailable.

Nothing here touches the network; the caller supplies the `getMe` response.
"""

from __future__ import annotations

from typing import Any

READINESS_LABELS: dict[str, str] = {
    "ready": "Ready",
    "setup_incomplete": "Setup incomplete",
    "needs_attention": "Needs attention",
    "failed": "Failed",
    "off": "Off",
}

# The platform health panel understands healthy / warning / failed.
_PLATFORM_STATUS: dict[str, str] = {
    "ready": "healthy",
    "off": "healthy",
    "setup_incomplete": "warning",
    "needs_attention": "warning",
    "failed": "failed",
}

MESSAGES: dict[str, str] = {
    "bot_token_required": "Save a bot token on the Setup page before verifying.",
    "bot_token_rejected": (
        "Telegram rejected this bot token. Copy a current token from @BotFather and try again."
    ),
    "telegram_unreachable": (
        "The token was saved, but Telegram could not be reached for verification. Try Verify again."
    ),
    "channel_required": "Choose an enabled Telegram channel before sending a test notification.",
    "storage_unavailable": "Telegram settings could not be saved. No partial setup was applied.",
}

_INBOUND_OPTIONAL = "needed only for replies and approve/reject/abort decisions"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def classify_bot_api_failure(response: dict[str, Any] | None) -> str:
    """`unauthorized` when Telegram rejected the token, otherwise `unreachable`."""
    payload = _as_dict(response)
    error = _text(payload.get("error") or payload.get("description")).lower()
    if payload.get("error_code") == 401 or "401" in error or "unauthorized" in error:
        return "unauthorized"
    return "unreachable"


def _check(
    name: str,
    readiness: str,
    detail: str,
    *,
    action: tuple[str, str, str] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": name,
        "status": _PLATFORM_STATUS[readiness],
        "readiness": readiness,
        "label": READINESS_LABELS[readiness],
        "detail": detail,
    }
    if action is not None:
        label, page_id, component_id = action
        row["action"] = {"label": label, "page_id": page_id, "component_id": component_id}
    return row


def _bot_check(
    *, token_present: bool, identity_response: dict[str, Any] | None
) -> tuple[dict[str, Any], str]:
    token_card = ("setup", "secrets_form")
    if not token_present:
        return (
            _check(
                "Bot",
                "setup_incomplete",
                "Setup incomplete: no bot token saved.",
                action=("Configure bot", *token_card),
            ),
            "",
        )
    response = _as_dict(identity_response)
    if _truthy(response.get("ok")):
        me = _as_dict(response.get("result"))
        username = _text(me.get("username"))
        detail = f"Connected as @{username}" if username else f"Connected (bot id {me.get('id')})"
        return _check("Bot", "ready", detail), username
    if classify_bot_api_failure(response) == "unauthorized":
        return (
            _check(
                "Bot",
                "failed",
                MESSAGES["bot_token_rejected"],
                action=("Replace token", *token_card),
            ),
            "",
        )
    return (
        _check(
            "Bot",
            "needs_attention",
            MESSAGES["telegram_unreachable"],
            action=("Verify token", *token_card),
        ),
        "",
    )


def _channels_check(channels: list[dict[str, Any]]) -> tuple[dict[str, Any], int, int]:
    def valid_enabled_channel(value: Any) -> bool:
        row = _as_dict(value)
        capabilities = _as_dict(row.get("capabilities"))
        return bool(
            _text(row.get("channel_id"))
            and _text(row.get("display_name"))
            and _text(row.get("chat_id"))
            and row.get("enabled") is True
            and isinstance(capabilities.get("notify"), bool)
            and isinstance(capabilities.get("human_decision"), bool)
        )

    enabled = [row for row in channels if valid_enabled_channel(row)]
    notify = sum(
        1 for row in enabled if _truthy(_as_dict(_as_dict(row).get("capabilities")).get("notify"))
    )
    decisions = sum(
        1
        for row in enabled
        if _truthy(_as_dict(_as_dict(row).get("capabilities")).get("human_decision"))
    )
    if not notify:
        return (
            _check(
                "Channels",
                "setup_incomplete",
                "No enabled notification channel.",
                action=("Add channel", "chats_channels", "channels_form"),
            ),
            notify,
            decisions,
        )
    return (
        _check("Channels", "ready", f"Enabled: {notify} notification, {decisions} human-decision"),
        notify,
        decisions,
    )


def _inbound_check(inbound_state: dict[str, Any]) -> tuple[dict[str, Any], str]:
    state = _as_dict(inbound_state)
    mode = _text(state.get("inbound_mode")).lower() or "auto"
    webhook = _as_dict(state.get("webhook_status"))
    webhook_state = _text(webhook.get("status")).lower()
    webhook_ok = _truthy(webhook.get("url_configured")) and webhook_state in {
        "healthy",
        "configured",
    }
    webhook_failed = webhook_state == "failed"
    polling_state = _text(state.get("polling_status")).lower()
    interval = state.get("polling_interval_seconds")
    configure = ("Configure inbound", "inbound", "inbound_mode_form")

    if webhook_failed and mode in {"auto", "webhook"}:
        reason = _text(webhook.get("last_error_message")) or "see Inbound status"
        return _check(
            "Inbound", "needs_attention", f"Webhook failing: {reason}", action=configure
        ), "Webhook"
    if webhook_ok and mode in {"auto", "webhook"}:
        return _check("Inbound", "ready", "Webhook active"), "Webhook"
    if polling_state == "failed" and mode in {"auto", "polling"}:
        reason = _text(state.get("last_inbound_error")) or "see Inbound status"
        return _check(
            "Inbound", "needs_attention", f"Polling failing: {reason}", action=configure
        ), "Polling"
    if mode == "polling" or (mode == "auto" and polling_state == "healthy"):
        every = f" every {interval} s" if interval else ""
        return _check("Inbound", "ready", f"Polling{every}"), "Polling"
    return (
        _check("Inbound", "off", f"Off (optional; {_INBOUND_OPTIONAL})", action=configure),
        "Off",
    )


def _deliveries_check(deliveries: list[dict[str, Any]]) -> tuple[dict[str, Any], int, int]:
    rows = [_as_dict(row) for row in deliveries if isinstance(row, dict)]
    rows.sort(key=lambda row: _text(row.get("created_at") or row.get("delivered_at")), reverse=True)
    recent = rows[:20]
    delivered = sum(1 for row in recent if _text(row.get("status")).lower() == "delivered")
    failed = sum(1 for row in recent if _text(row.get("status")).lower() == "failed")
    if not recent:
        return _check("Deliveries", "ready", "No deliveries yet."), 0, 0
    last = recent[0]
    counts = f"{delivered} delivered, {failed} failed recently."
    if _text(last.get("status")).lower() == "failed":
        reason = _text(last.get("error_text")) or "no error text recorded"
        return (
            _check(
                "Deliveries",
                "failed",
                f"{counts} Last delivery failed: {reason}",
                action=("Review deliveries", "deliveries", "delivery_grid"),
            ),
            delivered,
            failed,
        )
    when = _text(last.get("created_at") or last.get("delivered_at"))
    suffix = f" at {when}" if when else ""
    return (
        _check(
            "Deliveries",
            "ready",
            f"{counts} Last: {_text(last.get('status')) or 'unknown'}{suffix}",
        ),
        delivered,
        failed,
    )


def derive_readiness(
    *,
    connection: dict[str, Any] | None,
    token_present: bool,
    identity_response: dict[str, Any] | None,
    channels: list[dict[str, Any]],
    inbound_state: dict[str, Any] | None,
    deliveries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Checks and summary for the Overview readiness panel. Never includes a token."""
    bot, username = _bot_check(token_present=token_present, identity_response=identity_response)
    channels_row, notify_count, decision_count = _channels_check(list(channels or []))
    inbound, inbound_label = _inbound_check(_as_dict(inbound_state))
    deliveries_row, delivered, failed = _deliveries_check(list(deliveries or []))
    checks = [bot, channels_row, inbound, deliveries_row]

    if bot["readiness"] == "failed":
        readiness = "failed"
    elif "setup_incomplete" in {bot["readiness"], channels_row["readiness"]}:
        readiness = "setup_incomplete"
    elif any(row["readiness"] in {"needs_attention", "failed"} for row in checks):
        readiness = "needs_attention"
    else:
        readiness = "ready"

    return {
        "checks": checks,
        "summary": {
            "provider": "telegram",
            "readiness": readiness,
            "status": READINESS_LABELS[readiness],
            "connection_id": _text(_as_dict(connection).get("connection_id")),
            "bot_username": username,
            "enabled_notification_channels": notify_count,
            "enabled_human_decision_channels": decision_count,
            "inbound": inbound_label,
            "deliveries": {"delivered": delivered, "failed": failed},
        },
    }


__all__ = ["MESSAGES", "READINESS_LABELS", "classify_bot_api_failure", "derive_readiness"]

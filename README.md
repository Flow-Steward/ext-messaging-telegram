# Telegram Reference Extension

`extensions/flowsteward_telegram` is the canonical Flow Steward reference for notification/chat providers that need:

- project-scoped provider defaults
- project-scoped secrets
- multiple named channels
- outbound notifications
- inbound human-in-the-loop callbacks

## Bundle layout

- `extension.yaml`: top-level bundle manifest and callback wiring
- `main.py`: subprocess runtime for queries, actions, outbound delivery, and inbound event normalization
- `health.py`: lightweight health probe
- `schemas/`
  - `config.schema.yaml`: provider defaults only
  - `secrets.schema.yaml`: secret values only
  - `subscriptions.schema.yaml`: project notification route shape
- `ui/`
  - `pages/`: `overview` (readiness, read-only), `setup` (quick start, bot token, message defaults, guide), `chats_channels` (discovery and named channels), `inbound` (optional mode, webhook, status), `deliveries` (summary and history)
  - `components/`: declarative forms and callback setup
  - `queries/queries.yaml`: typed query contract
  - `actions/actions.yaml`: typed action contract
- `tests/`: bundle-local reference tests

## Configuration model

Telegram uses three separate project-scoped data layers.

Provider defaults:

- `disable_web_page_preview`
- `message_prefix`

Secrets:

- `bot_token`

Channels:

- `channels[]`

Each channel has:

- `channel_id`
- `display_name`
- `chat_id`
- `enabled`
- `capabilities.notify`
- `capabilities.human_decision`

`channel_id` is the stable authoring key. Notification routes and workflow reactions target `channel_id`, not raw `chat_id`.

## Outbound delivery flow

1. Flow Steward runtime resolves the notification provider extension.
2. A project notification route or workflow reaction passes `route_config.channel_id`.
3. `main.py` resolves the named channel from `channels[]`.
4. The runtime validates:
   - channel exists
   - channel is enabled
   - channel has the required capability
5. Telegram `sendMessage` is called with the fixed safe HTML rendering mode, provider
   defaults, and an optional message prefix.
6. The real Telegram `message_id` is returned and persisted as `provider_message_id`.

Important behavior:

- no primary singleton `chat_id` UX
- no silent reroute to an unrelated default chat

## Send Message workflow step

`send_message` is the one Telegram operation a workflow can use as a step
(Action Step → Telegram Notifications → Send Message). It posts the text it is
given, not a rendered platform event, so a Template step in front of it composes
any message from workflow data.

- The step is bound to a Telegram connection and sends with its bot token.
- `channel_id` names one of the connection's channels (Chats & Channels page).
  It may be left empty when exactly one channel is enabled for notifications;
  otherwise the step fails before sending and lists the channel IDs.
- `format`: `text` sends the message as written (escaped for Telegram);
  `html` reads Telegram's HTML subset (b, i, u, s, a, code, pre, blockquote).
  If Telegram rejects the markup, the message is sent again without it.
- The connection's message prefix and link-preview setting apply;
  `disable_web_page_preview` overrides the latter per step.
- More than 4096 characters is refused (`message_too_long`) rather than cut.
- Test Run never sends (`external_effect_status: suppressed`).

## Inbound HITL flow

Telegram callbacks and replies normalize into canonical Flow Steward events.

Approve / reject / abort flow:

1. User taps inline button in Telegram
2. Callback payload carries compact project/job context
3. Extension emits `notification.interaction_received`
4. Reject keeps the two-step flow:
   - user presses Reject
   - bot asks for a reason
   - next reply emits completion event
5. Free-form reject comments emit `external.comment.received`
6. When the workflow's rework budget is exhausted, the gate exposes Abort instead of Reject.

The callback payload format is compact enough to support UUID project ids while staying inside Telegram limits.

## Copying this pattern

Use this bundle as the baseline for Slack, Teams, WhatsApp-like, or other chat providers when you need:

- declarative extension UI only
- no provider-specific frontend code in core
- project-scoped channels instead of singleton destinations
- separate defaults vs secrets vs channels
- explicit provider/channel targeting from notifications and workflows

Recommended adaptation checklist:

1. Keep the same three-layer storage model: defaults, secrets, channels.
2. Preserve named `channel_id` targeting as the contract between authoring UI and runtime delivery.
3. Keep bundle UI declarative in YAML.
4. Expose `list_channels` plus channel mutation actions in the action/query manifests.
5. Make delivery validate channel capability instead of silently falling back.

# Telegram Reference Extension

`extensions/flowsteward_telegram` is the canonical Flow Steward reference for notification/chat providers that need:

- project-scoped provider defaults
- project-scoped secrets
- multiple named channels
- outbound notifications
- inbound human-in-the-loop callbacks
- workflow steps (messages, files, edit, delete, pin) and a "New Telegram message" workflow trigger

## Requirements

This release (1.3.0) speaks the host contract `extension_host_v2` with its final
execution scope (`group_id`, `for_each_id`, `step_id`, `step_execution_id`)
and is built on `flowsteward-extension-sdk` 0.4.0. Every request (query,
action, notification delivery, inbound callback) with another contract, a
missing scope key, both a Group and a For each as its owner, or a step without
its execution is refused (`invalid_host_envelope`) before anything is read,
sent or saved. Install it on a Flow Steward Core that speaks this contract; an
older Core does not run it.

The "New Telegram message" trigger starts a workflow only on a Core that runs
provider-event triggers (it starts workflows from the events an extension's
`ingress_event` action returns, matching them to an enabled provider trigger).
On an older Core the trigger can be declared but never fires; everything else
works as before.

TODO(release): state the minimum Core version for the trigger. The manifest
field for it is `runtime.compatibility.platform_min`, but that field is on the
host-contract scale (`extension_host_v2`, currently 1.3.0), not the Core
application version, and no host-contract version marks "provider-event
triggers" yet. This bundle therefore declares no `platform_min`; set it (or name
the Core version in the release notes) once the Core that ships the trigger
dispatch is versioned.

## Bundle layout

- `extension.yaml`: top-level bundle manifest and callback wiring
- `main.py`: subprocess runtime for queries, actions, outbound delivery, and inbound event normalization
- `main_send.py`: the Send Message step, plus the reply / silent / keyboard options the message steps share
- `main_steps.py`: the other workflow steps (Send File, Edit, Delete, Pin, Unpin, Get Telegram File)
- `main_transport_steps.py`: Bot API calls behind those steps (multipart upload, error mapping, download)
- `health.py`: lightweight health probe
- `icon.png`: the extension logo (Logo: icon.png, the Flow Steward wave mark; PNG, 128x128)
- `schemas/`
  - `config.schema.yaml`: provider defaults only
  - `secrets.schema.yaml`: secret values only
  - `subscriptions.schema.yaml`: project notification route shape
- `contracts/`: Contract v2 connection type, workflow operations, step forms and artifact policies
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
- `capabilities.workflow_trigger` (default `false`; switches on the "New Telegram message" workflow trigger for this chat)

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
   The message names the workflow and job, then a `Group:` line only for a step
   or event in a Group, a `For each:` line only for one in a For each, and a
   `Step:` line only for a step (the Core labels, or the ids without them).
   There is no phase.
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
- Optional `reply_to_message_id` (send as a reply), `disable_notification`
  (silent) and `buttons`: an inline keyboard as a list of rows, each row a list
  of `{text, url}` or `{text, callback_data}`. `callback_data` is at most 64
  bytes and may not start with `tgx:` (reserved for approval buttons). A
  malformed keyboard fails with `keyboard_invalid` before anything is sent.
  Without these inputs the step sends exactly what it always sent.
- Test Run never sends (`external_effect_status: suppressed`).

Button limits: a button press with `callback_data` reaches the bot, and this
release answers it with "Unsupported action." in Telegram (only approval buttons
are handled). Use `url` buttons until a button-click trigger exists.

## More workflow steps

Every message step is bound to a Telegram connection, addresses a chat with the
same `channel_id` mechanism as Send Message (the channel must be enabled and have
`capabilities.notify`), reports `provider_message_id` and `channel_id`, declares
`idempotency: required` and `test_mode_behavior: suppress`, and is never run by a
Test Run. A step acting on an existing message also takes its `message_id`.

| Step | What it does | Notes |
| --- | --- | --- |
| `send_file` | Sends an artifact as `photo`, `document`, `video`, `audio`, `voice` or `animation` (multipart upload) | Optional caption (at most 1024 characters), `format`, `file_name`, reply and silent. A photo above 10 MB or any other file above 50 MB is refused (`file_too_large`), never cut |
| `edit_message` | Replaces the text of a message the bot sent (1-4096 characters), optional `format` and `buttons` | The connection's message prefix applies; markup Telegram rejects is sent again as plain text |
| `delete_message` | Deletes a message | Telegram deletes only messages younger than 48 hours; an older one fails with `telegram_delete_not_allowed` |
| `pin_message` | Pins a message (`disable_notification` pins silently) | The bot needs the right to pin in groups and channels |
| `unpin_message` | Unpins a message; without `message_id` Telegram unpins the most recent pin | |
| `get_file` | Downloads a file by `file_id` into an artifact (`artifact_handle`, `artifact_filename`, `size_bytes`, `sha256`) | Up to 20 MB (`file_too_large` above, never cut). Telegram keeps the link valid for about an hour; the step downloads at once. Needs no channel |

Telegram errors map to stable codes, for example `telegram_delete_not_allowed`,
`telegram_message_not_found`, `telegram_message_not_modified`,
`telegram_edit_not_allowed`, `telegram_not_enough_rights`,
`telegram_chat_not_found`, `telegram_bot_not_in_chat`, `telegram_rate_limited`
(with `retry_after_seconds`) and `telegram_request_rejected`. A timeout or a 5xx
answer is `telegram_send_ambiguous` (outcome unknown). Telegram allows about one
message per second to one chat.

Not covered on purpose: stickers, locations, polls, media groups and chat
administration.

## New Telegram message trigger

Declared in `extension.yaml` as `workflow_triggers[message_received]`
(provider `flowsteward.telegram`, event `telegram.message.received`). A workflow
author binds it under Triggers; optional `chat_id` and `user_id` narrow it
(empty accepts every allowed chat and sender).

It is closed by default. A plain message produces the event only when its chat is
a saved channel that is enabled **and** has `capabilities.workflow_trigger: true`
(Chats & Channels page); any other chat is ignored silently and still answered
successfully, so Telegram does not retry. Switch incoming messages on at the
Inbound page (webhook or polling); polling returns the same events.

Payload (these become the workflow inputs): `chat_id`, `user_id`, `message_id`,
`text`, `username`, `chat_type`, `date`, `has_file`, `file_id`, `file_kind`
(`photo`, `document`, `video`, `audio`, `voice`, `animation`, `sticker` or empty),
`file_name`, `caption`, `reply_to_message_id` (0 when not a reply), `update_id`
and, when known, `connection_id`. Only metadata and the `file_id` arrive; the
file is never downloaded by the trigger. Feed `file_id` to Get Telegram File to
fetch it. The replay key is the Telegram `update_id` (`correlation_id`), so a
Telegram retry does not start a second run.

Approval buttons, `/approve`, `/reject` and the reject-reason reply keep their
own flow and never produce this event. A button press that is not an approval
produces no trigger event (a separate button-click event would be needed).

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
   The buttons and commands are exactly the approval's `allowed_actions`
   (`approve`/`reject`, or `approve`/`abort`); anything else is refused.
7. A child workflow's approval delivered to its waiting parent
   (`propagated: true`) is decided on `approval_job_id` + `approval_id`; the job
   link and the decision address the child's job, never the parent's `job_id`.

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

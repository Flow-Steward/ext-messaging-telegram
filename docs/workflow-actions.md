# Workflow actions

Create a Telegram bot project connection with its bot token and add an enabled chat on Chats & Channels; select that connection and channel for messaging steps.

Select the connection in the workflow step. Map returned message IDs, file handles or dataset handles into later steps rather than pasting credentials or local paths.

## Send Telegram message (`send_message`)

Post a message from the workflow to one of this bot's Telegram channels. Create a Telegram bot project connection with its bot token and add an enabled chat on Chats & Channels; select that connection and channel for messaging steps. Example: Post a workflow completion summary to the ops-alerts project channel.

Inputs:

- `channel_id`: The ID of a channel on the Chats & Channels page. Leave empty when only one channel is enabled for notifications.
- `text`: At most 4096 characters. Map a Template step's result to send composed text.
- `format`: Choose text for plain text or html for supported Telegram HTML tags (b, i, u, s, a, code, pre, blockquote).
- `disable_web_page_preview`: Disable the preview for the first link in the message. Leave empty to use the connection setting.
- `reply_to_message_id`: Send as a reply to this Telegram message ID. Leave empty for a normal message.
- `disable_notification`: Deliver without a notification sound.
- `buttons`: Inline keyboard: a list of rows, each a list of buttons {"text": "Open", "url": "https://..."} or {"text": "Go", "callback_data": "my_data"}. callback_data is at most 64 bytes and may not start with "tgx:".

Result fields: `delivered`, `provider_message_id`, `channel_id`, `external_effect_status`.

## Send Telegram file (`send_file`)

Send a file from the workflow to one of this bot's Telegram channels as a photo, document, video, audio, voice message or animation. Photos up to 10 MB, other kinds up to 50 MB; a larger file is refused, never cut. Create a Telegram bot project connection with its bot token and add an enabled chat on Chats & Channels; select that connection and channel for messaging steps. Example: Send a generated PDF report artifact as kind=document to the configured team channel.

Inputs:

- `channel_id`: The ID of a channel on the Chats & Channels page. May be left empty when only one channel is enabled for notifications.
- `kind`: How Telegram shows the file: photo, document, video, audio, voice or animation. A photo is limited to 10 MB, every other kind to 50 MB.
- `artifact_handle`: The file (an artifact) to send, for example one a Download remote file or Download Telegram file step produced.
- `caption`: Optional text under the file, at most 1024 characters.
- `format`: text sends the message as written; html reads Telegram's HTML tags (b, i, u, s, a, code, pre, blockquote).
- `file_name`: The name Telegram shows for the file. Defaults to the artifact's name.
- `reply_to_message_id`: Send as a reply to this Telegram message ID. Leave empty for a normal message.
- `disable_notification`: Deliver without a notification sound.

Result fields: `delivered`, `provider_message_id`, `channel_id`, `external_effect_status`.

## Edit Telegram message (`edit_message`)

Replace the text of a message the bot sent to one of its Telegram channels. Create a Telegram bot project connection with its bot token and add an enabled chat on Chats & Channels; select that connection and channel for messaging steps. Example: Replace an earlier bot status message with the final result using its returned provider_message_id.

Inputs:

- `channel_id`: The ID of a channel on the Chats & Channels page. May be left empty when only one channel is enabled for notifications.
- `message_id`: The Telegram message ID, for example the provider_message_id a Send step returned.
- `text`: The text, at most 4096 characters.
- `format`: text sends the message as written; html reads Telegram's HTML tags (b, i, u, s, a, code, pre, blockquote).
- `buttons`: Inline keyboard: a list of rows, each a list of buttons {"text": "Open", "url": "https://..."} or {"text": "Go", "callback_data": "my_data"}. callback_data is at most 64 bytes and may not start with "tgx:".
- `disable_web_page_preview`: Leave out the preview Telegram shows for the first link. Defaults to the connection setting.

Result fields: `delivered`, `provider_message_id`, `channel_id`, `external_effect_status`.

## Delete Telegram message (`delete_message`)

Delete one message in a Telegram channel. Telegram deletes only messages younger than 48 hours; for an older message the step fails with telegram_delete_not_allowed. Create a Telegram bot project connection with its bot token and add an enabled chat on Chats & Channels; select that connection and channel for messaging steps. Example: Remove a temporary bot status message after its workflow finishes.

Inputs:

- `channel_id`: The ID of a channel on the Chats & Channels page. May be left empty when only one channel is enabled for notifications.
- `message_id`: The Telegram message ID, for example the provider_message_id a Send step returned.

Result fields: `delivered`, `provider_message_id`, `channel_id`, `external_effect_status`.

## Pin Telegram message (`pin_message`)

Pin a message in a Telegram channel. In groups and channels the bot needs the right to pin messages. Create a Telegram bot project connection with its bot token and add an enabled chat on Chats & Channels; select that connection and channel for messaging steps. Example: Pin the incident summary message in the configured team chat.

Inputs:

- `channel_id`: The ID of a channel on the Chats & Channels page. May be left empty when only one channel is enabled for notifications.
- `message_id`: The Telegram message ID, for example the provider_message_id a Send step returned.
- `disable_notification`: Deliver without a notification sound.

Result fields: `delivered`, `provider_message_id`, `channel_id`, `external_effect_status`.

## Unpin Telegram message (`unpin_message`)

Unpin a message in a Telegram channel. Without a message ID Telegram unpins the most recently pinned message. Create a Telegram bot project connection with its bot token and add an enabled chat on Chats & Channels; select that connection and channel for messaging steps. Example: Unpin a resolved incident message by its Telegram message ID.

Inputs:

- `channel_id`: The ID of a channel on the Chats & Channels page. May be left empty when only one channel is enabled for notifications.
- `message_id`: The Telegram message ID, for example the provider_message_id a Send step returned.

Result fields: `delivered`, `provider_message_id`, `channel_id`, `external_effect_status`.

## Download Telegram file (`get_file`)

Download a file the bot can see, by its Telegram file_id, into a workflow artifact. Telegram serves files up to 20 MB and keeps the download link valid for about an hour; the step downloads at once. A larger file is refused, never cut. Select a Telegram bot project connection whose bot can access the incoming file. Example: Download the file_id from a New Telegram message trigger and pass the artifact to document processing.

Inputs:

- `file_id`: The file_id of a file the bot can see, for example the file_id of a New Telegram message trigger. Telegram serves files up to 20 MB.

Result fields: `artifact_handle`, `artifact_filename`, `size_bytes`, `sha256`.

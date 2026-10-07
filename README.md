# Discord message relay

This is a configurable personal-account relay for new messages, including text,
uploaded images/files, and ordinary rich embeds. It uses discord.py-self 2.1.0.
Discord prohibits automated user accounts: this can lead to restrictions or
account termination. It is not a ban-safe or Discord-supported service.
Source: https://support.discord.com/hc/en-us/articles/115002192352

The default command now runs OFFLINE: it checks configuration without importing
Discord libraries, reading credentials, opening the queue, logging in, or sending
messages. These safeguards reduce accidental forwarding and credential exposure;
they do not reduce Discord's enforcement risk once personal-account automation runs.

## Windows setup

Install Python 3.11 or newer. Open PowerShell in this folder:

```powershell
py -m venv .venv
Copy-Item config.example.json config.json
notepad config.json
.\.venv\Scripts\python.exe relay.py
```

This performs the offline configuration check only. Network dependencies are not
needed for that check. It cannot validate server access or prove a channel exists.
If you intentionally choose to enable live account automation despite the ban
risk, install requirements.txt into the virtual environment and launch with
`python relay.py --live` using that environment's Python.

In live mode, enter your token and destination webhook URL only into hidden local prompts.
Paste the bare token: no quotes and no `Bot ` prefix. discord.py-self authenticates a
personal account and cannot log in a bot application; a bot token needs regular
discord.py and the bot must be invited to every source server.
Do not send them in chat, screenshots, or support requests. The script does not
save these credentials. Do not name the script `discord.py`, or install regular
discord.py into the same virtual environment.
If hidden input is unavailable, credential entry fails rather than echoing it.
Known credentials are redacted from the application's configured console logs.
This does not protect against a compromised host, dependency, or process inspection.

For IDs, enable Discord Settings > Advanced > Developer Mode, then use Copy
Server ID / Copy Channel ID / Copy User ID. Create an incoming webhook in your
destination channel's Integrations settings; it requires Manage Webhooks there.
The destination server must not be a configured source.

Configuration is a list of `routes`. Each route maps a set of source channels to
one destination webhook, so you can run as many source/destination pairs as you
need in one process. A message matching several routes is delivered to each.

```json
{
  "routes": [
    {"name": "signals", "source_server_ids": ["111"], "source_channel_ids": ["222", "333"],
     "destination_server_id": "999", "webhook_env": "RELAY_WEBHOOK_SIGNALS"},
    {"name": "news", "source_server_ids": ["111", "444"], "source_channel_ids": ["555", "666"],
     "destination_server_id": "999", "webhook_env": "RELAY_WEBHOOK_NEWS"}
  ],
  "max_total_attachment_mb": 25,
  "max_pending_messages": 5000
}
```

Route fields:

- `name`: unique label (letters, digits, `_`, `-`) used in logs and the queue.
  Do not rename a route while it has pending deliveries.
- `webhook_env`: name of the environment variable holding this route's destination
  webhook URL (default `DISCORD_DEST_WEBHOOK`). The URL itself never goes in the
  config. If the variable is unset you are prompted for it with hidden input.
  Routes sharing a `webhook_env` share that destination. Each distinct webhook
  gets its own delivery worker and pacing, so destinations do not slow each other.
- `webhook_username`: optional display name for relayed posts (default `Signal Relay`).
- `source_server_ids`: required list of source server IDs, as quoted strings.
- `source_channel_ids`: a list of selected channel IDs for this route.
- `all_source_channels`: defaults to false. Set true to select all accessible
  channels in the route's source servers, including newly created channels
  whose events the account receives. This overrides the channel list; leave that
  list empty. Server and author filters still apply. Empty lists without this
  explicit setting do NOT select the whole server.
- `include_threads`: defaults to false. To select threads, list their IDs directly,
  or explicitly set this to true to include children of selected parent channels.
  In all-channel mode, true also selects accessible thread/forum-post events;
  false excludes them. It does not join private threads or grant hidden access.
- `destination_server_id`: required. No route's destination may be any route's
  source server (this prevents relay loops). In live mode each webhook must belong
  to its route's destination server before any forwarding starts.
- `source_author_ids`: empty means every author except your own account;
  otherwise list the signal publishers' user/bot IDs.

Global fields:

- `max_total_attachment_mb`: total attachment size allowed for a message.
  The destination's actual upload limit still applies to each attachment.
- `max_pending_messages`: queue capacity across all routes; exceeding it stops
  the relay visibly.

The older single-destination layout (route fields at the top level, no `routes`
list) is still accepted and treated as one route named `default`. On first live
start, an existing queue's history is assigned to the first route so already
delivered messages are not resent.

Start with one channel and a private destination for testing. With no author
filter, ALL new text/image messages in selected channels are copied, including
ordinary chat. There is no AI signal detector. Mentions are copied without
pinging users or roles. Messages are clearly labeled as relayed and include the
source URL and original timestamp. A source URL still requires source access.

For whole-server selection, use `config.all-channels.example.json` as the template
and replace the two server placeholders. This selects ordinary chat as well as
signals. Only new events received while connected are copied; it is not a history
export and does not guarantee full coverage of every channel/thread. Messages
authored by the account running the script remain excluded.

## Continuous operation

The process must remain running on an always-on computer or server. Closing its
terminal, suspending the computer, or losing connectivity interrupts delivery.
For unattended operation, set DISCORD_USER_TOKEN and every route's `webhook_env` variable in
your hosting service's private environment/secret settings; do not put secret
values into command arguments, source code, or shell history. Use a process
supervisor and persistent storage for `data/`. Only run ONE instance per queue.

For Linux hosting, install these files and dependencies in a virtual environment.
The included `relay.service.example` is a systemd template. Replace the paths,
create a dedicated unprivileged service account, and store credentials in a
root-owned environment file readable only by that account/root. The service
restarts unexpected failures but stops on the script's explicit configuration/
authentication failure exit code 2. Nothing has been deployed or scheduled.
The template deliberately runs only the offline check and then exits. Continuous
forwarding requires explicitly adding `--live` to ExecStart; it is not enabled by
default. Existing unattended installations must remove any previous automatic
launcher if they want to remain offline.

The queue records message IDs, delivery states, and destination message IDs in
`data/deliveries.sqlite3`; protect this directory. It does not store message bodies
or permanent copies of downloaded files. Review logs and queue counts regularly.
Stop with Ctrl+C. Stop all instances before backing up or manipulating the DB.

## Limits and recovery

- Only new message events received while connected are queued. No initial history
  import or automatic history backfill is implemented. Library reconnection may
  recover a resumable session, but downtime can cause missed messages.
- The user API is unofficial and may change. Complete event coverage across large
  servers/private threads is not guaranteed; test each selected source channel.
- Queued messages are fetched at delivery time. A source deleted before delivery
  cannot be copied; its queue entry is marked failed. The latest version at fetch
  time is used. Later edits/deletions are NOT synchronized by this version.
- Long text is split into 1,900-character chunks. Markdown/code blocks can span
  chunks. Files and rich embeds accompany the first part. Image-only attachments
  are supported. Stickers, interactive buttons, forwarded snapshots, and exact
  rendering of special/link-preview embeds are not implemented.
- Messages exceeding attachment limits are marked failed, not silently sent with
  missing files. Other unsupported payloads may also fail; check the health counts.
- Failed and uncertain deliveries are retained for manual review, not retried
  indefinitely. Uncertain means the server might have accepted a send before a
  timeout/crash. Compare destination content with the source message ID before
  deciding whether a manual resend is needed. Exactly-once delivery is not claimed.
- Temporary read/download failures retry after 15 seconds. Every Discord call
  honours HTTP 429 Retry-After (plus a small buffer, or 2 seconds when the header
  is missing) before retrying; after 8 consecutive 429s the entry stays pending and
  deliveries pause. Each accepted send is followed by a random 1.5-3 second pause.
  This is ordinary API-limit courtesy, not protection against self-bot enforcement.
  Access failures stop or mark a delivery failed. No CAPTCHA
  solver, account rotation, proxy rotation, or permission bypass is included.
- Completed queue entries remain to prevent replay duplicates; plan storage
  maintenance for long-running use. There are console health logs, but no external
  outage notification service in this version.

## Validation

Run `python -m unittest discover -s tests -v` for offline queue/filter tests.
Tests also exercise offline startup with networking and secret prompts blocked,
explicit channel selection, configuration validation, and log redaction.
Those tests do not authenticate to Discord or send any messages. Before relying
on delivery, test text, image-only, text-plus-image, bursts, process restart,
connection loss, oversized attachments, and revoked destination permissions.
No live account delivery or 24/7 uptime has been verified for this package.

Library documentation: https://discordpy-self.readthedocs.io/en/latest/

# line-personal-mcp

An unofficial [MCP](https://modelcontextprotocol.io/) server for your **personal LINE account**: list chats, read messages (E2EE-decrypted), and send messages — from Claude Desktop, Muse, or any MCP client.

> ⚠️ **At your own risk.** This uses [okline](https://github.com/nicedayzc/okline), an unofficial SDK that reproduces the LINE Chrome-extension protocol. It is not affiliated with or endorsed by LY Corporation. Unofficial clients can get your account restricted or banned. Login happens as a **companion device**, so your phone stays logged in — but keep volumes human-like and don't spam.

## How it works

- Login reproduces the LINE Chrome extension (CHROMEOS) QR flow: scan the QR with LINE on your phone, confirm the PIN, done. Your phone remains the main device.
- Messages are decrypted locally with Letter Sealing (E2EE) keys from the login session.

## Install

```bash
pip install line-personal-mcp
# or from source:
git clone <this-repo> && cd line-personal-mcp && pip install .
```

Requires **Python 3.10+** and **Node.js 18+** (okline signs every request with LINE's own WASM module, run through a small Node bridge).

MCP clients such as Claude Desktop start servers with a minimal `PATH`. The server therefore also looks for `node` in the usual places (Homebrew, `/usr/local/bin`, `~/.local/bin`, volta, nvm, fnm). If yours lives elsewhere, set `LINE_NODE` to its absolute path in the server's `env` (see below).

## Login

```bash
line-mcp login
```

A QR code is drawn in the terminal (and saved to `~/.line-mcp/qr.png`). Scan it with LINE on your phone (Home → QR scanner), approve, then type the PIN printed in the terminal into your phone. LINE QR codes expire after a few minutes; `login` keeps generating fresh ones for 10 minutes (`--timeout`). The session is saved to `~/.line-mcp/session.json` (mode 600, directory 700 — tokens and E2EE keys are secrets, never commit this file). The QR image is deleted after login.

Logging in again later reuses the saved device certificate, so LINE usually skips the PIN step.

Set `LINE_SESSION_PATH` to override the session location.

**From inside Claude:** if the session expires, ask Claude to log you in. `line_login_start` returns the QR image in the chat, and `line_login_status` gives the PIN to type on your phone. No terminal needed.

Check your setup any time:

```bash
line-mcp doctor   # Node.js, session file + permissions, login, E2EE keys, local store
```

## Use with an MCP client

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "line-personal": {
      "command": "line-mcp",
      "args": ["serve"]
    }
  }
}
```

Muse (`~/.claude.json` or project `.mcp.json`):

```json
{
  "mcpServers": {
    "line-personal": {
      "command": "line-mcp",
      "args": ["serve"],
      "env": {
        "LINE_SESSION_PATH": "/home/you/.line-mcp/session.json",
        "LINE_NODE": "/opt/homebrew/bin/node"
      }
    }
  }
}
```

## Tools

Read tools are annotated `readOnlyHint`; tools that act as you are annotated as writes, so clients can auto-approve reads and gate the rest. Tools run in worker threads, so a slow call (search, history sync, waiting) never blocks the others.

**Reading**

| Tool | Description |
|---|---|
| `line_whoami` | Profile, E2EE status, local store + live-sync status |
| `line_chats(limit?)` | Recent chats: `chat_id`, contact/group name, unread count, last-message preview |
| `line_read(chat_id, count?, before_message_id?)` | Messages, E2EE-decrypted (incl. your own), oldest first, with `sender_name` (also for group members who aren't your friends), ISO `time`, `reply_to`, `file_name`, stickers. Pass the oldest id as `before_message_id` to page back |
| `line_get_chat(chat_id)` / `line_contact_chats(contact_id)` / `line_last_interaction(contact_id)` | Chat info / find a DM / latest message with a contact |
| `line_message_context(chat_id, message_id, before?, after?)` | Messages around a specific message |
| `line_search_messages(query, chat_id?, limit?)` | Substring search (Thai/Japanese friendly) over everything stored locally, newest first |
| `line_sync_history(chat_id, max_messages?)` | Page back through a chat and store it, so search covers it |
| `line_new_messages(since_minutes?, chat_id?, include_mine?)` | What arrived recently (live sync + store) |
| `line_wait_for_message(timeout_seconds?, chat_id?)` | Block until the next incoming message arrives |
| `line_read_receipts(chat_id, message_id?)` | Who read the chat, how far, and whether they read a given message |
| `line_find_contact(name)` / `line_groups()` / `line_group_members(chat_id)` | Contacts, groups, group members (with names) |
| `line_get_image(message_id, chat_id?)` | An image message, returned as the image (E2EE images are decrypted) |
| `line_download_media(message_id, chat_id?, save_dir?, file_name?)` | Any media to disk (E2EE decrypted; default `~/.line-mcp/media`) |

**Acting as you** (only with the user's explicit approval; max `LINE_MAX_SENDS_PER_MIN`, default 10/min)

| Tool | Description |
|---|---|
| `line_send(chat_id, text, reply_to_message_id?)` | Send a text, optionally as a reply |
| `line_send_file(chat_id, file_path, duration_ms?)` | Photo / video / document (images go as photos, not attachments) |
| `line_send_voice(chat_id, audio_path, duration_ms?)` | Voice message |
| `line_send_sticker(chat_id, package_id, sticker_id)` | Sticker (only packs you own) |
| `line_react(message_id, reaction?)` / `line_unreact(message_id)` | 👍 nice, ❤️ love, 😆 fun, 😮 amazing, 😢 sad, 😱 omg |
| `line_unsend(message_id)` | Unsend one of your messages for everyone |
| `line_mark_read(chat_id)` | Mark a chat read — **sends read receipts** to the others in the chat |

**Login**: `line_login_start` / `line_login_status` (see above).

## Local message store & live sync

`line-mcp serve` keeps every message it sees in `~/.line-mcp/messages.db`: everything you read, sync or search, plus everything that arrives while it runs. It subscribes to LINE's operation stream, exactly like the Chrome extension does, so new messages and unsends are picked up in real time. The last processed revision is saved, so after a restart it catches up on what arrived while it was down. LINE doesn't stream every message to a companion device (for example, messages you send from your phone), so the server also checks once a minute for chats whose latest message it hasn't seen yet. That costs one or two API calls; set the interval with `LINE_MCP_POLL_SECONDS`.

This powers full search, `line_new_messages` and `line_wait_for_message`. The database contains **decrypted message text**, so it's created mode 600 in the 700 `~/.line-mcp` directory, like the session file. Set `LINE_MCP_STORE=off` to disable it, or `LINE_MCP_DB=/path` to move it.

## Limits

- **History depth:** the LINE Chrome protocol only serves recent history to a companion device. In testing, about the last two weeks. Anything older is only available if the store saw it while it was recent.
- **Stickers:** `line_send_sticker` only works with packs you own; LINE rejects others.
- **Rate limits:** requests are capped at ~4/s, E2EE public keys are cached, and outgoing actions are capped at 10/min, all to stay clear of LINE's abuse detection. This lowers the risk; it can't remove it (see the warning at the top).
- **Paging:** `line_read` paging needs a message id the server has seen, from a previous `line_read` or the chat's last 200 messages. The Chrome gateway doesn't allow looking messages up by id.
- **Groups:** okline can't create the *first* E2EE key of a group, so sending into a sealed group that has never had an encrypted message may fail.
- **okline version:** it's pinned to `<2.8` because this server patches parts of okline's E2EE internals; newer versions need re-testing first.

## Safety notes for agent builders

- Treat `line_send` like a loaded footgun: require the user's explicit approval of the exact message text, per message.
- Never commit `session.json` / `qr.png`. They are bearer credentials for the account.

## License

MIT. Built on [okline](https://github.com/nicedayzc/okline) (check its license/terms too).

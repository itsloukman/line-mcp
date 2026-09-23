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

Check your setup any time:

```bash
line-mcp doctor   # Node.js, session file + permissions, login, E2EE keys
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

Read tools are annotated `readOnlyHint`; tools that act as you are annotated as writes, so clients can auto-approve reads and gate the rest.

| Tool | Description |
|---|---|
| `line_whoami` | Logged-in profile (mid, display name) + whether E2EE decryption is available |
| `line_chats(limit?)` | Recent chats: `chat_id`, resolved contact/group name, unread count, last-message preview |
| `line_get_chat(chat_id)` | Info about one chat: name, kind, unread count, last message id |
| `line_contact_chats(contact_id)` | Find the 1:1 chat with a contact |
| `line_last_interaction(contact_id)` | Most recent message with a contact |
| `line_read(chat_id, count?, before_message_id?)` | Messages, E2EE-decrypted, oldest first, with `sender_name`, ISO `time`, `reply_to`, `file_name`, stickers. Pass the oldest id as `before_message_id` to page back through history |
| `line_message_context(chat_id, message_id, before?, after?)` | Messages surrounding a specific message |
| `line_search_messages(query, per_chat?, chat_limit?)` | Substring search over the recent messages of recent chats (not full history; max 50 hits) |
| `line_find_contact(name)` | Find contacts by name / your nickname for them (mid doubles as DM `chat_id`) |
| `line_groups()` | Groups you're in: `chat_id` + name |
| `line_get_image(message_id, chat_id?)` | Download an image message — returns the image itself (not for E2EE media, see Limits) |
| `line_download_media(message_id, chat_id?, save_dir?, file_name?)` | Download media to disk (default `~/.line-mcp/media`, original file name kept) — returns the local path |
| `line_send(chat_id, text, reply_to_message_id?)` | Send a text (optionally as a reply) **as you** — only with explicit approval |
| `line_send_file(chat_id, file_path, duration_ms?)` | Send a photo / video / document **as you** (images go as photos, not attachments) |
| `line_send_voice(chat_id, audio_path, duration_ms?)` | Send a voice message **as you** |
| `line_send_sticker(chat_id, package_id, sticker_id)` | Send a LINE sticker **as you** (only packs you own) |
| `line_unsend(message_id)` | Unsend one of your messages for everyone |
| `line_mark_read(chat_id)` | Mark a chat read — **sends read receipts** to the others in the chat |

## Limits

- Requests are rate-limited to ~4/s and E2EE public keys are cached, to stay under LINE's abuse detection. Search is therefore bounded (default: 30 messages × 20 chats, ~10 s).
- **E2EE (Letter Sealing) media can't be downloaded.** Most photos/files people send you in 1:1 and group chats are sealed; the LINE Chrome protocol this server uses gets `401` from the media server for them, so `line_get_image` / `line_download_media` return a clear error instead. Non-sealed media (and media you send through this server) downloads fine. Text in sealed chats decrypts normally — including your own sent messages.
- Media you *send* goes through LINE's non-sealed (V1) path; it worked in live tests (photo arrives as a photo), but okline marks it experimental.
- `line_send_sticker` only works with sticker packs you own; LINE rejects others ("sticker is not owned").
- `line_read` paging (`before_message_id`) needs an id the server has seen (from a previous `line_read`, or within the chat's last 200 messages) — the Chrome gateway doesn't allow looking messages up by id.
- Creating the *first* E2EE key of a group isn't supported by okline, so sending into a sealed group that has never had an encrypted message can fail.

## Safety notes for agent builders

- Treat `line_send` like a loaded footgun: require the user's explicit approval of the exact message text, per message.
- Never commit `session.json` / `qr.png`. They are bearer credentials for the account.

## License

MIT. Built on [okline](https://github.com/nicedayzc/okline) (check its license/terms too).

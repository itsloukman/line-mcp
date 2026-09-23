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

Requires Python 3.10+.

## Login

```bash
line-mcp login
```

Scan the QR code (`~/.line-mcp/qr.png`) with LINE on your phone, enter the PIN it shows on your phone, and the session is saved to `~/.line-mcp/session.json` (mode 600 — tokens are secrets, never commit this file).

Set `LINE_SESSION_PATH` to override the session location.

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
      "env": { "LINE_SESSION_PATH": "/home/you/.line-mcp/session.json" }
    }
  }
}
```

## Tools

| Tool | Description |
|---|---|
| `line_whoami` | Logged-in profile (mid, display name) |
| `line_chats(limit?)` | Recent chats: `chat_id`, resolved name, unread count |
| `line_get_chat(chat_id)` | Info about one chat: kind, unread count, last message |
| `line_contact_chats(contact_id)` | Find the 1:1 chat with a contact |
| `line_last_interaction(contact_id)` | Most recent message with a contact |
| `line_read(chat_id, count?)` | Recent messages, E2EE-decrypted, oldest first (images flagged with `has_image`; stickers include package/sticker ids + preview URL) |
| `line_message_context(chat_id, message_id, before?, after?)` | Messages surrounding a specific message |
| `line_search_messages(query, per_chat?, chat_limit?)` | Full-text search over recent messages across chats |
| `line_send(chat_id, text)` | Send a text message **as you** — only with explicit approval |
| `line_send_file(chat_id, file_path)` | Send an image/video/document **as you** — only with explicit approval |
| `line_send_voice(chat_id, audio_path, duration_ms?)` | Send a voice message **as you** — only with explicit approval |
| `line_send_sticker(chat_id, package_id, sticker_id)` | Send a LINE sticker **as you** — only with explicit approval |
| `line_get_image(message_id)` | Download an image message — returns the image itself |
| `line_download_media(message_id, save_dir?)` | Download any media to disk — returns the local path |
| `line_unsend(message_id)` | Unsend one of your messages — only with explicit approval |
| `line_mark_read(chat_id)` | Mark a chat as read |
| `line_find_contact(name)` | Find contacts by name (mid doubles as DM `chat_id`) |
| `line_groups()` | Groups: id + name |
| `line_groups()` | Groups: id + name |

## Safety notes for agent builders

- Treat `line_send` like a loaded footgun: require the user's explicit approval of the exact message text, per message.
- Never commit `session.json` / `qr.png`. They are bearer credentials for the account.

## License

MIT. Built on [okline](https://github.com/nicedayzc/okline) (check its license/terms too).

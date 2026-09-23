"""MCP server exposing a personal LINE account.

Transport: stdio. Run with `line-mcp serve` (or `python -m line_mcp serve`).
Requires a prior `line-mcp login` (QR scan on your phone).
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent

from . import client as C

mcp = FastMCP(
    "line-personal",
    instructions=(
        "Unofficial bridge to the user's personal LINE account (companion device; "
        "the phone stays logged in). Chats are addressed by chat_id from line_chats. "
        "Messages are E2EE-decrypted when possible. Sending messages acts as the user: "
        "only send when the user explicitly approved the exact message."
    ),
)


def _api():
    return C.load_api()


def _my_mid(api) -> str | None:
    try:
        return api.get_profile().get("mid")
    except Exception:
        return None


@mcp.tool()
def line_whoami() -> dict:
    """Return the logged-in LINE profile (mid, display name)."""
    api = _api()
    p = api.get_profile()
    return {"mid": p.get("mid"), "displayName": p.get("displayName")}


@mcp.tool()
def line_chats(limit: int = 30) -> list[dict]:
    """List recent chats: chat_id, resolved name, unread count. Use chat_id with line_read/line_send."""
    return C.chats_to_list(_api(), limit=limit)


@mcp.tool()
def line_read(chat_id: str, count: int = 20) -> list[dict]:
    """Read recent messages from a chat (E2EE-decrypted). Oldest first. Image messages come back with has_image=true and text "[image]" — fetch the bytes with line_get_image(message_id)."""
    api = _api()
    my_mid = _my_mid(api)
    msgs = api.get_recent_messages(chat_id, count) or []
    return [
        C.message_to_dict(api, my_mid, m)
        for m in reversed(msgs)
        if isinstance(m, dict)
    ]


@mcp.tool()
def line_send(chat_id: str, text: str) -> dict:
    """Send a text message to a chat as the user. Only call with the user's explicit approval of the exact text."""
    api = _api()
    return {"result": api.send_text(chat_id, text)}


@mcp.tool()
def line_get_chat(chat_id: str) -> dict:
    """Get info about a chat: id, kind, unread count, last message id."""
    api = _api()
    box = C.find_box(api, chat_id)
    if not box:
        return {"error": "chat not found"}
    return C.box_to_dict(box)


@mcp.tool()
def line_contact_chats(contact_id: str) -> dict:
    """Find the 1:1 chat with a contact (contact_id is their user mid). Note: group membership is not exposed by the underlying library, so only direct chats are returned."""
    api = _api()
    box = C.find_box(api, contact_id)
    if not box:
        return {"error": "no direct chat found with this contact"}
    return C.box_to_dict(box)


@mcp.tool()
def line_last_interaction(contact_id: str) -> dict:
    """Get the most recent message in the 1:1 chat with a contact."""
    api = _api()
    me = _my_mid(api)
    msgs = api.get_recent_messages(contact_id, 1) or []
    if not msgs:
        return {"error": "no messages found"}
    return C.message_to_dict(api, me, msgs[0])


@mcp.tool()
def line_message_context(
    chat_id: str, message_id: str, before: int = 5, after: int = 5
) -> dict:
    """Get the messages surrounding a specific message (for context). Reads up to 300 recent messages."""
    api = _api()
    ctx = C.message_context(api, _my_mid(api), chat_id, message_id, before=before, after=after)
    if ctx is None:
        return {"error": "message not found in the last 300 messages of this chat"}
    return ctx


@mcp.tool()
def line_search_messages(
    query: str, per_chat: int = 40, chat_limit: int = 30
) -> list:
    """Full-text (substring) search over recent messages across the most recent chats. Returns matching messages with their chat_id."""
    api = _api()
    return C.search_messages(api, _my_mid(api), query, per_chat=per_chat, chat_limit=chat_limit)


@mcp.tool()
def line_send_file(chat_id: str, file_path: str) -> dict:
    """Send a file (image, video, document) to a chat as the user. file_path is a local path on the machine running the server. Only call with the user's explicit approval."""
    api = _api()
    with open(file_path, "rb") as f:
        data = f.read()
    import os

    return {"result": api.send_file(chat_id, data, name=os.path.basename(file_path))}


@mcp.tool()
def line_send_voice(chat_id: str, audio_path: str, duration_ms: int = 0) -> dict:
    """Send an audio file as a LINE voice message as the user. audio_path is a local path on the machine running the server. Only call with the user's explicit approval."""
    api = _api()
    with open(audio_path, "rb") as f:
        data = f.read()
    return {"result": api.send_audio(chat_id, data, duration_ms=duration_ms)}


@mcp.tool()
def line_send_sticker(chat_id: str, package_id: str, sticker_id: str) -> dict:
    """Send a LINE sticker to a chat as the user. Stickers are core LINE culture — use package_id/sticker_id from a sticker the user received (see the 'sticker' field in line_read). Only call with the user's explicit approval."""
    api = _api()
    return {"result": api.send_sticker(chat_id, package_id, sticker_id)}


@mcp.tool()
def line_download_media(message_id: str, save_dir: str = "") -> dict:
    """Download any media message (image/video/voice/file) to disk and return the local file path, mime type, and size. For images you can use line_get_image instead to get the image inline."""
    api = _api()
    return C.download_media_to_file(api, message_id, save_dir or None)


@mcp.tool()
def line_unsend(message_id: str) -> dict:
    """Unsend (recall) a message you sent. Only works on your own recent messages. Only call with the user's explicit approval."""
    api = _api()
    return {"result": api.unsend_message(message_id)}


@mcp.tool()
def line_mark_read(chat_id: str) -> dict:
    """Mark a chat as read up to its latest message."""
    api = _api()
    msgs = api.get_recent_messages(chat_id, 1) or []
    if not msgs:
        return {"error": "no messages in chat"}
    return {"result": api.mark_as_read(chat_id, str(msgs[0].get("id")))}


@mcp.tool()
def line_get_image(message_id: str):
    """Download an image message's bytes and return the image itself. Get message_id from line_read (messages with has_image=true)."""
    import base64

    api = _api()
    data, mime = C.download_image(api, message_id)
    return ImageContent(
        type="image", data=base64.b64encode(data).decode("ascii"), mimeType=mime
    )


@mcp.tool()
def line_find_contact(name: str) -> list[dict]:
    """Find contacts by display-name substring. Returns mid + displayName (mid doubles as chat_id for DMs)."""
    api = _api()
    contacts, _ = C._name_cache()
    needle = name.lower()
    return [
        {"mid": mid, "displayName": dn}
        for mid, dn in contacts.items()
        if needle in dn.lower()
    ]


@mcp.tool()
def line_groups() -> list[dict]:
    """List LINE groups: id (chat_id) and name."""
    from .client import _unwrap_groups

    return [
        {"chat_id": gid, "name": name}
        for gid, name in _unwrap_groups(_api()).items()
    ]

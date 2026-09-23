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
def line_get_image(message_id: str):
    """Download an image message's bytes and return the image itself. Get message_id from line_read (messages with has_image=true)."""
    import base64

    api = _api()
    data, mime = C.download_image(api, message_id)
    return ImageContent(
        type="image", data=base64.b64encode(data).decode("ascii"), mimeType=mime
    )


@mcp.tool()
def line_send_image(chat_id: str, image_path: str) -> dict:
    """Send an image file to a chat as the user. image_path is a local file path on the machine running the server. Only call with the user's explicit approval."""
    api = _api()
    with open(image_path, "rb") as f:
        data = f.read()
    return {"result": api.send_image(chat_id, data)}


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

"""MCP server exposing a personal LINE account.

Transport: stdio. Run with `line-mcp serve` (or `python -m line_mcp serve`).
Requires a prior `line-mcp login` (QR scan on your phone).
"""

from __future__ import annotations

import base64
import functools
import os

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, ToolAnnotations

from . import client as C

mcp = FastMCP(
    "line-personal",
    instructions=(
        "Unofficial bridge to the user's personal LINE account (companion device; "
        "the phone stays logged in). Chats are addressed by chat_id from line_chats "
        "(a contact's mid doubles as the chat_id of the 1:1 chat). "
        "Messages are E2EE-decrypted when possible. Sending, unsending and marking "
        "read act as the user and are visible to others: only do them when the user "
        "explicitly approved the exact action. Keep request volumes human-like."
    ),
)

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
LOCAL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
SEND = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)
MARK = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(v)))


def _friendly_errors(fn):
    """Turn okline's low-level failures into actionable messages."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        from okline.exceptions import LineApiError, LineAuthError
        from okline.hmac_signer import HmacSignerError

        try:
            return fn(*args, **kwargs)
        except LineAuthError as exc:
            C.reset_api()  # next call reloads the session file
            raise RuntimeError(
                f"LINE rejected the session ({exc}). It may have expired or been logged "
                "out from the phone — run `line-mcp login` again."
            ) from exc
        except HmacSignerError as exc:
            C.reset_api()
            raise RuntimeError(
                f"{str(exc).rstrip('.')}. The server needs Node.js 18+; if it is installed but not on the "
                "MCP client's PATH, set LINE_NODE=/path/to/node in the server's env."
            ) from exc
        except LineApiError as exc:
            if exc.code == 32 and "sticker" in str(exc).lower():  # "not owned by the user"
                raise RuntimeError(
                    "LINE only lets you send stickers you own (bought or downloaded "
                    "free packs) — pick a sticker from a pack in your collection."
                ) from exc
            raise

    return wrapper


def tool(annotations: ToolAnnotations):
    def deco(fn):
        return mcp.tool(annotations=annotations)(_friendly_errors(fn))

    return deco


def _sent(result) -> dict:
    """Compact send result (the raw Message can carry large sealed chunks)."""
    if isinstance(result, dict):
        return {"sent": True, "message_id": result.get("id"), "created_ms": result.get("createdTime")}
    return {"sent": True, "result": result}


def _local_file(path: str) -> str:
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no such file: {path}")
    return path


@tool(READ)
def line_whoami() -> dict:
    """Return the logged-in LINE profile (mid, display name) and whether E2EE decryption is available."""
    api = C.get_api()
    p = api.get_profile()
    return {
        "mid": p.get("mid"),
        "displayName": p.get("displayName"),
        "e2ee_ready": bool(api.e2ee and api.e2ee.is_ready()),
    }


@tool(READ)
def line_chats(limit: int = 30) -> list[dict]:
    """List recent chats (newest first): chat_id, resolved name, unread count and a last-message preview. Use chat_id with line_read/line_send."""
    return C.chats_to_list(C.get_api(), limit=_clamp(limit, 1, 100))


@tool(READ)
def line_read(chat_id: str, count: int = 20, before_message_id: str = "") -> list[dict]:
    """Read messages from a chat (E2EE-decrypted), oldest first. To scroll back, pass the id of the oldest message you have as before_message_id to get the page before it. Image messages have has_image=true — fetch them with line_get_image(message_id)."""
    api = C.get_api()
    msgs = C.read_messages(api, chat_id, _clamp(count, 1, 200), before_message_id or None)
    return C.messages_to_dicts(api, msgs)


@tool(SEND)
def line_send(chat_id: str, text: str, reply_to_message_id: str = "") -> dict:
    """Send a text message to a chat as the user (optionally as a reply to a message id). Only call with the user's explicit approval of the exact text."""
    api = C.get_api()
    if reply_to_message_id:
        return _sent(api.reply_text(chat_id, text, str(reply_to_message_id)))
    return _sent(api.send_text(chat_id, text))


@tool(READ)
def line_get_chat(chat_id: str) -> dict:
    """Get info about a chat: id, name, kind, unread count, last message id."""
    api = C.get_api()
    box = C.find_box(api, chat_id)
    if not box:
        return {"error": "chat not found"}
    return C.box_to_dict(box)


@tool(READ)
def line_contact_chats(contact_id: str) -> dict:
    """Find the 1:1 chat with a contact (contact_id is their user mid). Note: group membership is not looked up, so only the direct chat is returned."""
    api = C.get_api()
    box = C.find_box(api, contact_id)
    if not box:
        return {"error": "no direct chat found with this contact"}
    return C.box_to_dict(box)


@tool(READ)
def line_last_interaction(contact_id: str) -> dict:
    """Get the most recent message in the 1:1 chat with a contact."""
    api = C.get_api()
    msgs = C.messages_to_dicts(api, C.read_messages(api, contact_id, 1))
    if not msgs:
        return {"error": "no messages found"}
    return msgs[-1]


@tool(READ)
def line_message_context(
    chat_id: str, message_id: str, before: int = 5, after: int = 5
) -> dict:
    """Get the messages surrounding a specific message (for context). Reads up to 300 recent messages."""
    api = C.get_api()
    ctx = C.message_context(
        api, C.my_mid(api), chat_id, message_id,
        before=_clamp(before, 0, 50), after=_clamp(after, 0, 50),
    )
    if ctx is None:
        return {"error": "message not found in the last 300 messages of this chat"}
    return ctx


@tool(READ)
def line_search_messages(query: str, per_chat: int = 30, chat_limit: int = 20) -> list:
    """Case-insensitive substring search over the recent messages (per_chat per chat) of the most recent chats (chat_limit). Returns up to 50 matches with chat_id and chat_name. This is not a full-history search."""
    if not query.strip():
        raise ValueError("query must not be empty")
    api = C.get_api()
    return C.search_messages(
        api, C.my_mid(api), query,
        per_chat=_clamp(per_chat, 1, 100), chat_limit=_clamp(chat_limit, 1, 50),
    )


@tool(SEND)
def line_send_file(chat_id: str, file_path: str, duration_ms: int = 0) -> dict:
    """Send a local file to a chat as the user. Images (jpg/png/gif/webp) are sent as photos, videos as videos (pass duration_ms), anything else as a file attachment. file_path is on the machine running the server. Only call with the user's explicit approval."""
    api = C.get_api()
    path = _local_file(file_path)
    kind = C.media_kind(path)
    if kind == "image":
        res = api.send_image(chat_id, path)
    elif kind == "video":
        res = api.send_video(chat_id, path, duration_ms=max(0, int(duration_ms)))
    else:
        res = api.send_file(chat_id, path)
    return {**_sent(res), "sent_as": kind}


@tool(SEND)
def line_send_voice(chat_id: str, audio_path: str, duration_ms: int = 0) -> dict:
    """Send an audio file (m4a/aac recommended) as a LINE voice message as the user. Pass duration_ms so LINE shows the right length. audio_path is on the machine running the server. Only call with the user's explicit approval."""
    api = C.get_api()
    return _sent(api.send_audio(chat_id, _local_file(audio_path), duration_ms=max(0, int(duration_ms))))


@tool(SEND)
def line_send_sticker(chat_id: str, package_id: str, sticker_id: str) -> dict:
    """Send a LINE sticker to a chat as the user. The user must own the sticker pack; package_id/sticker_id come from the 'sticker' field in line_read (e.g. a sticker the user sent before). Only call with the user's explicit approval."""
    api = C.get_api()
    return _sent(api.send_sticker(chat_id, str(package_id), str(sticker_id)))


@tool(LOCAL_WRITE)
def line_download_media(message_id: str, chat_id: str = "", save_dir: str = "", file_name: str = "") -> dict:
    """Download a media message (image/video/voice/file) to disk and return the local path, mime type and size. Pass the chat_id it came from so its metadata can be found. For images, line_get_image returns the image inline instead. End-to-end-encrypted (Letter Sealing) media can't be downloaded."""
    api = C.get_api()
    return C.download_media_to_file(api, message_id, save_dir or None, file_name or None, chat_id or None)


@tool(SEND)
def line_unsend(message_id: str) -> dict:
    """Unsend (recall) a message you sent, for everyone in the chat. Only works on your own recent messages. Only call with the user's explicit approval."""
    api = C.get_api()
    api.unsend_message(str(message_id))
    return {"unsent": True, "message_id": str(message_id)}


@tool(MARK)
def line_mark_read(chat_id: str) -> dict:
    """Mark a chat as read up to its latest message. This sends read receipts ("Read") to the other people in the chat — only call when the user asked for it."""
    api = C.get_api()
    msgs = [m for m in C.read_messages(api, chat_id, 1) if isinstance(m, dict)]
    if not msgs:
        return {"error": "no messages in chat"}
    last_id = str(msgs[0].get("id"))
    api.mark_as_read(chat_id, last_id)
    return {"marked_read": True, "up_to_message_id": last_id}


@tool(READ)
def line_get_image(message_id: str, chat_id: str = ""):
    """Download an image message and return the image itself. Get message_id (and chat_id) from line_read (messages with has_image=true). End-to-end-encrypted (Letter Sealing) images can't be downloaded."""
    api = C.get_api()
    data, mime = C.download_image(api, message_id, chat_id or None)
    if not mime.startswith("image/"):
        raise ValueError(
            f"message {message_id} did not download as an image ({mime}, {len(data)} bytes); "
            "try line_download_media."
        )
    return ImageContent(type="image", data=base64.b64encode(data).decode("ascii"), mimeType=mime)


@tool(READ)
def line_find_contact(name: str) -> list[dict]:
    """Find contacts by name substring (case-insensitive; matches your nickname for them or their display name). Returns mid + displayName (mid doubles as chat_id for DMs)."""
    needle = name.lower().strip()

    def matches(contacts: dict) -> list[dict]:
        return [{"mid": mid, "displayName": dn} for mid, dn in contacts.items() if needle in dn.lower()]

    hits = matches(C._name_cache()[0])
    return hits or matches(C._name_cache(refresh=True)[0])  # maybe a brand-new contact


@tool(READ)
def line_groups() -> list[dict]:
    """List the LINE groups you are a member of: chat_id and name."""
    _, groups = C._name_cache()
    return [{"chat_id": gid, "name": name} for gid, name in groups.items()]

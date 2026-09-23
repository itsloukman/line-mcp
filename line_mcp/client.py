"""Shared LINE session helpers for the MCP server and CLI."""

from __future__ import annotations

import json
import os
import stat
from functools import lru_cache

from okline import OkLine

DEFAULT_SESSION_PATH = os.path.expanduser("~/.line-mcp/session.json")


def session_path() -> str:
    return os.environ.get("LINE_SESSION_PATH", DEFAULT_SESSION_PATH)


def load_api() -> OkLine:
    path = session_path()
    if not os.path.exists(path):
        raise RuntimeError(
            f"No LINE session found at {path}. Run `line-mcp login` first."
        )
    return OkLine.from_tokens_file(path)


def save_session(api: OkLine, path: str | None = None) -> str:
    path = path or session_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    api.save_tokens(path)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 600: tokens are secrets
    return path


def _unwrap_contacts(api: OkLine) -> dict:
    """mid -> display name for all contacts."""
    try:
        mids = api.get_all_contact_ids() or []
        res = api.get_contacts(mids) if mids else {}
        entries = res.get("contacts", {}) if isinstance(res, dict) else {}
        out = {}
        for mid, entry in entries.items():
            c = entry.get("contact", {}) if isinstance(entry, dict) else {}
            name = str(c.get("displayName", "") or "").strip()
            if name:
                out[mid] = name
        return out
    except Exception:
        return {}


def _unwrap_groups(api: OkLine) -> dict:
    """group id -> group name."""
    try:
        groups = api.get_groups() or []
    except Exception:
        return {}
    items = groups.get("groups", groups) if isinstance(groups, dict) else groups
    out = {}
    for g in items if isinstance(items, list) else []:
        if not isinstance(g, dict):
            continue
        gid = g.get("id") or g.get("groupId") or g.get("mid")
        name = g.get("name") or g.get("groupName")
        if gid and name:
            out[str(gid)] = str(name)
    return out


@lru_cache(maxsize=1)
def _name_cache() -> tuple[dict, dict]:
    # Cache is per-process; the MCP server is long-lived but contacts rarely
    # change mid-session. Callers can bust with _name_cache.cache_clear().
    api = load_api()
    return _unwrap_contacts(api), _unwrap_groups(api)


def resolve_chat_name(chat_id: str) -> str:
    contacts, groups = _name_cache()
    return contacts.get(chat_id) or groups.get(chat_id) or chat_id


def decrypt_text(api: OkLine, m: dict) -> str:
    text = m.get("text") or ""
    if m.get("chunks") and getattr(api, "e2ee", None) is not None:
        try:
            if api.e2ee.is_ready():
                text = api.decrypt_message(m).get("text") or text
        except Exception:
            text = "[encrypted]"
    return text


def message_to_dict(api: OkLine, my_mid: str | None, m: dict) -> dict:
    ts = m.get("createdTime")
    ctype = m.get("contentType")
    text = decrypt_text(api, m)
    if ctype == 1 and not text:
        text = "[image]"
    return {
        "id": m.get("id"),
        "from": m.get("from"),
        "from_me": bool(my_mid) and m.get("from") == my_mid,
        "type": ctype,
        "text": text,
        "has_image": ctype == 1,
        "created_ms": ts,
    }


def sniff_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def download_image(api: OkLine, message_id: str) -> tuple[bytes, str]:
    """Download an image message's bytes. Returns (bytes, mime)."""
    data = api.obs.download_object("talk", "m", message_id)
    return bytes(data), sniff_mime(bytes(data))


def chats_to_list(api: OkLine, limit: int = 30) -> list[dict]:
    boxes = api.get_message_boxes(limit=limit)
    items = boxes.get("messageBoxes", []) if isinstance(boxes, dict) else []
    return [
        {
            "chat_id": b.get("id"),
            "name": resolve_chat_name(str(b.get("id"))),
            "unread": b.get("unreadCount", 0),
        }
        for b in items
        if isinstance(b, dict) and b.get("id")
    ]

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


TYPE_LABELS = {
    0: "text",
    1: "image",
    2: "video",
    3: "voice message",
    6: "location",
    7: "sticker",
    13: "contact",
    14: "file",
    16: "audio",
    22: "flex message",
}


def sticker_info(m: dict) -> dict | None:
    meta = m.get("contentMetadata") or {}
    pkg = meta.get("STKPKGID")
    sid = meta.get("STKID")
    if not pkg or not sid:
        return None
    return {
        "package_id": pkg,
        "sticker_id": sid,
        "preview_url": f"https://stickershop.line-scdn.net/stickershop/v1/sticker/{sid}/iPhone/sticker@2x.png",
    }


def message_to_dict(api: OkLine, my_mid: str | None, m: dict) -> dict:
    ts = m.get("createdTime")
    ctype = m.get("contentType")
    text = decrypt_text(api, m)
    out: dict = {
        "id": m.get("id"),
        "from": m.get("from"),
        "from_me": bool(my_mid) and m.get("from") == my_mid,
        "type": ctype,
        "text": text,
        "has_image": ctype == 1,
        "created_ms": ts,
    }
    if ctype == 7:
        info = sticker_info(m)
        out["text"] = "[sticker]" if not text else text
        if info:
            out["sticker"] = info
    elif ctype and ctype != 0 and not text:
        out["text"] = f"[{TYPE_LABELS.get(ctype, f'message type {ctype}')}]"
    return out


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


def download_media_to_file(
    api: OkLine, message_id: str, save_dir: str | None = None
) -> dict:
    """Download any media message (image/video/voice/file) to disk. Returns path info."""
    import os

    data = bytes(api.obs.download_object("talk", "m", message_id))
    mime = sniff_mime(data)
    ext = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(mime, ".bin")
    directory = save_dir or os.path.join(os.path.expanduser("~"), ".line-mcp", "media")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{message_id}{ext}")
    with open(path, "wb") as f:
        f.write(data)
    return {"path": path, "mime": mime, "bytes": len(data)}


def find_box(api: OkLine, chat_id: str) -> dict | None:
    boxes = api.get_message_boxes(limit=200).get("messageBoxes", [])
    for b in boxes:
        if b.get("id") == chat_id:
            return b
    return None


def box_to_dict(box: dict) -> dict:
    last = box.get("lastMessages") or []
    return {
        "id": box.get("id"),
        "kind": box.get("midType"),
        "unread_count": box.get("unreadCount"),
        "last_message_id": (last[0].get("id") if last else None),
    }


def search_messages(
    api: OkLine,
    my_mid: str | None,
    query: str,
    *,
    per_chat: int = 40,
    chat_limit: int = 30,
) -> list[dict]:
    """Substring search over recent messages across the most recent chats."""
    q = query.lower()
    hits: list[dict] = []
    boxes = api.get_message_boxes(limit=chat_limit).get("messageBoxes", [])
    for b in boxes:
        chat_id = b.get("id")
        msgs = api.get_recent_messages(chat_id, per_chat) or []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            d = message_to_dict(api, my_mid, m)
            if d["text"] and q in d["text"].lower():
                d["chat_id"] = chat_id
                hits.append(d)
    return hits


def message_context(
    api: OkLine,
    my_mid: str | None,
    chat_id: str,
    message_id: str,
    *,
    before: int = 5,
    after: int = 5,
) -> dict | None:
    """Messages surrounding a given message id (reads up to 300 recent messages)."""
    msgs = api.get_recent_messages(chat_id, 300) or []
    msgs = [m for m in msgs if isinstance(m, dict)]
    idx = next((i for i, m in enumerate(msgs) if str(m.get("id")) == str(message_id)), None)
    if idx is None:
        return None
    # get_recent_messages is newest-first; convert to oldest-first for context
    ordered = list(reversed(msgs))
    pos = len(ordered) - 1 - idx
    window = ordered[max(0, pos - before) : pos + after + 1]
    return {
        "target_index": min(pos, before),
        "messages": [message_to_dict(api, my_mid, m) for m in window],
    }


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

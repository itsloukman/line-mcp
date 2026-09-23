"""Shared LINE session helpers for the MCP server and CLI."""

from __future__ import annotations

import atexit
import base64
import logging
import os
import stat
import threading
import time
from collections import OrderedDict
from datetime import datetime

from okline import OkLine

log = logging.getLogger("line_mcp")

DEFAULT_SESSION_DIR = os.path.expanduser("~/.line-mcp")
DEFAULT_SESSION_PATH = os.path.join(DEFAULT_SESSION_DIR, "session.json")

# Requests per second sent to LINE. Unofficial clients that burst get throttled
# (EXCESSIVE_ACCESS) or blocked (ABUSE_BLOCK), so keep this human-ish.
RATE_PER_SEC = 4.0
NAME_CACHE_TTL = 600  # seconds

UNDECRYPTABLE = "[encrypted message — could not decrypt; run `line-mcp login` again]"


def session_path() -> str:
    return os.environ.get("LINE_SESSION_PATH", DEFAULT_SESSION_PATH)


# -- Node.js discovery -----------------------------------------------------------


def find_node() -> str | None:
    """Locate a node binary: LINE_NODE, PATH, then common install locations.

    MCP clients (e.g. Claude Desktop) launch servers with a minimal PATH that
    usually misses Homebrew / nvm / volta installs.
    """
    import glob
    import shutil

    explicit = os.environ.get("LINE_NODE")
    if explicit:
        return explicit
    found = shutil.which("node")
    if found:
        return found
    home = os.path.expanduser("~")
    candidates = [
        "/opt/homebrew/bin/node",
        "/usr/local/bin/node",
        os.path.join(home, ".local/bin/node"),
        os.path.join(home, ".volta/bin/node"),
        *sorted(glob.glob(os.path.join(home, ".nvm/versions/node/*/bin/node")), reverse=True),
        *sorted(glob.glob(os.path.join(home, ".fnm/node-versions/*/installation/bin/node")), reverse=True),
    ]
    return next((c for c in candidates if os.path.isfile(c) and os.access(c, os.X_OK)), None)


def ensure_node_env() -> str | None:
    """Point okline at a discovered node (okline reads LINE_NODE)."""
    node = find_node()
    if node and not os.environ.get("LINE_NODE"):
        os.environ["LINE_NODE"] = node
    return node


# -- the one shared client ------------------------------------------------------

_api: OkLine | None = None
_api_lock = threading.Lock()


def get_api() -> OkLine:
    """The process-wide OkLine client (built once, reused by every tool call).

    One instance means one Node bridge process, one set of E2EE keys and one
    set of okline's peer/group key caches — instead of a fresh (leaked) Node
    process and cold caches per call.
    """
    global _api
    with _api_lock:
        if _api is None:
            path = session_path()
            if not os.path.exists(path):
                raise RuntimeError(
                    f"No LINE session found at {path}. Run `line-mcp login` first."
                )
            ensure_node_env()
            api = OkLine.from_tokens_file(path, record=False)
            from okline.ratelimit import RateLimiter

            api.transport.rate_limiter = RateLimiter(rate=RATE_PER_SEC, per=1.0)
            _cache_public_keys(api)
            _fix_own_message_decrypt(api)
            atexit.register(api.close)
            _api = api
        return _api


def reset_api() -> None:
    """Drop the shared client (e.g. after an auth error) so the next call
    reloads the session file — picks up a fresh `line-mcp login`."""
    global _api
    with _api_lock:
        if _api is not None:
            try:
                _api.close()
            except Exception:
                pass
        _api = None
    _names.clear()


# Backwards-compatible name.
load_api = get_api


def _cache_public_keys(api: OkLine) -> None:
    """Memoize getE2EEPublicKey for a specific key id.

    okline's decrypt path fetches the sender's public key once *per message*;
    a key id's public key never changes, so cache it. key_id=0 means "latest"
    and is not cached (the peer may rotate keys).
    """
    orig = api.get_e2ee_public_key
    cache: dict[tuple[str, int, int], object] = {}

    def cached(mid: str, key_version: int = 1, key_id: int = 0):
        if not key_id:
            return orig(mid, key_version, key_id)
        k = (mid, int(key_version), int(key_id))
        if k not in cache:
            cache[k] = orig(mid, key_version, key_id)
        return cache[k]

    api.get_e2ee_public_key = cached  # type: ignore[method-assign]


def _fix_own_message_decrypt(api: OkLine) -> None:
    """Decrypt 1:1 messages *we* sent.

    okline 2.7.0 derives the channel from the *sender's* public key, which for
    our own messages is our key, so they fail ("data authentication
    failure"). The right channel is ECDH(our key[senderKeyId], peer
    key[receiverKeyId]) — the same one used to encrypt them.
    """
    from okline import e2ee_crypto as fr

    e2ee = api.e2ee
    orig = e2ee._decrypt_user
    channels: dict[tuple[str, int, int], int] = {}

    def decrypt_user(message: dict) -> dict:
        me = my_mid(api)
        if not me or message.get("from") != me:
            return orig(message)
        version = fr.message_e2ee_version(message)
        parse = fr.parse_chunks_v1 if version == 1 else fr.parse_chunks
        ciphertext, sender_kid, receiver_kid = parse(message.get("chunks") or [])
        peer = message.get("to") or ""
        key = (peer, int(sender_kid or 0), int(receiver_kid or 0))
        if key not in channels:
            my_handle = e2ee.my_keys.get(key[1]) or e2ee.my_keys.get(e2ee.latest_key_id or 0)
            if my_handle is None:
                raise RuntimeError("no local E2EE key for this message")
            channels[key] = api.transport.bridge.e2ee_create_channel_with_pubkey(
                my_handle, e2ee._user_pub(peer, key[2])
            )
        ct_b64 = base64.b64encode(ciphertext).decode("ascii")
        bridge = api.transport.bridge
        if version == 1:
            pt_b64 = bridge.e2ee_decrypt_v1(channels[key], ciphertext_b64=ct_b64)
        else:
            pt_b64 = bridge.e2ee_decrypt_v2(
                channels[key], to=peer, frm=me, sender_key_id=key[1],
                receiver_key_id=key[2], content_type=int(message.get("contentType", 0)),
                ciphertext_b64=ct_b64,
            )
        return e2ee._finish_decrypt(message, pt_b64)

    e2ee._decrypt_user = decrypt_user  # type: ignore[method-assign]


def my_mid(api: OkLine) -> str | None:
    mid = getattr(api.tokens, "mid", None)
    if mid:
        return mid
    try:
        return api.get_profile().get("mid")
    except Exception:
        return None


def save_session(api: OkLine, path: str | None = None) -> str:
    path = path or session_path()
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    if os.path.abspath(directory) == os.path.abspath(DEFAULT_SESSION_DIR):
        os.chmod(directory, stat.S_IRWXU)  # 700: only we can list it
    # Create the file 600 *before* okline writes the tokens into it, so the
    # secrets are never on disk with the default (world-readable) umask.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    api.save_tokens(path)
    return path


# -- contact / group names ------------------------------------------------------


def _unwrap_contacts(api: OkLine) -> dict:
    """mid -> display name for all contacts (the user's nickname wins)."""
    mids = api.get_all_contact_ids() or []
    res = api.get_contacts(mids) if mids else {}
    entries = res.get("contacts", {}) if isinstance(res, dict) else {}
    out = {}
    for mid, entry in entries.items():
        c = entry.get("contact", {}) if isinstance(entry, dict) else {}
        name = str(c.get("displayNameOverridden") or c.get("displayName") or "").strip()
        if name:
            out[mid] = name
    return out


def _unwrap_groups(api: OkLine) -> dict:
    """group chat mid -> group name."""
    res = api.get_all_chat_mids() or {}
    mids = list(res.get("memberChatMids") or []) if isinstance(res, dict) else []
    chats = api.get_chats(mids, with_members=False, with_invitees=False) if mids else {}
    out = {}
    for c in (chats.get("chats") or []) if isinstance(chats, dict) else []:
        if not isinstance(c, dict):
            continue
        gid, name = c.get("chatMid"), c.get("chatName")
        if gid:
            out[str(gid)] = str(name or gid)
    return out


_names: dict = {}
_names_lock = threading.Lock()


def _name_cache(refresh: bool = False) -> tuple[dict, dict]:
    """(contacts, groups) name maps, cached for NAME_CACHE_TTL seconds.

    Raises if LINE can't be reached — failures are never cached, so a
    transient error doesn't hide every name for the next 10 minutes.
    """
    with _names_lock:
        if refresh or not _names or time.monotonic() - _names["at"] > NAME_CACHE_TTL:
            api = get_api()
            contacts, groups = _unwrap_contacts(api), _unwrap_groups(api)
            _names.update(contacts=contacts, groups=groups, at=time.monotonic())
        return _names["contacts"], _names["groups"]


def names_or_empty() -> tuple[dict, dict]:
    """Best-effort name maps for display (names are optional there)."""
    try:
        return _name_cache()
    except Exception as exc:
        log.warning("name lookup failed: %s", exc)
        return {}, {}


def resolve_chat_name(chat_id: str) -> str:
    contacts, groups = names_or_empty()
    return contacts.get(chat_id) or groups.get(chat_id) or chat_id


# -- messages -------------------------------------------------------------------


def decrypt(api: OkLine, m: dict) -> tuple[dict, bool]:
    """Return (message with plaintext, ok). ok=False if it is sealed and we
    could not decrypt it."""
    if not m.get("chunks"):
        return m, True
    e2ee = getattr(api, "e2ee", None)
    if e2ee is None or not e2ee.is_ready():
        return m, False
    try:
        return api.decrypt_message(m), True
    except Exception as exc:
        log.debug("decrypt of message %s failed: %s", m.get("id"), exc)
        return m, False


def decrypt_text(api: OkLine, m: dict) -> str:
    d, ok = decrypt(api, m)
    return (d.get("text") or "") if ok else UNDECRYPTABLE


# okline.enums.ContentType
TYPE_LABELS = {
    0: "text",
    1: "image",
    2: "video",
    3: "voice message",
    6: "call",
    7: "sticker",
    13: "contact",
    14: "file",
    15: "location",
    16: "post notification",
    18: "chat event",
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


# Recently seen raw messages by id. The Chrome gateway rejects
# getMessagesByIds, so this is how pagination / media tools find a message's
# deliveredTime and metadata from just its id.
_seen: OrderedDict[str, dict] = OrderedDict()
_SEEN_MAX = 3000


def _remember(m: dict) -> None:
    mid = str(m.get("id") or "")
    if not mid:
        return
    _seen[mid] = m
    _seen.move_to_end(mid)
    while len(_seen) > _SEEN_MAX:
        _seen.popitem(last=False)


def lookup_message(api: OkLine, message_id: str, chat_id: str | None = None) -> dict | None:
    """A raw message by id: from the seen cache, else the chat's last 200."""
    mid = str(message_id)
    if mid in _seen:
        return _seen[mid]
    if chat_id:
        for m in _as_list(api.get_recent_messages(chat_id, 200)):
            if isinstance(m, dict):
                _remember(m)
        return _seen.get(mid)
    return None


def _iso(ts) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts) / 1000).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def message_to_dict(
    api: OkLine, my_mid: str | None, m: dict, names: dict | None = None
) -> dict:
    _remember(m)
    ts = m.get("createdTime")
    ctype = m.get("contentType")
    try:
        ctype = int(ctype) if ctype is not None else 0
    except (TypeError, ValueError):
        pass
    d, ok = decrypt(api, m)
    text = (d.get("text") or "") if ok else UNDECRYPTABLE
    meta = m.get("contentMetadata") or {}
    sender = m.get("from")
    out: dict = {
        "id": m.get("id"),
        "from": sender,
        "from_me": bool(my_mid) and sender == my_mid,
        "type": ctype,
        "text": text,
        "has_image": ctype == 1,
        "created_ms": ts,
        "time": _iso(ts),
    }
    if names and sender in names:
        out["sender_name"] = names[sender]
    if m.get("relatedMessageId"):
        out["reply_to"] = m.get("relatedMessageId")
    if ctype == 7:
        info = sticker_info(m)
        out["text"] = "[sticker]" if not text else text
        if info:
            out["sticker"] = info
    elif ctype and ctype != 0 and not text:
        out["text"] = f"[{TYPE_LABELS.get(ctype, f'message type {ctype}')}]"
    if meta.get("FILE_NAME"):
        out["file_name"] = meta["FILE_NAME"]
    loc = d.get("location")
    if isinstance(loc, dict) and loc:
        out["location"] = {
            k: loc.get(k) for k in ("title", "address", "latitude", "longitude") if loc.get(k)
        }
    return out


def messages_to_dicts(api: OkLine, msgs: list, *, oldest_first: bool = True) -> list[dict]:
    """Convert a raw (newest-first) message list, resolving sender names once."""
    me = my_mid(api)
    names = names_or_empty()[0]
    items = [m for m in (msgs or []) if isinstance(m, dict)]
    if oldest_first:
        items = list(reversed(items))
    return [message_to_dict(api, me, m, names) for m in items]


def _as_list(res) -> list:
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        for key in ("messages", "messageBoxes", "boxes"):
            if isinstance(res.get(key), list):
                return res[key]
        by_id = res.get("messageBoxesByIds")
        if isinstance(by_id, dict):
            return list(by_id.values())
    return []


def read_messages(
    api: OkLine, chat_id: str, count: int, before_message_id: str | None = None
) -> list:
    """Raw messages, newest first. With before_message_id, the page of messages
    older than that message (for scrolling back through history)."""
    if not before_message_id:
        return _as_list(api.get_recent_messages(chat_id, count))
    anchor = lookup_message(api, before_message_id, chat_id)
    if anchor is None:
        raise ValueError(
            f"message {before_message_id} not found — pass an id returned by line_read for this chat"
        )
    delivered = anchor.get("deliveredTime") or anchor.get("createdTime")
    res = api.get_previous_messages(chat_id, str(before_message_id), int(delivered), count)
    for m in _as_list(res):
        if isinstance(m, dict):
            _remember(m)
    return [
        m for m in _as_list(res)
        if isinstance(m, dict) and str(m.get("id")) != str(before_message_id)
    ]


def sniff_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"M4A ", b"M4B "):
            return "audio/mp4"
        if brand in (b"qt  ",):
            return "video/quicktime"
        return "video/mp4"
    if data[:4] == b"%PDF":
        return "application/pdf"
    if data[:4] == b"PK\x03\x04":
        return "application/zip"
    if data[:3] == b"ID3" or data[:2] == b"\xff\xfb":
        return "audio/mpeg"
    if data[:4] == b"#!AM":  # AMR voice notes
        return "audio/amr"
    return "application/octet-stream"


_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/amr": ".amr",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
}


def _check_message_id(message_id: str) -> str:
    mid = str(message_id).strip()
    if not mid.isdigit():
        raise ValueError(f"invalid message_id {message_id!r} (LINE message ids are numeric)")
    return mid


class UnsupportedMedia(RuntimeError):
    pass


def download_media(api: OkLine, message_id: str, chat_id: str | None = None) -> tuple[bytes, str]:
    """Raw bytes + sniffed mime of a media message."""
    message_id = _check_message_id(message_id)
    m = lookup_message(api, message_id, chat_id) or {}
    meta = m.get("contentMetadata") or {}
    if meta.get("SID") == "emi" or (m.get("chunks") and meta.get("OID")):
        raise UnsupportedMedia(
            f"message {message_id} is end-to-end-encrypted (Letter Sealing) media; downloading "
            "E2EE media isn't supported by the LINE Chrome protocol this server uses. "
            "Open it in the LINE app instead."
        )
    import requests

    try:
        data = bytes(api.obs.download_object("talk", meta.get("SID") or "m", meta.get("OID") or message_id))
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", None)
        if status in (401, 403, 404) and not m:
            raise UnsupportedMedia(
                f"could not download message {message_id} (HTTP {status}). Pass chat_id so the "
                "message can be looked up — it may be E2EE media, which isn't supported."
            ) from exc
        if status in (404, 410):
            raise UnsupportedMedia(f"media for message {message_id} is gone (expired on LINE's servers)") from exc
        raise
    return data, sniff_mime(data)


def download_image(api: OkLine, message_id: str, chat_id: str | None = None) -> tuple[bytes, str]:
    """Download an image message's bytes. Returns (bytes, mime)."""
    return download_media(api, message_id, chat_id)


def download_media_to_file(
    api: OkLine,
    message_id: str,
    save_dir: str | None = None,
    file_name: str | None = None,
    chat_id: str | None = None,
) -> dict:
    """Download any media message (image/video/voice/file) to disk. Returns path info."""
    message_id = _check_message_id(message_id)
    if not file_name:
        file_name = ((lookup_message(api, message_id, chat_id) or {}).get("contentMetadata") or {}).get("FILE_NAME")
    data, mime = download_media(api, message_id, chat_id)
    directory = os.path.expanduser(save_dir) if save_dir else os.path.join(DEFAULT_SESSION_DIR, "media")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    safe_name = os.path.basename(file_name or "").strip()
    if safe_name in ("", ".", ".."):
        safe_name = f"{message_id}{_EXT.get(mime, '.bin')}"
    else:
        safe_name = f"{message_id}_{safe_name}"
    path = os.path.join(directory, safe_name)
    with open(path, "wb") as f:
        f.write(data)
    return {"path": path, "mime": mime, "bytes": len(data)}


def media_kind(path: str) -> str:
    """'image' | 'video' | 'file' — how LINE should present an outgoing file."""
    import mimetypes

    mime = mimetypes.guess_type(path)[0] or ""
    if mime in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    return "file"


# -- chats ----------------------------------------------------------------------


def find_box(api: OkLine, chat_id: str) -> dict | None:
    try:
        res = api.get_message_boxes_by_ids([chat_id])
        for b in _as_list(res):
            if isinstance(b, dict) and b.get("id") == chat_id:
                return b
    except Exception as exc:
        log.debug("getMessageBoxesByIds failed, falling back to scan: %s", exc)
    for b in _as_list(api.get_message_boxes(limit=200)):
        if isinstance(b, dict) and b.get("id") == chat_id:
            return b
    return None


_KINDS = {0: "user", 1: "room", 2: "group", 3: "square", 4: "square_chat", 6: "bot"}


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _last_delivered(box: dict) -> tuple[str | None, str | None]:
    """(message id, ISO time) of a box's last delivered message."""
    ld = box.get("lastDeliveredMessageId") or {}
    mid = ld.get("messageId") if isinstance(ld, dict) else None
    ts = ld.get("deliveredTime") if isinstance(ld, dict) else None
    if mid in (None, "-1") or _int(ts) <= 0:
        return None, None
    return str(mid), _iso(ts)


def box_to_dict(box: dict) -> dict:
    last = [m for m in (box.get("lastMessages") or []) if isinstance(m, dict)]
    last_id, last_time = _last_delivered(box)
    return {
        "id": box.get("id"),
        "name": resolve_chat_name(str(box.get("id"))),
        "kind": _KINDS.get(_int(box.get("midType"), -1), box.get("midType")),
        "unread_count": _int(box.get("unreadCount")),
        "last_message_id": (last[0].get("id") if last else last_id),
        "last_message_time": last_time,
    }


def search_messages(
    api: OkLine,
    my_mid: str | None,
    query: str,
    *,
    per_chat: int = 30,
    chat_limit: int = 20,
    max_results: int = 50,
) -> list[dict]:
    """Substring search over recent messages across the most recent chats."""
    q = query.lower()
    hits: list[dict] = []
    names = names_or_empty()[0]
    for b in _as_list(api.get_message_boxes(limit=chat_limit)):
        chat_id = b.get("id") if isinstance(b, dict) else None
        if not chat_id:
            continue
        for m in _as_list(api.get_recent_messages(chat_id, per_chat)):
            if not isinstance(m, dict):
                continue
            d = message_to_dict(api, my_mid, m, names)
            if d["text"] and d["text"] != UNDECRYPTABLE and q in d["text"].lower():
                d["chat_id"] = chat_id
                d["chat_name"] = resolve_chat_name(chat_id)
                hits.append(d)
                if len(hits) >= max_results:
                    return hits
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
    msgs = [m for m in _as_list(api.get_recent_messages(chat_id, 300)) if isinstance(m, dict)]
    idx = next((i for i, m in enumerate(msgs) if str(m.get("id")) == str(message_id)), None)
    if idx is None:
        return None
    # get_recent_messages is newest-first; convert to oldest-first for context
    ordered = list(reversed(msgs))
    pos = len(ordered) - 1 - idx
    window = ordered[max(0, pos - before) : pos + after + 1]
    names = names_or_empty()[0]
    return {
        "target_index": min(pos, before),
        "messages": [message_to_dict(api, my_mid, m, names) for m in window],
    }


def chats_to_list(api: OkLine, limit: int = 30) -> list[dict]:
    me = my_mid(api)
    names = names_or_empty()[0]
    out = []
    for b in _as_list(api.get_message_boxes(limit=limit)):
        if not isinstance(b, dict) or not b.get("id"):
            continue
        item = {
            "chat_id": b.get("id"),
            "name": resolve_chat_name(str(b.get("id"))),
            "unread": _int(b.get("unreadCount")),
        }
        last = [m for m in (b.get("lastMessages") or []) if isinstance(m, dict)]
        if not last:
            _, t = _last_delivered(b)
            if t:
                item["last_message"] = {"text": None, "from_me": None, "time": t}
        if last:
            d = message_to_dict(api, me, last[0], names)
            text = d["text"]
            item["last_message"] = {
                "text": text if len(text) <= 120 else text[:117] + "...",
                "from_me": d["from_me"],
                "time": d["time"],
            }
        out.append(item)
    return out

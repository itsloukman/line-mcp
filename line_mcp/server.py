"""MCP server exposing a personal LINE account.

Transport: stdio. Run with `line-mcp serve` (or `python -m line_mcp serve`).
Requires a prior `line-mcp login` (QR scan on your phone) — or use the
line_login_start / line_login_status tools from the MCP client.
"""

from __future__ import annotations

import base64
import collections
import functools
import os
import threading
import time

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent, ToolAnnotations

from . import client as C
from . import store as S

mcp = FastMCP(
    "line-personal",
    instructions=(
        "Unofficial bridge to the user's personal LINE account (companion device; "
        "the phone stays logged in). Chats are addressed by chat_id from line_chats "
        "(a contact's mid doubles as the chat_id of the 1:1 chat). "
        "Messages are E2EE-decrypted when possible. Sending, reacting, unsending and "
        "marking read act as the user and are visible to others: only do them when "
        "the user explicitly approved the exact action. Keep request volumes human-like. "
        "If a tool says the session expired, use line_login_start."
    ),
)

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
LOCAL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
SEND = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)
MARK = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)

# Outgoing actions per minute (sends, reactions, unsends). A runaway agent loop
# spamming as the user is the fastest way to get an unofficial client banned.
MAX_SENDS_PER_MIN = int(os.environ.get("LINE_MAX_SENDS_PER_MIN", "10"))
_sends: collections.deque = collections.deque()
_sends_lock = threading.Lock()


def _send_guard() -> None:
    now = time.monotonic()
    with _sends_lock:
        while _sends and now - _sends[0] > 60:
            _sends.popleft()
        if len(_sends) >= MAX_SENDS_PER_MIN:
            wait = int(60 - (now - _sends[0])) + 1
            raise RuntimeError(
                f"Safety limit: at most {MAX_SENDS_PER_MIN} outgoing actions per minute "
                f"(LINE_MAX_SENDS_PER_MIN). Try again in ~{wait}s."
            )
        _sends.append(now)


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
                "out from the phone — log in again with line_login_start (or `line-mcp login`)."
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


def tool(annotations: ToolAnnotations, *, guard: bool = False):
    """Register a sync function as an MCP tool that runs in a worker thread, so
    a slow call (search, history sync, waiting) never blocks the others."""

    def deco(fn):
        safe = _friendly_errors(fn)

        @functools.wraps(fn)
        async def runner(*args, **kwargs):
            if guard:
                _send_guard()
            return await anyio.to_thread.run_sync(functools.partial(safe, *args, **kwargs))

        runner.sync = safe  # direct (blocking) access for scripts/tests
        mcp.tool(annotations=annotations)(runner)
        return runner

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


# -- account ----------------------------------------------------------------------


@tool(READ)
def line_whoami() -> dict:
    """Return the logged-in LINE profile (mid, display name), whether E2EE decryption is available, and the local store / live-sync status."""
    api = C.get_api()
    p = api.get_profile()
    out = {
        "mid": p.get("mid"),
        "displayName": p.get("displayName"),
        "e2ee_ready": bool(api.e2ee and api.e2ee.is_ready()),
        **S.sync_status(),
    }
    st = S.get_store()
    if st:
        out["stored"] = st.stats()
    return out


_login: "object | None" = None


@tool(LOCAL_WRITE)
def line_login_start():
    """Start a LINE QR login (e.g. when the session expired) and return the QR code image. The user scans it with LINE on their phone (Home → QR scanner) and approves; then call line_login_status to get the PIN they must type on the phone, and again until state is 'done'. QR codes refresh automatically for 10 minutes."""
    from .login import LoginFlow

    global _login
    if _login is not None and _login.running() and _login.state != "done":
        flow = _login
    else:
        flow = LoginFlow(timeout=600)
        flow.start()
        _login = flow
    deadline = time.monotonic() + 30
    while not flow.qr_png and flow.state not in ("failed", "done") and time.monotonic() < deadline:
        time.sleep(0.3)
    if not flow.qr_png:
        return [TextContent(type="text", text=f"Login could not start: {flow.status()}")]
    return [
        TextContent(
            type="text",
            text="Scan this QR code with LINE on your phone (Home → QR scanner) and approve. "
            "Then call line_login_status for the PIN. It expires in ~2–3 minutes; "
            "call line_login_start again for the current one.",
        ),
        ImageContent(type="image", data=base64.b64encode(flow.qr_png).decode("ascii"), mimeType="image/png"),
    ]


@tool(READ)
def line_login_status() -> dict:
    """Progress of the login started with line_login_start: waiting_for_scan, waiting_for_pin (includes the PIN to type on the phone), done, or failed."""
    if _login is None:
        return {"state": "not_started", "hint": "call line_login_start"}
    if _login.state == "done":
        S.start_live_sync()
    return _login.status()


# -- chats & messages --------------------------------------------------------------


@tool(READ)
def line_chats(limit: int = 30) -> list[dict]:
    """List recent chats (newest first): chat_id, resolved name, unread count and a last-message preview. Use chat_id with line_read/line_send."""
    return C.chats_to_list(C.get_api(), limit=_clamp(limit, 1, 100))


@tool(READ)
def line_read(chat_id: str, count: int = 20, before_message_id: str = "") -> list[dict]:
    """Read messages from a chat (E2EE-decrypted), oldest first, with sender names. To scroll back, pass the id of the oldest message you have as before_message_id to get the page before it. Image messages have has_image=true — fetch them with line_get_image(message_id, chat_id)."""
    api = C.get_api()
    msgs = C.read_messages(api, chat_id, _clamp(count, 1, 200), before_message_id or None)
    return C.messages_to_dicts(api, msgs)


@tool(READ)
def line_get_chat(chat_id: str) -> dict:
    """Get info about a chat: id, name, kind, unread count, last message id/time."""
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
def line_message_context(chat_id: str, message_id: str, before: int = 5, after: int = 5) -> dict:
    """Get the messages surrounding a specific message (for context). Reads up to 300 recent messages."""
    api = C.get_api()
    ctx = C.message_context(
        api, C.my_mid(api), chat_id, message_id,
        before=_clamp(before, 0, 50), after=_clamp(after, 0, 50),
    )
    if ctx is None:
        return {"error": "message not found in the last 300 messages of this chat"}
    return ctx


_warmed = False


@tool(READ)
def line_search_messages(query: str, chat_id: str = "", limit: int = 50) -> list:
    """Case-insensitive substring search (works for Thai/Japanese/…) over every message stored locally — everything read, synced (line_sync_history) or received live since the server started — newest first, with chat_id and chat_name. The first search per session also scans the 20 most recent chats. Pass chat_id to search one chat."""
    global _warmed
    if not query.strip():
        raise ValueError("query must not be empty")
    api = C.get_api()
    limit = _clamp(limit, 1, 200)
    store = S.get_store()
    if store is None:  # store disabled: live scan of recent chats only
        return C.search_messages(api, C.my_mid(api), query, max_results=limit)
    if not _warmed:
        C.ingest_recent(api, per_chat=30, chat_limit=20)
        _warmed = True
    if chat_id:
        C.messages_to_dicts(api, C.read_messages(api, chat_id, 50))  # freshen that chat
    hits = store.search(query, chat_id=chat_id or None, limit=limit)
    for h in hits:
        h["chat_name"] = C.resolve_chat_name(h["chat_id"])
    return hits


@tool(READ)
def line_sync_history(chat_id: str, max_messages: int = 300) -> dict:
    """Page back through a chat's history (up to max_messages, max 2000) and store it locally so line_search_messages covers it. Returns how many messages were stored and how far back they go."""
    api = C.get_api()
    if S.get_store() is None:
        raise RuntimeError("the local store is disabled (LINE_MCP_STORE=off)")
    target = _clamp(max_messages, 1, 2000)
    page = C.messages_to_dicts(api, C.read_messages(api, chat_id, min(100, target)))
    total = len(page)
    oldest = page[0] if page else None
    while oldest and total < target:
        older = C.messages_to_dicts(api, C.read_messages(api, chat_id, min(100, target - total), oldest["id"]))
        if not older:
            break
        total += len(older)
        oldest = older[0]
    return {
        "chat_id": chat_id,
        "chat_name": C.resolve_chat_name(chat_id),
        "stored": total,
        "oldest_time": oldest["time"] if oldest else None,
        "reached_beginning": total < target,
    }


@tool(READ)
def line_new_messages(since_minutes: int = 60, chat_id: str = "", include_mine: bool = False, limit: int = 100) -> list:
    """Messages that arrived in the last since_minutes (from the live sync + local store), oldest first, across all chats or one chat_id. By default only messages from other people."""
    store = S.get_store()
    if store is None:
        raise RuntimeError("the local store is disabled (LINE_MCP_STORE=off)")
    S.start_live_sync()
    since_ms = int((time.time() - _clamp(since_minutes, 1, 60 * 24 * 30) * 60) * 1000)
    hits = store.since(since_ms, chat_id=chat_id or None, include_mine=include_mine, limit=_clamp(limit, 1, 500))
    for h in hits:
        h["chat_name"] = C.resolve_chat_name(h["chat_id"])
    return hits


@tool(READ)
def line_wait_for_message(timeout_seconds: int = 60, chat_id: str = "") -> list:
    """Wait (up to timeout_seconds, max 600) for the next incoming message — in any chat, or only chat_id — and return it. Returns [] on timeout. Uses the live operation stream."""
    store = S.get_store()
    if store is None:
        raise RuntimeError("the local store is disabled (LINE_MCP_STORE=off)")
    S.start_live_sync()
    # Baseline on the newest stored message (server timestamps), not the local
    # clock, so clock skew can't hide a new message.
    after_ms = store.max_created_ms() or int(time.time() * 1000)
    hits = store.wait_new(after_ms, chat_id=chat_id or None, timeout=_clamp(timeout_seconds, 1, 600))
    for h in hits:
        h["chat_name"] = C.resolve_chat_name(h["chat_id"])
    return hits


@tool(READ)
def line_read_receipts(chat_id: str, message_id: str = "") -> dict:
    """Who has read a chat and up to which message/time. With message_id, also says whether each person has read that message (read_by)."""
    return C.read_receipts(C.get_api(), chat_id, message_id or None)


# -- media ---------------------------------------------------------------------------


@tool(READ)
def line_get_image(message_id: str, chat_id: str = ""):
    """Download an image message and return the image itself (end-to-end-encrypted images are decrypted). Get message_id and chat_id from line_read (messages with has_image=true)."""
    api = C.get_api()
    data, mime = C.download_image(api, message_id, chat_id or None)
    if not mime.startswith("image/"):
        raise ValueError(
            f"message {message_id} did not download as an image ({mime}, {len(data)} bytes); "
            "try line_download_media."
        )
    return ImageContent(type="image", data=base64.b64encode(data).decode("ascii"), mimeType=mime)


@tool(LOCAL_WRITE)
def line_download_media(message_id: str, chat_id: str = "", save_dir: str = "", file_name: str = "") -> dict:
    """Download a media message (image/video/voice/file; E2EE media is decrypted) to disk and return the local path, mime type and size. Pass the chat_id it came from so its metadata can be found."""
    api = C.get_api()
    return C.download_media_to_file(api, message_id, save_dir or None, file_name or None, chat_id or None)


# -- contacts & groups -----------------------------------------------------------


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


@tool(READ)
def line_group_members(chat_id: str) -> dict:
    """List a group's members (mid + name, including people who aren't your friends) and pending invitees."""
    api = C.get_api()
    res = api.get_chats([chat_id], with_members=True, with_invitees=True)
    chats = res.get("chats") if isinstance(res, dict) else None
    if not chats:
        return {"error": "group not found"}
    extra = (chats[0].get("extra") or {}).get("groupExtra") or {}
    members = list((extra.get("memberMids") or {}).keys())
    invitees = list((extra.get("inviteeMids") or {}).keys())
    names = C.names_for(api, members + invitees)
    me = C.my_mid(api)
    if me in members and not names.get(me):
        try:
            names[me] = api.get_profile().get("displayName")
        except Exception:
            pass
    return {
        "chat_id": chat_id,
        "name": chats[0].get("chatName"),
        "members": [{"mid": m, "name": names.get(m), "is_me": m == me} for m in members],
        "invitees": [{"mid": m, "name": names.get(m)} for m in invitees],
    }


# -- acting as the user (sends) ---------------------------------------------------


@tool(SEND, guard=True)
def line_send(chat_id: str, text: str, reply_to_message_id: str = "") -> dict:
    """Send a text message to a chat as the user (optionally as a reply to a message id). Only call with the user's explicit approval of the exact text."""
    api = C.get_api()
    if reply_to_message_id:
        return _sent(api.reply_text(chat_id, text, str(reply_to_message_id)))
    return _sent(api.send_text(chat_id, text))


@tool(SEND, guard=True)
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


@tool(SEND, guard=True)
def line_send_voice(chat_id: str, audio_path: str, duration_ms: int = 0) -> dict:
    """Send an audio file (m4a/aac recommended) as a LINE voice message as the user. Pass duration_ms so LINE shows the right length. audio_path is on the machine running the server. Only call with the user's explicit approval."""
    api = C.get_api()
    return _sent(api.send_audio(chat_id, _local_file(audio_path), duration_ms=max(0, int(duration_ms))))


@tool(SEND, guard=True)
def line_send_sticker(chat_id: str, package_id: str, sticker_id: str) -> dict:
    """Send a LINE sticker to a chat as the user. The user must own the sticker pack; package_id/sticker_id come from the 'sticker' field in line_read (e.g. a sticker the user sent before). Only call with the user's explicit approval."""
    api = C.get_api()
    return _sent(api.send_sticker(chat_id, str(package_id), str(sticker_id)))


@tool(SEND, guard=True)
def line_react(message_id: str, reaction: str = "nice") -> dict:
    """React to a message as the user. reaction: nice (👍), love (❤️), fun (😆), amazing (😮), sad (😢), omg (😱). Visible to the chat. Only call with the user's explicit approval."""
    key = reaction.strip().lower()
    if key not in C.REACTIONS:
        raise ValueError(f"reaction must be one of {', '.join(C.REACTIONS)}")
    C.get_api().react(str(message_id), C.REACTIONS[key])
    return {"reacted": True, "message_id": str(message_id), "reaction": key}


@tool(SEND, guard=True)
def line_unreact(message_id: str) -> dict:
    """Remove your reaction from a message. Only call with the user's explicit approval."""
    C.get_api().cancel_reaction(str(message_id))
    return {"unreacted": True, "message_id": str(message_id)}


@tool(SEND, guard=True)
def line_unsend(message_id: str) -> dict:
    """Unsend (recall) a message you sent, for everyone in the chat. Only works on your own recent messages. Only call with the user's explicit approval."""
    api = C.get_api()
    api.unsend_message(str(message_id))
    st = S.get_store()
    if st:
        st.mark_unsent(str(message_id))
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

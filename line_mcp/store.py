"""Local message store (SQLite) + real-time sync from LINE's operation stream.

Every message the server sees — from line_read / search / history sync, and
live from the SSE operation stream — is kept in ``~/.line-mcp/messages.db``
(mode 600). That gives full-history search, "what's new" queries and
wait-for-message without re-fetching from LINE.

The DB holds decrypted message text: it is as sensitive as the session file.
Disable with ``LINE_MCP_STORE=off``.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sqlite3
import stat
import threading
import time

from . import client as C

log = logging.getLogger("line_mcp.store")

# okline.enums.OpType
OP_SEND_MESSAGE = 25
OP_RECEIVE_MESSAGE = 26
OP_DESTROY_MESSAGE = 64
OP_NOTIFIED_DESTROY_MESSAGE = 65


def enabled() -> bool:
    return os.environ.get("LINE_MCP_STORE", "on").strip().lower() not in ("0", "off", "false", "no")


def db_path() -> str:
    return os.environ.get("LINE_MCP_DB") or os.path.join(
        os.path.dirname(C.session_path()), "messages.db"
    )


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    chat_id TEXT,
    sender TEXT,
    from_me INTEGER,
    type INTEGER,
    text TEXT,
    text_lc TEXT,
    created_ms INTEGER,
    unsent INTEGER DEFAULT 0,
    json TEXT
);
CREATE INDEX IF NOT EXISTS messages_chat_time ON messages (chat_id, created_ms);
CREATE INDEX IF NOT EXISTS messages_time ON messages (created_ms);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path: str) -> None:
        directory = os.path.dirname(path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        if not os.path.exists(path):
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        self.path = path
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        self._new = threading.Condition()
        self.last_live_ms = 0  # created_ms of the newest message seen on the live stream

    # -- writes --------------------------------------------------------------
    def add(self, d: dict, chat_id: str | None, *, live: bool = False) -> None:
        mid = str(d.get("id") or "")
        if not mid or not chat_id:
            return
        text = d.get("text") or ""
        row = (
            mid, chat_id, d.get("from"), int(bool(d.get("from_me"))), C._int(d.get("type")),
            text, text.casefold(), C._int(d.get("created_ms")), json.dumps(d, ensure_ascii=False),
        )
        with self._lock:
            self._db.execute(
                """INSERT INTO messages (id, chat_id, sender, from_me, type, text, text_lc, created_ms, json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET text=excluded.text, text_lc=excluded.text_lc,
                     json=excluded.json, chat_id=excluded.chat_id""",
                row,
            )
            self._db.commit()
        if live:
            with self._new:
                self.last_live_ms = max(self.last_live_ms, C._int(d.get("created_ms")))
                self._new.notify_all()

    def mark_unsent(self, message_id: str) -> None:
        with self._lock:
            self._db.execute("UPDATE messages SET unsent=1 WHERE id=?", (str(message_id),))
            self._db.commit()

    # -- reads ---------------------------------------------------------------
    def _rows(self, sql: str, args: tuple) -> list[dict]:
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        out = []
        for chat_id, unsent, js in rows:
            d = json.loads(js)
            d["chat_id"] = chat_id
            if unsent:
                d["unsent"] = True
            out.append(d)
        return out

    def search(self, query: str, *, chat_id: str | None = None, limit: int = 50) -> list[dict]:
        """Case-insensitive substring search (works for Thai/Japanese too), newest first."""
        sql = "SELECT chat_id, unsent, json FROM messages WHERE instr(text_lc, ?) > 0"
        args: list = [query.casefold()]
        if chat_id:
            sql += " AND chat_id = ?"
            args.append(chat_id)
        sql += " ORDER BY created_ms DESC LIMIT ?"
        args.append(int(limit))
        return self._rows(sql, tuple(args))

    def since(
        self, since_ms: int, *, chat_id: str | None = None, include_mine: bool = False, limit: int = 100
    ) -> list[dict]:
        """Messages newer than since_ms, oldest first."""
        sql = "SELECT chat_id, unsent, json FROM messages WHERE created_ms > ?"
        args: list = [int(since_ms)]
        if chat_id:
            sql += " AND chat_id = ?"
            args.append(chat_id)
        if not include_mine:
            sql += " AND from_me = 0"
        sql += " ORDER BY created_ms DESC LIMIT ?"
        args.append(int(limit))
        return list(reversed(self._rows(sql, tuple(args))))

    def wait_new(self, after_ms: int, *, chat_id: str | None, timeout: float) -> list[dict]:
        """Block until an incoming live message newer than after_ms arrives."""
        deadline = time.monotonic() + timeout
        while True:
            found = self.since(after_ms, chat_id=chat_id)
            if found:
                return found
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            with self._new:
                self._new.wait(timeout=min(remaining, 5.0))

    def known_ids(self, ids) -> set[str]:
        ids = [str(i) for i in ids if i]
        if not ids:
            return set()
        with self._lock:
            rows = self._db.execute(
                f"SELECT id FROM messages WHERE id IN ({','.join('?' * len(ids))})", ids
            ).fetchall()
        return {r[0] for r in rows}

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            self._db.commit()

    def max_created_ms(self) -> int:
        with self._lock:
            (v,) = self._db.execute("SELECT MAX(created_ms) FROM messages").fetchone()
        return int(v or 0)

    def stats(self) -> dict:
        with self._lock:
            n, chats, oldest = self._db.execute(
                "SELECT COUNT(*), COUNT(DISTINCT chat_id), MIN(created_ms) FROM messages"
            ).fetchone()
        return {"messages": n, "chats": chats, "oldest": C._iso(oldest) if oldest else None}


_store: Store | None = None
_store_lock = threading.Lock()


def get_store() -> Store | None:
    """The shared store, or None when disabled. Hooks itself into the client
    so every converted message is persisted."""
    global _store
    if not enabled():
        return None
    with _store_lock:
        if _store is None:
            _store = Store(db_path())
            C.message_hooks.append(lambda d, chat_id: _store.add(d, chat_id))
        return _store


# -- real-time sync -------------------------------------------------------------


def _sse_events(resp):
    """Parse a text/event-stream response into (event, id, data) tuples as
    bytes arrive (no read-ahead buffering, so events aren't delayed)."""
    buf = b""
    for chunk in resp.iter_content(chunk_size=None):
        buf += chunk
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            event, ev_id, data = "message", None, []
            for line in block.decode("utf-8", "replace").splitlines():
                if line.startswith(":"):
                    continue
                key, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if key == "event":
                    event = value
                elif key == "id":
                    ev_id = value
                elif key == "data":
                    data.append(value)
            yield event, ev_id, "\n".join(data)


class LiveSync(threading.Thread):
    """Consumes the SSE operation stream and stores new / unsent messages.

    Like the Chrome extension, the stream is opened with ``localRev`` (the last
    operation revision we processed) — without it the gateway only sends
    pings. The revision is persisted, so after a restart the server replays
    what arrived while it was down.
    """

    def __init__(self) -> None:
        super().__init__(name="line-mcp-sync", daemon=True)
        self.connected = False
        self.last_event = 0.0
        self.error: str | None = None
        self.ops_seen = 0
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        import requests
        from okline import endpoints as ep
        from okline.exceptions import LineAuthError
        from okline.operations import Operation

        backoff = 5.0
        while not self._stop.is_set():
            try:
                api = C.get_api()
                store = get_store()
                if store is None:
                    return
                rev = store.get_meta("localRev") or str(api.get_last_op_revision())
                store.set_meta("localRev", rev)
                # A private HTTP session: the stream holds its connection open.
                t = copy.copy(api.transport)
                t.session = requests.Session()
                t.recorder = None
                resp = t.get(
                    "/" + ep.SPECIAL_ENDPOINTS["operation.receive"],
                    stream=True,
                    params={"localRev": rev, "version": "3.7.2", "language": "en_US"},
                    extra_headers={"accept": "text/event-stream", "cache-control": "no-cache"},
                    timeout=(30, 120),  # read timeout > the ~20s ping interval
                )
                if resp.status_code in (401, 403):
                    raise LineAuthError(f"operation stream HTTP {resp.status_code}")
                if resp.status_code != 200:
                    raise RuntimeError(f"operation stream HTTP {resp.status_code}")
                self.connected, self.error = True, None
                backoff = 5.0
                try:
                    for event, _id, data in _sse_events(resp):
                        if self._stop.is_set():
                            return
                        self.last_event = time.time()
                        if event == "message" and data:
                            op = json.loads(data)
                            ops = op.get("operations") if isinstance(op, dict) and "operations" in op else [op]
                            for o in ops if isinstance(ops, list) else []:
                                if isinstance(o, dict):
                                    self.ops_seen += 1
                                    self._handle(api, store, Operation.from_dict(o))
                                    if o.get("revision"):
                                        store.set_meta("localRev", str(o["revision"]))
                        elif event in ("fullSync", "partialFullSync") and data:
                            nxt = (json.loads(data) or {}).get("nextRevision")
                            if nxt:
                                store.set_meta("localRev", str(nxt))
                        elif event == "reconnect":
                            break
                finally:
                    resp.close()
            except LineAuthError as exc:
                self.error = f"session rejected: {exc}"
                C.reset_api()
                backoff = 60.0
            except Exception as exc:  # network drop, gateway reconnect, ...
                self.error = str(exc)[:200]
                log.info("live sync reconnecting: %s", exc)
            self.connected = False
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 300.0)

    def _handle(self, api, store: Store, op) -> None:
        try:
            if op.type in (OP_SEND_MESSAGE, OP_RECEIVE_MESSAGE) and isinstance(op.message, dict):
                me = C.my_mid(api)
                m = op.message
                names = C.names_for(api, {m.get("from")})
                d = C.message_to_dict(api, me, m, names, notify=False)
                store.add(d, C.chat_of(m, me), live=True)
            elif op.type in (OP_DESTROY_MESSAGE, OP_NOTIFIED_DESTROY_MESSAGE):
                for p in (op.param2, op.param3):
                    if p and str(p).isdigit() and len(str(p)) > 6:
                        store.mark_unsent(str(p))
        except Exception as exc:  # one bad op must not kill the stream
            log.warning("could not process operation %s: %s", op.type, exc)


POLL_SECONDS = int(os.environ.get("LINE_MCP_POLL_SECONDS", "60"))


class BoxPoller(threading.Thread):
    """Backstop for the operation stream.

    LINE doesn't deliver every message to a companion device as an operation
    (e.g. messages you send from your phone). Once a minute, one
    getMessageBoxes call compares each chat's last delivered message with the
    store and fetches the chats that changed.
    """

    def __init__(self, interval: int = POLL_SECONDS) -> None:
        super().__init__(name="line-mcp-poll", daemon=True)
        self.interval = max(15, interval)
        self.polls = 0
        self.fetched = 0
        self.error: str | None = None
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def poll_once(self) -> int:
        api = C.get_api()
        store = get_store()
        if store is None:
            return 0
        boxes = [b for b in C._as_list(api.get_message_boxes(limit=30)) if isinstance(b, dict) and b.get("id")]
        me = C.my_mid(api)
        if me and all(b["id"] != me for b in boxes):
            # Keep Memo (your own chat) is never in the active chat list.
            boxes += [b for b in C._as_list(api.get_message_boxes_by_ids([me])) if isinstance(b, dict) and b.get("id")]
        latest = {}
        for b in boxes:
            last_id, _ = C._last_delivered(b)
            last = [m for m in (b.get("lastMessages") or []) if isinstance(m, dict)]
            latest[b["id"]] = str(last[0].get("id")) if last else last_id
        known = store.known_ids(v for v in latest.values() if v)
        new = 0
        for chat_id, last_id in latest.items():
            if not last_id or last_id in known:
                continue
            raw = [m for m in C.read_messages(api, chat_id, 20) if isinstance(m, dict)]
            fresh = [m for m in raw if str(m.get("id")) not in store.known_ids(str(x.get("id")) for x in raw)]
            names = C.names_for(api, {m.get("from") for m in fresh})
            for m in reversed(fresh):  # oldest first
                store.add(C.message_to_dict(api, me, m, names, notify=False), C.chat_of(m, me), live=True)
                new += 1
        self.polls += 1
        self.fetched += new
        return new

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.poll_once()
                self.error = None
            except Exception as exc:
                self.error = str(exc)[:200]
                log.info("box poll failed: %s", exc)


_sync: LiveSync | None = None
_poller: BoxPoller | None = None


def start_live_sync() -> LiveSync | None:
    global _sync, _poller
    if not enabled() or not os.path.exists(C.session_path()):
        return None
    if _sync is None or not _sync.is_alive():
        _sync = LiveSync()
        _sync.start()
    if _poller is None or not _poller.is_alive():
        _poller = BoxPoller()
        _poller.start()
    return _sync


def sync_status() -> dict:
    s = _sync
    return {
        "store_enabled": enabled(),
        "live_sync_running": bool(s and s.is_alive()),
        "live_connected": bool(s and s.connected),
        "last_event": C._iso(int(s.last_event * 1000)) if s and s.last_event else None,
        "last_error": s.error if s else None,
        "ops_seen": s.ops_seen if s else 0,
        "poll_every_s": _poller.interval if _poller else None,
        "polled_new": _poller.fetched if _poller else 0,
    }

"""Offline tests for the local store and server-level behaviour."""

from __future__ import annotations

import asyncio
import os
import stat
import threading
import time

import pytest

from line_mcp import store as S


@pytest.fixture
def store(tmp_path):
    return S.Store(str(tmp_path / "db" / "messages.db"))


def m(i, text, chat="uA", from_me=False, t=None):
    return {"id": str(i), "from": "uME" if from_me else chat, "from_me": from_me, "type": 0,
            "text": text, "created_ms": t or 1_790_000_000_000 + i}


def test_store_is_private(store):
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


def test_search_thai_and_case(store):
    store.add(m(1, "สวัสดีครับ ยินดีต้อนรับ"), "uA")
    store.add(m(2, "Hello WORLD"), "uB")
    store.add(m(3, "unrelated"), "uB")
    assert [d["id"] for d in store.search("ยินดี")] == ["1"]  # no word boundaries needed
    assert [d["id"] for d in store.search("world")] == ["2"]
    assert store.search("world", chat_id="uA") == []


def test_upsert_and_unsent(store):
    store.add(m(1, "v1"), "uA")
    store.add(m(1, "v2"), "uA")
    store.mark_unsent("1")
    [d] = store.search("v2")
    assert d["unsent"] is True and store.stats()["messages"] == 1


def test_since_excludes_mine_by_default(store):
    store.add(m(1, "theirs"), "uA")
    store.add(m(2, "mine", from_me=True), "uA")
    assert [d["id"] for d in store.since(0)] == ["1"]
    assert [d["id"] for d in store.since(0, include_mine=True)] == ["1", "2"]


def test_wait_new_wakes_on_live_message(store):
    base = store.max_created_ms()

    def later():
        time.sleep(0.3)
        store.add(m(10, "ping"), "uA", live=True)

    threading.Thread(target=later).start()
    t0 = time.monotonic()
    got = store.wait_new(base, chat_id=None, timeout=5)
    assert [d["id"] for d in got] == ["10"] and time.monotonic() - t0 < 3
    assert store.wait_new(store.max_created_ms(), chat_id=None, timeout=0.2) == []


def test_send_guard(monkeypatch):
    from line_mcp import server

    monkeypatch.setattr(server, "MAX_SENDS_PER_MIN", 2)
    server._sends.clear()
    server._send_guard(); server._send_guard()
    with pytest.raises(RuntimeError, match="Safety limit"):
        server._send_guard()
    server._sends.clear()


def test_tools_run_off_the_event_loop(monkeypatch):
    """A slow tool must not block others (tools run in worker threads)."""
    from line_mcp import server

    @server.tool(server.READ)
    def _slow_probe() -> dict:
        time.sleep(0.5)
        return {"ok": True}

    async def main():
        t0 = time.monotonic()
        await asyncio.gather(*[_slow_probe() for _ in range(4)])
        return time.monotonic() - t0

    assert asyncio.run(main()) < 1.5  # 4 × 0.5s in parallel, not 2s serial


def test_all_tools_registered_with_annotations():
    from line_mcp import server

    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    for expected in ("line_react", "line_read_receipts", "line_group_members", "line_new_messages",
                     "line_wait_for_message", "line_sync_history", "line_login_start", "line_login_status"):
        assert expected in names
    sends = {t.name for t in tools if t.annotations and t.annotations.readOnlyHint is False}
    assert {"line_send", "line_react", "line_unsend", "line_send_file"} <= sends


def test_sse_parser_handles_split_chunks():
    class Resp:
        def iter_content(self, chunk_size=None):
            yield b"event: ping\ndata: null\n\nid: 7\nda"
            yield b'ta: {"revision":"7","type":26}\n\n: comment\n\n'

    evs = list(S._sse_events(Resp()))
    assert evs[0] == ("ping", None, "null")
    assert evs[1] == ("message", "7", '{"revision":"7","type":26}')


def test_live_handler_stores_received_and_unsent(store, monkeypatch):
    from types import SimpleNamespace
    from line_mcp import client as C

    api = SimpleNamespace(tokens=SimpleNamespace(mid="uME"), e2ee=None, get_contacts=lambda mids: {})
    monkeypatch.setattr(C, "names_or_empty", lambda: ({"uA": "Alice"}, {}))
    sync = S.LiveSync()
    msg = {"id": "633036244812562999", "from": "uA", "to": "uME", "toType": 0, "contentType": 0,
           "text": "hi there", "createdTime": "1790150000000"}
    sync._handle(api, store, SimpleNamespace(type=S.OP_RECEIVE_MESSAGE, message=msg, param1=None, param2=None, param3=None))
    [d] = store.since(0)
    assert d["chat_id"] == "uA" and d["text"] == "hi there" and d["sender_name"] == "Alice"
    sync._handle(api, store, SimpleNamespace(type=S.OP_NOTIFIED_DESTROY_MESSAGE, message=None,
                                             param1="uA", param2="633036244812562999", param3="0"))
    assert store.since(0)[0]["unsent"] is True


def test_expired_session_gives_login_hint(monkeypatch):
    from okline.exceptions import LineAuthError
    from line_mcp import client as C, server

    resets = []
    monkeypatch.setattr(C, "reset_api", lambda: resets.append(1))

    @server._friendly_errors
    def boom():
        raise LineAuthError("expired", code=8)

    with pytest.raises(RuntimeError, match="line_login_start"):
        boom()
    assert resets == [1]

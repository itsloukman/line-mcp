"""Offline tests for line_mcp.client (a fake OkLine, no network / Node)."""

from __future__ import annotations

import os
import stat
from types import SimpleNamespace

import pytest

from line_mcp import client as C


class FakeE2EE:
    def __init__(self, ready=True):
        self.ready = ready

    def is_ready(self):
        return self.ready


class FakeApi:
    def __init__(self, *, ready=True, msgs=None):
        self.e2ee = FakeE2EE(ready)
        self.tokens = SimpleNamespace(mid="uME")
        self.msgs = msgs or []
        self.pubkey_calls = 0

    # e2ee
    def get_e2ee_public_key(self, mid, key_version=1, key_id=0):
        self.pubkey_calls += 1
        return {"keyData": f"pk-{mid}-{key_id}"}

    def decrypt_message(self, m):
        self.get_e2ee_public_key(m["from"], 1, 7)  # what okline does per message
        return {**m, "text": "secret hello", "_decrypted": True}

    # names
    def get_all_contact_ids(self):
        return ["uA", "uB"]

    def get_contacts(self, mids):
        return {"contacts": {
            "uA": {"contact": {"displayName": "Alice", "displayNameOverridden": "Ali (work)"}},
            "uB": {"contact": {"displayName": "Bob"}},
        }}

    def get_all_chat_mids(self):
        return {"memberChatMids": ["cG1", "cG2"], "invitedChatMids": ["cINV"]}

    def get_chats(self, mids, with_members=True, with_invitees=True):
        assert mids == ["cG1", "cG2"]
        return {"chats": [{"chatMid": "cG1", "chatName": "Family"}, {"chatMid": "cG2", "chatName": ""}]}

    # messages
    def get_recent_messages(self, chat_id, count):
        return self.msgs[:count]

    def get_message_boxes(self, limit=100):
        return {"messageBoxes": [{"id": "uA", "unreadCount": 2, "lastMessages": self.msgs[:1]}]}


@pytest.fixture(autouse=True)
def fake_names(monkeypatch):
    api = FakeApi()
    monkeypatch.setattr(C, "get_api", lambda: api)
    C._names.clear()
    C._seen.clear()
    yield
    C._names.clear()
    C._seen.clear()


def msg(i, **kw):
    base = {"id": str(i), "from": "uA", "contentType": 0, "text": f"m{i}", "createdTime": 1_700_000_000_000 + i}
    base.update(kw)
    return base


def test_type_labels_match_okline_enum():
    from okline.enums import ContentType

    assert C.TYPE_LABELS[int(ContentType.CALL)] == "call"
    assert C.TYPE_LABELS[int(ContentType.LOCATION)] == "location"
    assert C.TYPE_LABELS[int(ContentType.AUDIO)] == "voice message"
    assert C.TYPE_LABELS[int(ContentType.FILE)] == "file"


def test_message_to_dict_basic_and_names():
    api = FakeApi()
    d = C.message_to_dict(api, "uME", msg(1, relatedMessageId="99"), {"uA": "Alice"})
    assert d["text"] == "m1" and d["sender_name"] == "Alice" and d["reply_to"] == "99"
    assert d["from_me"] is False and d["time"].startswith("2023-11-")


def test_message_to_dict_media_sticker_file_call():
    api = FakeApi()
    assert C.message_to_dict(api, None, msg(1, contentType=1, text=""))["text"] == "[image]"
    assert C.message_to_dict(api, None, msg(1, contentType=6, text=""))["text"] == "[call]"
    st = C.message_to_dict(api, None, msg(1, contentType=7, text="", contentMetadata={"STKPKGID": "1", "STKID": "2"}))
    assert st["text"] == "[sticker]" and st["sticker"]["sticker_id"] == "2"
    f = C.message_to_dict(api, None, msg(1, contentType=14, text="", contentMetadata={"FILE_NAME": "a.pdf"}))
    assert f["text"] == "[file]" and f["file_name"] == "a.pdf"


def test_encrypted_message_decrypts_or_is_labelled():
    sealed = msg(1, text=None, chunks=["a", "b", "c", "d", "e"])
    assert C.message_to_dict(FakeApi(), None, sealed)["text"] == "secret hello"
    assert C.message_to_dict(FakeApi(ready=False), None, sealed)["text"] == C.UNDECRYPTABLE


def test_public_key_cache_dedupes_per_key_id():
    api = FakeApi()
    C._cache_public_keys(api)
    for _ in range(5):
        api.get_e2ee_public_key("uA", 1, 7)
    api.get_e2ee_public_key("uA", 1, 0)  # "latest" is never cached
    api.get_e2ee_public_key("uA", 1, 0)
    assert api.pubkey_calls == 3  # 1 for key 7 + 2 for "latest"


def test_groups_use_real_okline_api():
    assert C._unwrap_groups(FakeApi()) == {"cG1": "Family", "cG2": "cG2"}


def test_contacts_prefer_nickname():
    assert C._unwrap_contacts(FakeApi()) == {"uA": "Ali (work)", "uB": "Bob"}


def test_message_context_window():
    api = FakeApi(msgs=[msg(i) for i in range(10, 0, -1)])  # newest first
    ctx = C.message_context(api, None, "uA", "5", before=2, after=1)
    assert [m["id"] for m in ctx["messages"]] == ["3", "4", "5", "6"]
    assert ctx["target_index"] == 2
    assert C.message_context(api, None, "uA", "999") is None


def test_messages_to_dicts_oldest_first():
    api = FakeApi()
    out = C.messages_to_dicts(api, [msg(3), msg(2), "junk", msg(1)])
    assert [m["id"] for m in out] == ["1", "2", "3"]
    assert out[0]["sender_name"] == "Ali (work)"


def test_search_skips_undecryptable_and_limits():
    api = FakeApi(msgs=[msg(i, text="hello there") for i in range(5)])
    hits = C.search_messages(api, None, "HELLO", max_results=3)
    assert len(hits) == 3 and hits[0]["chat_id"] == "uA" and hits[0]["chat_name"] == "Ali (work)"


def test_chats_to_list_has_preview():
    api = FakeApi(msgs=[msg(1, text="x" * 200)])
    [chat] = C.chats_to_list(api)
    assert chat["name"] == "Ali (work)" and chat["unread"] == 2
    assert len(chat["last_message"]["text"]) == 120


def test_sniff_mime():
    assert C.sniff_mime(b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert C.sniff_mime(b"\x00\x00\x00\x18ftypmp42") == "video/mp4"
    assert C.sniff_mime(b"\x00\x00\x00\x18ftypM4A ") == "audio/mp4"
    assert C.sniff_mime(b"%PDF-1.4") == "application/pdf"
    assert C.sniff_mime(b"nope") == "application/octet-stream"


def test_media_kind():
    assert C.media_kind("/x/a.JPG") == "image"
    assert C.media_kind("/x/a.mp4") == "video"
    assert C.media_kind("/x/a.pdf") == "file"
    assert C.media_kind("/x/noext") == "file"


def test_message_id_is_validated():
    with pytest.raises(ValueError):
        C._check_message_id("../../etc/passwd")
    assert C._check_message_id(" 123 ") == "123"


def test_download_media_names(tmp_path):
    api = SimpleNamespace(obs=SimpleNamespace(download_object=lambda *a: b"%PDF-1.7 data"))
    r = C.download_media_to_file(api, "42", str(tmp_path))
    assert r["path"].endswith("42.pdf") and r["mime"] == "application/pdf"
    r = C.download_media_to_file(api, "42", str(tmp_path), "../evil/report.pdf")
    assert r["path"] == os.path.join(str(tmp_path), "42_report.pdf")


def test_save_session_is_private(tmp_path):
    written = {}

    class Api:
        def save_tokens(self, path):
            written["mode"] = os.stat(path).st_mode & 0o777  # mode at write time
            with open(path, "w") as f:
                f.write("{}")

    path = str(tmp_path / "s" / "session.json")
    C.save_session(Api(), path)
    assert written["mode"] == 0o600
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_as_list_handles_boxes_by_ids():
    res = {"messageBoxesByIds": {"uA": {"id": "uA"}}}
    assert C._as_list(res) == [{"id": "uA"}]


def test_box_to_dict_normalizes_types():
    box = {"id": "uA", "midType": 0, "unreadCount": "3", "lastMessages": [],
           "lastDeliveredMessageId": {"messageId": "77", "deliveredTime": "1790148702675"}}
    d = C.box_to_dict(box)
    assert d["kind"] == "user" and d["unread_count"] == 3 and d["last_message_id"] == "77"
    assert d["last_message_time"].startswith("2026-")
    empty = C.box_to_dict({"id": "uB", "lastDeliveredMessageId": {"messageId": "-1", "deliveredTime": "0"}})
    assert empty["last_message_id"] is None and empty["last_message_time"] is None


def test_pagination_uses_seen_message():
    class Api(FakeApi):
        def get_previous_messages(self, chat_id, end_id, delivered, count):
            self.prev_args = (chat_id, end_id, delivered, count)
            return [msg(5), msg(4)]

    api = Api(msgs=[msg(6, deliveredTime="1700000000006")])
    C.messages_to_dicts(api, api.get_recent_messages("uA", 1))  # like line_read did
    older = C.read_messages(api, "uA", 2, "6")
    assert api.prev_args == ("uA", "6", 1700000000006, 2)
    assert [m["id"] for m in older] == ["5", "4"]
    with pytest.raises(ValueError):
        C.read_messages(api, "uA", 2, "12345")  # never seen, not in recent


def _encrypt_like_line(plain: bytes, km_b64: str, chunked: bool = False) -> bytes:
    import base64, hashlib, hmac
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    k = HKDF(hashes.SHA256(), 76, salt=None, info=b"FileEncryption").derive(base64.b64decode(km_b64))
    enc = Cipher(algorithms.AES(k[:32]), modes.CTR(k[64:76] + b"\0" * 4)).encryptor()
    ct = enc.update(plain) + enc.finalize()
    mac_in = b"".join(hashlib.sha256(ct[i:i + 131072]).digest() for i in range(0, len(ct), 131072)) if chunked else ct
    return ct + hmac.new(k[32:64], mac_in, hashlib.sha256).digest()


def test_decrypt_file_roundtrip_plain_and_chunked_mac():
    import base64
    km = base64.b64encode(b"k" * 32).decode()
    jpeg = b"\xff\xd8\xff" + os.urandom(300_000)
    assert C.decrypt_file(_encrypt_like_line(jpeg, km), km) == jpeg
    assert C.decrypt_file(_encrypt_like_line(jpeg, km, chunked=True), km) == jpeg
    bad = bytearray(_encrypt_like_line(jpeg, km)); bad[5] ^= 1
    with pytest.raises(ValueError):
        C.decrypt_file(bytes(bad), km)


def test_talk_meta_matches_extension_encoding():
    import base64, json
    outer = json.loads(base64.b64decode(C.talk_meta("632886324617019823")))
    raw = base64.b64decode(outer["message"])
    assert raw == (b"\x0b\x00\x04\x00\x00\x00\x12" + b"632886324617019823"
                   + b"\x0f\x00\x1b\x0c\x00\x00\x00\x00" + b"\x00")


def test_e2ee_media_download_decrypts(monkeypatch):
    import base64
    km = base64.b64encode(b"m" * 32).decode()
    jpeg = b"\xff\xd8\xff\xe0" + b"x" * 100
    sent = {}

    class Resp:
        status_code = 200
        content = _encrypt_like_line(jpeg, km)
        def raise_for_status(self): pass

    def _send(method, url, headers):
        sent.update(url=url, headers=headers)
        return Resp()

    api = FakeApi()
    api.config = SimpleNamespace(obs_base="https://obs", application_header="CHROMEOS\t3.7.2\tChrome_OS\t", user_agent="UA")
    api.transport = SimpleNamespace(_send=_send)
    api.get_encrypted_access_token = lambda ft: "ENC"
    C._remember(msg(9, contentType=1, chunks=["x"] * 5, contentMetadata={"SID": "emi", "OID": "abc"}, _plain={"keyMaterial": km}))
    data, mime = C.download_media(api, "9")
    assert data == jpeg and mime == "image/jpeg"
    assert sent["url"] == "https://obs/r/talk/emi/abc"
    assert sent["headers"]["X-Line-Access"] == "ENC" and sent["headers"]["X-Talk-Meta"] == C.talk_meta("9")


def test_e2ee_media_without_key_is_reported():
    api = FakeApi(ready=False)
    C._remember(msg(9, contentType=1, chunks=["x"] * 5, contentMetadata={"SID": "emi", "OID": "abc"}))
    with pytest.raises(C.UnsupportedMedia):
        C.download_media(api, "9")


def test_name_failures_are_not_cached(monkeypatch):
    class Broken(FakeApi):
        def get_all_contact_ids(self):
            raise ConnectionError("offline")

    monkeypatch.setattr(C, "get_api", lambda: Broken())
    assert C.names_or_empty() == ({}, {})
    assert not C._names  # nothing cached
    with pytest.raises(ConnectionError):
        C._name_cache()


def test_find_node_prefers_env(monkeypatch):
    monkeypatch.setenv("LINE_NODE", "/custom/node")
    assert C.find_node() == "/custom/node"
    monkeypatch.delenv("LINE_NODE")
    monkeypatch.setenv("PATH", "/nonexistent")
    found = C.find_node()
    assert found is None or os.path.isfile(found)


def test_names_for_looks_up_non_friends_once():
    calls = []

    class Api(FakeApi):
        def get_contacts(self, mids):
            calls.append(list(mids))
            if mids == ["uA", "uB"]:
                return super().get_contacts(mids)
            return {"contacts": {"uZ": {"contact": {"displayName": "Zoe"}}}}

    C._extra_names.clear()
    api = Api()
    C.names_for(api, {"uZ", "uA"})
    names = C.names_for(api, {"uZ", "uQ"})
    assert names["uZ"] == "Zoe" and names["uA"] == "Ali (work)"
    assert calls.count(["uZ"]) == 1  # cached after first lookup
    C._extra_names.clear()


def test_chat_of():
    assert C.chat_of({"from": "uA", "to": "uME", "toType": 0}, "uME") == "uA"
    assert C.chat_of({"from": "uME", "to": "uA", "toType": 0}, "uME") == "uA"
    assert C.chat_of({"from": "uA", "to": "Cgroup", "toType": 2}, "uME") == "Cgroup"


def test_read_receipts():
    class Api(FakeApi):
        def get_message_read_range(self, chat_ids):
            return [{"chatId": "Cg", "ranges": {
                "uA": [{"startMessageId": "1", "endMessageId": "100", "endTime": "1790000000000"}],
                "uB": [{"startMessageId": "1", "endMessageId": "50", "endTime": "1780000000000"}],
                "uME": [{"startMessageId": "1", "endMessageId": "100"}],
            }}]

    r = C.read_receipts(Api(), "Cg", "80")
    assert r["read_by"] == ["Ali (work)"]
    assert {x["mid"] for x in r["readers"]} == {"uA", "uB"}

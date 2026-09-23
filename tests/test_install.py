"""Client installers, run against a throwaway HOME."""

from __future__ import annotations

import json
import os

import pytest

from line_mcp import install as I

SPEC = {"command": "/py", "args": ["-m", "line_mcp", "serve"], "env": {"LINE_NODE": "/node"}}


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return tmp_path


def test_json_client_merges_and_backs_up(fake_home):
    path = fake_home / ".cursor" / "mcp.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
    I.CLIENTS["cursor"].install(SPEC)
    data = json.loads(path.read_text())
    assert data["mcpServers"]["other"] == {"command": "x"} and data["theme"] == "dark"
    assert data["mcpServers"]["line-personal"] == {"command": "/py", "args": ["-m", "line_mcp", "serve"], "env": {"LINE_NODE": "/node"}}
    assert (fake_home / ".cursor" / "mcp.json.bak-line-mcp").exists()
    I.CLIENTS["cursor"].install(SPEC, remove=True)
    assert "line-personal" not in json.loads(path.read_text())["mcpServers"]


def test_vscode_uses_servers_key_and_stdio_type():
    I.CLIENTS["vscode"].install(SPEC)
    data = json.loads(open(I._vscode_path()).read())
    assert data["servers"]["line-personal"]["type"] == "stdio"


def test_claude_desktop_created_from_scratch():
    I.CLIENTS["claude-desktop"].install(SPEC)
    data = json.loads(open(I._claude_desktop_path()).read())
    assert data["mcpServers"]["line-personal"]["command"] == "/py"


def test_invalid_json_is_not_clobbered(fake_home):
    path = fake_home / ".gemini" / "settings.json"
    path.parent.mkdir()
    path.write_text("{ not json")
    with pytest.raises(RuntimeError, match="valid JSON"):
        I.CLIENTS["gemini"].install(SPEC)
    assert path.read_text() == "{ not json"


def test_codex_toml_add_replace_remove_keeps_other_config(fake_home):
    path = fake_home / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_text('model = "gpt-5"  # my comment\n\n[mcp_servers.other]\ncommand = "x"\n')
    I.CLIENTS["codex"].install(SPEC)
    I.CLIENTS["codex"].install({**SPEC, "command": "/py2"})  # re-install replaces, not duplicates
    text = path.read_text()
    assert text.count("[mcp_servers.line-personal]") == 1 and 'command = "/py2"' in text
    assert 'model = "gpt-5"  # my comment' in text and "[mcp_servers.other]" in text
    assert 'env = { "LINE_NODE" = "/node" }' in text
    try:
        import tomllib
        cfg = tomllib.loads(text)
        assert cfg["mcp_servers"]["line-personal"]["args"] == ["-m", "line_mcp", "serve"]
    except ImportError:  # Python 3.10
        pass
    I.CLIENTS["codex"].install(SPEC, remove=True)
    text = path.read_text()
    assert "line-personal" not in text and "[mcp_servers.other]" in text


def test_uvx_spec_points_at_repo():
    spec = I.server_spec(use_uvx=True)
    assert spec["args"][:2] == ["--from", I.REPO] and spec["args"][-2:] == ["line-mcp", "serve"]

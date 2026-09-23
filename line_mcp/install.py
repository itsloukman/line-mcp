"""Register line-mcp with MCP clients (`line-mcp install <client>` / `line-mcp setup`).

Every client gets the same stdio server: this Python interpreter running
`-m line_mcp serve`, with LINE_NODE pinned to the detected Node.js. Absolute
paths mean GUI apps with a minimal PATH (Claude Desktop, Cursor, ...) work.
Existing config files are merged (never overwritten) and backed up first.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable

from . import client as C

NAME = "line-personal"
REPO = "git+https://github.com/itsloukman/line-mcp"


def server_spec(*, use_uvx: bool = False) -> dict:
    """{command, args, env} for this installation."""
    env = {}
    node = C.find_node()
    if node:
        env["LINE_NODE"] = os.path.abspath(node)
    if os.environ.get("LINE_SESSION_PATH"):
        env["LINE_SESSION_PATH"] = os.environ["LINE_SESSION_PATH"]
    if use_uvx or _ephemeral_python():
        uvx = shutil.which("uvx") or "uvx"
        return {"command": uvx, "args": ["--from", REPO, "line-mcp", "serve"], "env": env}
    return {"command": sys.executable, "args": ["-m", "line_mcp", "serve"], "env": env}


def _ephemeral_python() -> bool:
    """Running from a throwaway uvx environment? Then don't pin its path."""
    exe = sys.executable.replace("\\", "/")
    return "/uv/archive-" in exe or "/.cache/uv/" in exe


# -- config files -------------------------------------------------------------------


def _home(*parts: str) -> str:
    return os.path.join(os.path.expanduser("~"), *parts)


def _claude_desktop_path() -> str:
    if sys.platform == "darwin":
        return _home("Library", "Application Support", "Claude", "claude_desktop_config.json")
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", _home("AppData", "Roaming")), "Claude", "claude_desktop_config.json")
    return _home(".config", "Claude", "claude_desktop_config.json")


def _vscode_path() -> str:
    if sys.platform == "darwin":
        return _home("Library", "Application Support", "Code", "User", "mcp.json")
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", _home("AppData", "Roaming")), "Code", "User", "mcp.json")
    return _home(".config", "Code", "User", "mcp.json")


def _load_json(path: str) -> dict:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return {}
    with open(path, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except ValueError as exc:
            raise RuntimeError(f"{path} isn't valid JSON ({exc}); fix it or add the server by hand") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} doesn't contain a JSON object")
    return data


def _backup(path: str) -> None:
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak-line-mcp")


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _backup(path)
    tmp = path + ".tmp-line-mcp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def _json_installer(path_fn: Callable[[], str], key: str = "mcpServers", *, vscode: bool = False):
    def install(spec: dict, remove: bool = False) -> str:
        path = path_fn()
        data = _load_json(path)
        servers = data.setdefault(key, {})
        if remove:
            if servers.pop(NAME, None) is None:
                return f"not registered in {path}"
            _write_json(path, data)
            return f"removed from {path}"
        entry = {"command": spec["command"], "args": spec["args"]}
        if spec.get("env"):
            entry["env"] = spec["env"]
        if vscode:
            entry = {"type": "stdio", **entry}
        servers[NAME] = entry
        _write_json(path, data)
        return f"added to {path}"

    return install


def _codex_path() -> str:
    return os.path.join(os.environ.get("CODEX_HOME", _home(".codex")), "config.toml")


def _toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)  # TOML basic strings share JSON escaping


def _codex_install(spec: dict, remove: bool = False) -> str:
    """~/.codex/config.toml: [mcp_servers.line-personal] (edited textually so
    the user's comments and formatting survive)."""
    path = _codex_path()
    text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    header = f"[mcp_servers.{NAME}]"
    lines = text.splitlines()
    # drop an existing block (header .. next table header)
    out, skipping, found = [], False, False
    for line in lines:
        stripped = line.strip()
        if stripped in (header, f"[mcp_servers.{NAME}.env]"):
            skipping, found = True, True
            continue
        if skipping and stripped.startswith("["):
            skipping = False
        if not skipping:
            out.append(line)
    if remove:
        if not found:
            return f"not registered in {path}"
    else:
        block = [
            "",
            header,
            f"command = {_toml_str(spec['command'])}",
            "args = [" + ", ".join(_toml_str(a) for a in spec["args"]) + "]",
        ]
        if spec.get("env"):
            block.append("env = { " + ", ".join(f"{_toml_str(k)} = {_toml_str(v)}" for k, v in spec["env"].items()) + " }")
        while out and not out[-1].strip():
            out.pop()
        out += block
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _backup(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out).lstrip("\n") + "\n")
    return f"{'removed from' if remove else 'added to'} {path}"


def _claude_code_install(spec: dict, remove: bool = False) -> str:
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("the `claude` CLI isn't on PATH (install Claude Code first)")
    subprocess.run([claude, "mcp", "remove", "--scope", "user", NAME], capture_output=True, text=True)
    if remove:
        return "removed from Claude Code (user scope)"
    cmd = [claude, "mcp", "add", "--scope", "user"]
    for k, v in (spec.get("env") or {}).items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [NAME, "--", spec["command"], *spec["args"]]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip() or "claude mcp add failed")
    return "added to Claude Code (user scope, all projects)"


@dataclass
class Client:
    key: str
    label: str
    install: Callable[..., str]
    detect: Callable[[], bool]
    restart_hint: str


def _dir_exists(path_fn: Callable[[], str]) -> Callable[[], bool]:
    return lambda: os.path.isdir(os.path.dirname(path_fn()))


CLIENTS: dict[str, Client] = {
    c.key: c
    for c in [
        Client("claude-desktop", "Claude Desktop", _json_installer(_claude_desktop_path),
               _dir_exists(_claude_desktop_path), "Quit and reopen Claude Desktop."),
        Client("claude-code", "Claude Code", _claude_code_install,
               lambda: bool(shutil.which("claude")), "Start a new `claude` session."),
        Client("codex", "OpenAI Codex", _codex_install,
               lambda: bool(shutil.which("codex")) or os.path.isdir(os.path.dirname(_codex_path())),
               "Start a new `codex` session."),
        Client("cursor", "Cursor", _json_installer(lambda: _home(".cursor", "mcp.json")),
               _dir_exists(lambda: _home(".cursor", "mcp.json")), "Reload Cursor (MCP settings)."),
        Client("windsurf", "Windsurf", _json_installer(lambda: _home(".codeium", "windsurf", "mcp_config.json")),
               _dir_exists(lambda: _home(".codeium", "windsurf", "mcp_config.json")), "Refresh MCP servers in Windsurf."),
        Client("vscode", "VS Code (Copilot agent mode)", _json_installer(_vscode_path, "servers", vscode=True),
               _dir_exists(_vscode_path), "Reload the VS Code window."),
        Client("gemini", "Gemini CLI", _json_installer(lambda: _home(".gemini", "settings.json")),
               _dir_exists(lambda: _home(".gemini", "settings.json")), "Start a new `gemini` session."),
    ]
}


def detected() -> list[Client]:
    return [c for c in CLIENTS.values() if c.detect()]


def manual_snippet(spec: dict | None = None) -> str:
    spec = spec or server_spec()
    entry = {"command": spec["command"], "args": spec["args"]}
    if spec.get("env"):
        entry["env"] = spec["env"]
    return json.dumps({"mcpServers": {NAME: entry}}, indent=2)

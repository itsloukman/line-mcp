"""line-mcp: `line-mcp login` (QR), `line-mcp serve` (MCP over stdio), `line-mcp doctor`."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys


def cmd_login(args) -> int:
    from okline.qrterm import print_qr

    from .client import session_path
    from .login import LoginFlow

    qr_path = os.path.join(os.path.dirname(session_path()), "qr.png")
    flow: LoginFlow

    def on_qr(url: str) -> None:
        os.makedirs(os.path.dirname(qr_path), mode=0o700, exist_ok=True)
        with open(qr_path, "wb") as f:
            f.write(flow.qr_png or b"")
        print("\nScan this QR with LINE on your phone (Home → QR scanner):", flush=True)
        if not args.no_terminal_qr:
            print_qr(url)
        print(f"QR image: {qr_path}", flush=True)
        print(f"Login URL (if you prefer): {url}", flush=True)

    def on_pin(pin: str) -> None:
        print(f"Enter this PIN on your phone when asked: {pin}", flush=True)

    flow = LoginFlow(timeout=args.timeout, on_qr=on_qr, on_pin=on_pin)
    try:
        ok = flow.run()
    finally:
        if os.path.exists(qr_path):
            os.remove(qr_path)  # the QR is a login secret; don't leave it around
    if not ok:
        print(f"Login failed: {flow.error}", file=sys.stderr)
        return 1
    p = flow.profile or {}
    print(f"Logged in as {p.get('displayName')} ({p.get('mid')})" if p else "Logged in.")
    if not flow.e2ee_ready:
        print("Warning: E2EE keys were not captured; encrypted chats won't decrypt.")
    print(f"Session saved to {session_path()} (mode 600).")
    return 0


def cmd_serve(args) -> int:
    from . import store
    from .server import mcp

    if store.get_store() is not None:
        store.start_live_sync()
    mcp.run()
    return 0


def cmd_doctor(args) -> int:
    """Check everything the server needs, with actionable hints."""
    from .client import find_node, get_api, session_path

    ok = True

    def report(good: bool, label: str, hint: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print(f"[{'ok' if good else 'FAIL'}] {label}" + (f"\n       → {hint}" if hint and not good else ""))

    node = find_node()
    version = ""
    if node:
        try:
            version = subprocess.run(
                [node, "--version"], capture_output=True, text=True, timeout=10
            ).stdout.strip()
        except Exception:
            version = ""
    major = int(version.lstrip("v").split(".")[0]) if version.lstrip("v")[:1].isdigit() else 0
    report(major >= 18, f"Node.js {version or 'not found'} ({node or 'no LINE_NODE / not on PATH'})",
           "Install Node 18+, or set LINE_NODE=/path/to/node (MCP clients often have a minimal PATH).")

    path = session_path()
    exists = os.path.exists(path)
    report(exists, f"Session file {path}", "Run `line-mcp login`.")
    if not exists:
        return 1
    mode = os.stat(path).st_mode & 0o777
    report(mode & 0o077 == 0, f"Session file mode {oct(mode)}", f"chmod 600 {path}")

    try:
        api = get_api()
        p = api.get_profile()
        report(True, f"Logged in as {p.get('displayName')} ({p.get('mid')})")
        report(api.e2ee.is_ready(), "E2EE (Letter Sealing) keys loaded",
               "Encrypted chats won't decrypt — run `line-mcp login` again.")
    except Exception as exc:
        report(False, f"LINE API call failed: {exc}", "Run `line-mcp login` again.")

    from . import store

    if store.enabled():
        st = store.get_store()
        print(f"[ok] Local message store {store.db_path()}: {st.stats()}")
    else:
        print("[--] Local message store disabled (LINE_MCP_STORE=off)")
    return 0 if ok else 1


def cmd_install(args) -> int:
    from . import install as I

    spec = I.server_spec(use_uvx=args.uvx)
    if args.print:
        print(I.manual_snippet(spec))
        return 0
    keys = list(I.CLIENTS) if args.all else args.clients
    if not keys:
        found = I.detected()
        print("Detected clients: " + (", ".join(c.key for c in found) or "none"))
        print("Usage: line-mcp install <client> [...] | --all | --print")
        print("Clients: " + ", ".join(I.CLIENTS))
        return 0
    rc = 0
    for key in keys:
        c = I.CLIENTS.get(key)
        if c is None:
            print(f"[FAIL] unknown client {key!r} (choose from: {', '.join(I.CLIENTS)})")
            rc = 1
            continue
        try:
            msg = c.install(spec, remove=args.remove)
            print(f"[ok] {c.label}: {msg}" + ("" if args.remove else f" — {c.restart_hint}"))
        except Exception as exc:
            print(f"[FAIL] {c.label}: {exc}")
            rc = 1
    return rc


def cmd_setup(args) -> int:
    """Guided first run: check Node, log in, register with detected clients."""
    from . import install as I
    from .client import find_node, session_path

    print("line-mcp setup\n")
    node = find_node()
    if not node:
        print("[FAIL] Node.js 18+ is required (LINE's request signing runs in Node).")
        print("       Install it from https://nodejs.org/ and run `line-mcp setup` again.")
        return 1
    print(f"[ok] Node.js: {node}")
    if os.path.exists(session_path()):
        print(f"[ok] Already logged in ({session_path()})")
    else:
        print("\nStep 1 — log in to LINE (your phone stays logged in):")
        login_args = argparse.Namespace(timeout=600, no_terminal_qr=False)
        if cmd_login(login_args) != 0:
            return 1
    found = I.detected()
    if not found:
        print("\nNo MCP clients detected. Add this to your client's MCP config:\n")
        print(I.manual_snippet())
        return 0
    print("\nStep 2 — connect your apps:")
    spec = I.server_spec()
    for c in found:
        answer = "y" if args.yes else input(f"  Add to {c.label}? [Y/n] ").strip().lower()
        if answer in ("", "y", "yes"):
            try:
                print(f"  [ok] {c.install(spec)} — {c.restart_hint}")
            except Exception as exc:
                print(f"  [FAIL] {c.label}: {exc}")
    print("\nDone. Try asking: \"What did I miss on LINE today?\"")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="line-mcp", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login", help="QR login; saves session tokens")
    login.add_argument("--timeout", type=int, default=600,
                       help="seconds to keep offering fresh QR codes (default 600)")
    login.add_argument("--no-terminal-qr", action="store_true",
                       help="don't draw the QR in the terminal (image file only)")
    sub.add_parser("serve", help="Run the MCP server over stdio")
    sub.add_parser("doctor", help="Check Node.js, the session and E2EE keys")
    setup = sub.add_parser("setup", help="Guided first run: log in and connect your MCP apps")
    setup.add_argument("-y", "--yes", action="store_true", help="add to every detected client without asking")
    for name, helptext in (("install", "Add line-mcp to MCP clients"), ("uninstall", "Remove line-mcp from MCP clients")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("clients", nargs="*", help="claude-desktop, claude-code, codex, cursor, windsurf, vscode, gemini")
        p.add_argument("--all", action="store_true", help="every supported client")
        p.add_argument("--uvx", action="store_true", help="launch via uvx from GitHub instead of this Python")
        p.add_argument("--print", action="store_true", help="print a JSON config snippet instead")
    args = parser.parse_args()
    if args.command == "login":
        sys.exit(cmd_login(args))
    if args.command == "doctor":
        sys.exit(cmd_doctor(args))
    if args.command == "setup":
        sys.exit(cmd_setup(args))
    if args.command in ("install", "uninstall"):
        args.remove = args.command == "uninstall"
        sys.exit(cmd_install(args))
    sys.exit(cmd_serve(args))


if __name__ == "__main__":
    main()

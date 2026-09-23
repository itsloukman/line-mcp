"""line-mcp: `line-mcp login` (QR), `line-mcp serve` (MCP over stdio), `line-mcp doctor`."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time


def _existing_certificate() -> str | None:
    """Device certificate from a previous login — lets LINE skip the PIN step."""
    from .client import session_path

    try:
        from okline.session import Session

        return Session.load(session_path()).certificate or None
    except Exception:
        return None


def cmd_login(args) -> int:
    import qrcode
    from okline import OkLine
    from okline.exceptions import LineApiError, LineTransportError
    from okline.transport import LineConfig
    from okline.qrterm import print_qr

    from .client import ensure_node_env, save_session, session_path

    ensure_node_env()
    session_dir = os.path.dirname(session_path())
    os.makedirs(session_dir, mode=0o700, exist_ok=True)
    qr_path = os.path.join(session_dir, "qr.png")

    def on_qr(url: str) -> None:
        qrcode.make(url).save(qr_path)
        print("\nScan this QR with LINE on your phone (Home → QR scanner):", flush=True)
        if not args.no_terminal_qr:
            print_qr(url)
        print(f"QR image: {qr_path}", flush=True)
        print(f"Login URL (if you prefer): {url}", flush=True)

    def on_pin(pin: str) -> None:
        print(f"Enter this PIN on your phone when asked: {pin}", flush=True)

    deadline = time.monotonic() + args.timeout
    cert = _existing_certificate()
    try:
        while True:
            # The gateway holds the scan/PIN long-polls open longer than okline's
            # default 30s read timeout, so give login requests more room.
            api = OkLine(certificate=cert, config=LineConfig(timeout=90.0))
            try:
                # LINE QR codes expire after a few minutes; keep offering a
                # fresh one until the overall timeout.
                api.qr_login(on_qr=on_qr, on_pin=on_pin, wait_seconds=180)
                break
            except (LineApiError, LineTransportError) as exc:
                api.close()
                expired = isinstance(exc, LineTransportError) or (
                    "expired" in str(exc).lower() or exc.code == 100
                )
                if expired and time.monotonic() < deadline:
                    print("QR code expired or timed out — generating a new one...", flush=True)
                    continue
                print(f"Login failed: {exc}", file=sys.stderr)
                return 1
    finally:
        if os.path.exists(qr_path):
            os.remove(qr_path)  # the QR is a login secret; don't leave it around
    path = save_session(api)
    try:
        profile = api.get_profile()
        print(f"Logged in as {profile.get('displayName')} ({profile.get('mid')})")
    except Exception:
        print("Logged in.")
    if not api.e2ee.is_ready():
        print("Warning: E2EE keys were not captured; encrypted chats won't decrypt.")
    print(f"Session saved to {path} (mode 600).")
    api.close()
    return 0


def cmd_serve(args) -> int:
    from .server import mcp

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
    return 0 if ok else 1


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
    args = parser.parse_args()
    if args.command == "login":
        sys.exit(cmd_login(args))
    if args.command == "doctor":
        sys.exit(cmd_doctor(args))
    sys.exit(cmd_serve(args))


if __name__ == "__main__":
    main()

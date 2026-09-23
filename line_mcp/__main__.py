"""line-mcp: `line-mcp login` (QR) and `line-mcp serve` (MCP over stdio)."""

from __future__ import annotations

import argparse
import os
import sys


def cmd_login(args) -> int:
    import qrcode
    from okline import OkLine

    from .client import save_session, session_path

    qr_path = os.path.join(os.path.dirname(session_path()), "qr.png")
    os.makedirs(os.path.dirname(session_path()), exist_ok=True)

    def on_qr(url: str) -> None:
        qrcode.make(url).save(qr_path)
        print(f"QR code saved to {qr_path} — scan it with LINE on your phone.")
        print(f"Login URL (if you prefer): {url}", flush=True)

    def on_pin(pin: str) -> None:
        print(f"Enter this PIN on your phone when asked: {pin}", flush=True)

    api = OkLine()
    print("Waiting for QR scan (5 minutes)...", flush=True)
    api.qr_login(on_qr=on_qr, on_pin=on_pin, wait_seconds=300)
    path = save_session(api)
    try:
        profile = api.get_profile()
        print(f"Logged in as {profile.get('displayName')} ({profile.get('mid')})")
    except Exception:
        print("Logged in.")
    print(f"Session saved to {path} (mode 600).")
    return 0


def cmd_serve(args) -> int:
    from .server import mcp

    mcp.run()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="line-mcp", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="QR login; saves session tokens")
    sub.add_parser("serve", help="Run the MCP server over stdio")
    args = parser.parse_args()
    if args.command == "login":
        sys.exit(cmd_login(args))
    sys.exit(cmd_serve(args))


if __name__ == "__main__":
    main()

"""QR login flow shared by `line-mcp login` and the line_login_* MCP tools."""

from __future__ import annotations

import io
import os
import threading
import time
from typing import Callable

from . import client as C


def _existing_certificate() -> str | None:
    """Device certificate from a previous login — lets LINE skip the PIN step."""
    try:
        from okline.session import Session

        return Session.load(C.session_path()).certificate or None
    except Exception:
        return None


class LoginFlow:
    """One QR login attempt (with automatic QR refresh until `timeout`)."""

    def __init__(
        self,
        *,
        timeout: float = 600,
        on_qr: Callable[[str], None] | None = None,
        on_pin: Callable[[str], None] | None = None,
    ) -> None:
        self.timeout = timeout
        self._on_qr, self._on_pin = on_qr, on_pin
        self.state = "starting"  # waiting_for_scan | waiting_for_pin | done | failed
        self.qr_url: str | None = None
        self.qr_png: bytes | None = None
        self.qr_generated_at: float | None = None
        self.pin: str | None = None
        self.error: str | None = None
        self.profile: dict | None = None
        self.e2ee_ready = False
        self._thread: threading.Thread | None = None

    # -- callbacks -------------------------------------------------------------
    def _qr(self, url: str) -> None:
        import qrcode

        buf = io.BytesIO()
        qrcode.make(url).save(buf, format="PNG")
        self.qr_url, self.qr_png, self.qr_generated_at = url, buf.getvalue(), time.time()
        self.pin = None
        self.state = "waiting_for_scan"
        if self._on_qr:
            self._on_qr(url)

    def _pin(self, pin: str) -> None:
        self.pin, self.state = pin, "waiting_for_pin"
        if self._on_pin:
            self._on_pin(pin)

    # -- run -------------------------------------------------------------------
    def run(self) -> bool:
        from okline import OkLine
        from okline.exceptions import LineApiError, LineTransportError
        from okline.transport import LineConfig

        C.ensure_node_env()
        deadline = time.monotonic() + self.timeout
        cert = _existing_certificate()
        while True:
            # The gateway holds the scan/PIN long-polls open longer than okline's
            # default 30s read timeout, so give login requests more room.
            api = OkLine(certificate=cert, config=LineConfig(timeout=90.0), record=False)
            try:
                # LINE QR codes expire after a few minutes; keep offering a
                # fresh one until the overall timeout.
                api.qr_login(on_qr=self._qr, on_pin=self._pin, wait_seconds=180)
                break
            except (LineApiError, LineTransportError) as exc:
                api.close()
                expired = isinstance(exc, LineTransportError) or (
                    "expired" in str(exc).lower() or exc.code == 100
                )
                if expired and time.monotonic() < deadline:
                    continue
                self.state, self.error = "failed", str(exc)
                return False
            except Exception as exc:
                api.close()
                self.state, self.error = "failed", str(exc)
                return False
        try:
            C.save_session(api)
            self.e2ee_ready = api.e2ee.is_ready()
            try:
                p = api.get_profile()
                self.profile = {"mid": p.get("mid"), "displayName": p.get("displayName")}
            except Exception:
                self.profile = None
        finally:
            api.close()
        C.reset_api()  # a running server picks up the new session on its next call
        self.qr_png = self.qr_url = None  # the QR is a login secret; drop it
        self.state = "done"
        return True

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="line-mcp-login", daemon=True)
        self._thread.start()

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict:
        out: dict = {"state": self.state}
        if self.state == "waiting_for_scan":
            out["hint"] = "Scan the QR with LINE on your phone (Home → QR scanner), then approve."
            out["qr_age_seconds"] = int(time.time() - (self.qr_generated_at or time.time()))
        if self.state == "waiting_for_pin" and self.pin:
            out["pin"] = self.pin
            out["hint"] = "Type this PIN into LINE on your phone."
        if self.state == "done":
            out["profile"] = self.profile
            out["e2ee_ready"] = self.e2ee_ready
            out["session_path"] = C.session_path()
        if self.state == "failed":
            out["error"] = self.error
        return out


def qr_file_writer(path: str) -> Callable[[LoginFlow], None]:
    """Helper for the CLI: keep a PNG of the current QR at `path`."""

    def write(flow: LoginFlow) -> None:
        if flow.qr_png:
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            with open(path, "wb") as f:
                f.write(flow.qr_png)

    return write

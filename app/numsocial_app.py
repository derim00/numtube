"""NumSocial: one app to install NumTube (YouTube) and NumChat (Snapchat) on a
NumWorks, and to run their gateway.

Double-click it: a page opens in the browser with the install button, and the
gateway runs in the background as long as the app is open.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_VERSION = "2.0"

if len(sys.argv) > 1 and sys.argv[1] == "--yt-dlp":
    # The gateway runs yt-dlp as a subprocess of this very executable.
    import yt_dlp

    sys.exit(yt_dlp.main(sys.argv[2:]))

if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
    # Used by the build workflow: checks that every bundled part loads.
    import subprocess

    import imageio_ffmpeg
    import libusb_package
    import usb.core
    import yt_dlp

    here = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    print("ffmpeg:", subprocess.run([ffmpeg, "-version"], capture_output=True,
                                    text=True).stdout.splitlines()[0])
    print("yt-dlp:", yt_dlp.version.__version__)
    backend = libusb_package.get_libusb1_backend()
    print("libusb:", "ok" if backend else "MISSING")
    list(usb.core.find(find_all=True, backend=backend))
    from playwright._impl._driver import compute_driver_executable

    driver = compute_driver_executable()
    driver = driver if isinstance(driver, (list, tuple)) else [driver]
    print("playwright:", subprocess.run([*map(str, driver), "--version"], capture_output=True,
                                        text=True).stdout.strip() or "MISSING")
    import numchat_media, numchat_service, numchat_snap  # noqa: F401  (NumChat loads)
    import mss, numscreen_input, numscreen_service  # noqa: F401  (NumScreen loads)
    print("numscreen:", numscreen_input.create("real").name)
    firmware = sorted(os.listdir(os.path.join(here, "firmware")))
    print("firmware:", firmware)
    sys.exit(0 if backend and firmware else 1)

HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "gateway"))  # when run from source

try:
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except ImportError:
    pass

import flasher  # noqa: E402
import numtube_gateway as gateway  # noqa: E402

PORT = 8737


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.logs: list[str] = []
        self.calculator = "none"  # none | dfu | numtube | error
        self.detail: dict = {}
        self.detected_version: str | None = None
        self.version_probe_done = False
        self.installing = False
        self.progress = 0
        self.install_result = ""
        self.gateway_connected = False

    def log(self, line: str) -> None:
        with self.lock:
            self.logs.append(line)
            del self.logs[:-300]

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "calculator": self.calculator,
                "detail": self.detail,
                "detectedVersion": self.detected_version,
                "installing": self.installing,
                "progress": self.progress,
                "installResult": self.install_result,
                "gatewayConnected": self.gateway_connected,
                "logs": self.logs[-120:],
                "platform": sys.platform,
            }


STATE = State()
gateway.LOG_LISTENERS.append(STATE.log)


def app_log(message: str) -> None:
    gateway.log(message)


def monitor_usb() -> None:
    """Watches which mode the calculator is in."""
    while True:
        if not STATE.installing:
            try:
                device, info = flasher.find_calculator()
            except Exception as error:
                device, info = None, None
                with STATE.lock:
                    STATE.calculator, STATE.detail = "error", {"error": str(error)}
            if device is not None:
                with STATE.lock:
                    STATE.calculator = "error" if "error" in info else "dfu"
                    STATE.detail = info
                if "error" not in info and not STATE.version_probe_done:
                    STATE.version_probe_done = True
                    version = flasher.read_official_version(info["active_slot"])
                    with STATE.lock:
                        STATE.detected_version = version
                    app_log(f"version officielle lue : {version}" if version
                            else "version officielle non lisible, saisie manuelle")
            else:
                with STATE.lock:
                    STATE.version_probe_done = False
                    STATE.calculator = "numtube" if STATE.gateway_connected else "none"
        time.sleep(1)


def run_install(version: str, apps: list[str]) -> None:
    with STATE.lock:
        STATE.installing, STATE.progress, STATE.install_result = True, 0, ""
    try:
        flasher.install(version, app_log, lambda p: setattr(STATE, "progress", p), apps)
        result = "ok"
        app_log("Installation terminée. Ouvre l'appli sur la calculatrice.")
    except Exception as error:
        result = f"Erreur : {error}"
        app_log(result)
    with STATE.lock:
        STATE.installing, STATE.install_result = False, result


def run_gateway() -> None:
    def status(connected: bool) -> None:
        with STATE.lock:
            STATE.gateway_connected = connected
            if connected:
                STATE.calculator = "numtube"

    while True:
        try:
            gateway.serve_usb(gateway.default_options(), status)
        except Exception as error:  # never let the gateway die silently
            app_log(f"erreur de la passerelle : {error}")
            time.sleep(2)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/api/state":
            self._send(200, json.dumps(STATE.snapshot()).encode(), "application/json")
        elif self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "page.html"), "rb") as handle:
                self._send(200, handle.read(), "text/html; charset=utf-8")
        else:
            self._send(404, b"", "text/plain")

    def do_POST(self) -> None:
        # Only the page served by this app may trigger actions.
        if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{PORT}"):
            self._send(403, b"", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/install":
            if STATE.installing:
                self._send(409, b"", "text/plain")
                return
            version = str(body.get("version", "")).strip()
            apps = [str(a) for a in body.get("apps") or ["numtube", "numchat", "numscreen"]]
            threading.Thread(target=run_install, args=(version, apps), daemon=True).start()
            self._send(202, b"{}", "application/json")
        elif self.path == "/api/quit":
            self._send(200, b"{}", "application/json")
            threading.Timer(0.3, lambda: os._exit(0)).start()
        else:
            self._send(404, b"", "text/plain")


def main() -> None:
    url = f"http://127.0.0.1:{PORT}/"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        webbrowser.open(url)  # already running: just show it
        return
    app_log(f"NumSocial {APP_VERSION} demarre")
    threading.Thread(target=monitor_usb, daemon=True).start()
    threading.Thread(target=run_gateway, daemon=True).start()
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    server.serve_forever()


if __name__ == "__main__":
    main()

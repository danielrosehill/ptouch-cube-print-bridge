#!/usr/bin/env python3
"""Label print bridge — drives the Brother PT-P710BT over USB via ptouch-print.

Runs on the machine the printer is plugged into. A web app backend proxies
authenticated print requests here over the LAN or a VPN (e.g. Tailscale).

  GET  /health -> {ok, printer, tapeMm, maxPx}   (queries the printer over USB)
  POST /print  -> {imageDataUrl, copies}         (PNG label, landscape: width = along tape)

The image is scaled so its height fits the printable width of the loaded tape
(reported by ptouch-print --info), thresholded to 1-bit, and printed N times —
one job per copy so the auto-cutter separates the labels.
"""
import base64
import io
import json
import os
import re
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

PORT = int(os.environ.get("BRIDGE_PORT", "9180"))
PTOUCH = os.environ.get("PTOUCH_BIN", "/usr/local/bin/ptouch-print")
MAX_BODY = 8 * 1024 * 1024
MAX_COPIES = 20
THRESHOLD = 160

PRINT_LOCK = threading.Lock()


def printer_info():
    """Query the printer. Returns (ok, {tapeMm, maxPx, raw})."""
    try:
        r = subprocess.run([PTOUCH, "--info"], capture_output=True, text=True, timeout=15)
    except Exception as e:  # noqa: BLE001
        return False, {"error": str(e)}
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        return False, {"error": out.strip() or f"ptouch-print exited {r.returncode}"}
    tape = re.search(r"media width = (\d+) mm", out)
    maxpx = re.search(r"maximum printing width for this tape is (\d+)px", out)
    return True, {
        "printer": "PT-P710BT",
        "tapeMm": int(tape.group(1)) if tape else None,
        "maxPx": int(maxpx.group(1)) if maxpx else None,
    }


def prepare_image(data_url: str, max_px: int) -> Image.Image:
    if "," in data_url:
        data_url = data_url.split(",", 1)[1]
    img = Image.open(io.BytesIO(base64.b64decode(data_url)))
    img = img.convert("L")
    if img.height != max_px:
        w = max(1, round(img.width * max_px / img.height))
        img = img.resize((w, max_px), Image.LANCZOS)
    return img.point(lambda p: 0 if p < THRESHOLD else 255, mode="1")


class Handler(BaseHTTPRequestHandler):
    server_version = "ptouch-print-bridge/1.0"

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path != "/health":
            return self._send(404, {"error": "not found"})
        ok, info = printer_info()
        return self._send(200 if ok else 503, {"ok": ok, **info})

    def do_POST(self):  # noqa: N802
        if self.path != "/print":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self._send(413, {"error": "bad body size"})
        try:
            req = json.loads(self.rfile.read(length))
            data_url = req["imageDataUrl"]
            copies = min(MAX_COPIES, max(1, int(req.get("copies", 1))))
        except Exception:  # noqa: BLE001
            return self._send(400, {"error": "invalid request"})

        with PRINT_LOCK:
            ok, info = printer_info()
            if not ok or not info.get("maxPx"):
                return self._send(503, {"error": "printer unavailable", **info})
            try:
                img = prepare_image(data_url, info["maxPx"])
            except Exception as e:  # noqa: BLE001
                return self._send(400, {"error": f"bad image: {e}"})
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                img.save(f, "PNG")
                path = f.name
            try:
                for _ in range(copies):
                    # --precut chops off the ~25mm blank leader as scrap so the
                    # label itself comes out clean-edged (matches the Brother app)
                    r = subprocess.run(
                        [PTOUCH, "--precut", "--image", path], capture_output=True, text=True, timeout=60
                    )
                    if r.returncode != 0:
                        err = (r.stdout + r.stderr).strip()
                        return self._send(500, {"error": f"print failed: {err}"})
            except subprocess.TimeoutExpired:
                return self._send(500, {"error": "print timed out"})
            finally:
                os.unlink(path)
        return self._send(200, {"ok": True, "copies": copies, "tapeMm": info.get("tapeMm")})

    def log_message(self, fmt, *args):  # quiet default access log
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

#!/usr/bin/env python3
"""WMS label print bridge — drives the Brother PT-P710BT over USB via ptouch-print.

Runs on residencehome next to the printer (deployed from a checkout at
/home/daniel/ptouch-cube-print-bridge/bridge/bridge.py, systemd unit
wms-print-bridge.service). The WMS app (on the VPS) proxies authenticated print
requests here over Tailscale (100.105.192.28).

  GET  /health      -> {ok, printer, tapeMm, maxPx}   (queries the printer over USB)
  POST /print       -> {imageDataUrl, copies}         (PNG label, landscape: width = along tape)
                       {ok, copies, tapeMm, labelMm, tapeUsedMm, rasterPx, attempts, retried}
  POST /print-batch -> {labels: [{imageDataUrl, copies}, ...]}
                       {ok, labels, pages, tapeMm, tapeUsedMm, labelsMm, attempts, retried}

The reply reports the run's measured geometry so the app can log tape
consumption: labelMm is one label's length as actually rastered (after scaling
to the loaded tape), tapeUsedMm adds the once-per-job leader. Both are computed
here rather than app-side because only the bridge knows the tape the printer
reports and the raster it produced from it.

Images are scaled so their height fits the printable width of the loaded tape
(reported by ptouch-print --info) and thresholded to 1-bit.

## Why /print-batch exists: the leader is per *job*, not per label

The print head sits ~25mm behind the cutter, so every job mechanically feeds
~25mm of blank tape before the printed area; --precut trims that off as a scrap
piece. Crucially that cost is once per ptouch-print *invocation*, not per label:
inside one invocation every page but the last is finalized with the chain
command (0x0C, print without feeding) and only the last gets 0x1A (print with
feeding), so the tape reaches the cutter once. --precut then cuts the pages
apart with no further leader.

That is why `--copies N` has always been cheap. It was never available across
*different* labels, because stock ptouch-print concatenates multiple --image
arguments into one long label (img_append) rather than paginating them. So a
batch of N distinct labels meant N invocations and N scrap leaders — which
defeated the point of batching them at all.

`patches/ptouch-print-batch-pages.patch` adds a `--page` separator that ends the
current label and starts a new one, giving N distinct labels as N pages of one
job. This bridge expands copies into repeated pages and sends the whole run in a
single invocation. Verified against the patched binary; if an unpatched
ptouch-print is installed, HAS_PAGE is false and the bridge falls back to the
old one-invocation-per-label behaviour (correct output, N leaders of waste).

ptouch-print exits 0 as soon as the raster data is streamed — a printer-side
abort (red LED) is invisible to it. So after streaming we hold the USB lock for
the estimated physical print time, then read the printer's error register and
automatically resend the job once if it aborted.

Empirical PT-P710BT failure mode (root-caused 2026-07-21): the Cube refuses USB
raster jobs while the Brother phone app holds a Bluetooth session — jobs are
silently discarded with error 0x0100 ("replace media", status_type 0x02, red
LED), and the state flaps as the phone connects/disconnects. Retries keep
--precut and failures carry a close-the-phone-app hint.

The printer is located by USB vendor/product id (04f9:20af), never by port path.

Keep-alive: this does NOT keep the printer awake, despite the name — measured
2026-07-28, correcting the original 2026-07-21 assumption. The Cube powers off 60
minutes after its last *print job*; a status read does not count as activity and
does not defer it. Three independent windows from this bridge's own journal, with
the 300s status-read keepalive running throughout: off 60m45s and 60m33s after the
last POST /print, and ~55-60m after a power-on with no prints at all.

Nothing over USB can wake it either — the printer is bus-powered, advertises no
remote-wakeup bit, and drops off the USB bus entirely when it powers off. So the
background thread only *detects* state: it feeds the "keepalive" block in /health
so callers can see reachability without hitting the bus. It never touches the bus
while a print job holds PRINT_LOCK, and skips a tick if a print happened within the
interval anyway. Set KEEPALIVE_SECONDS=0 to disable the polling.

The real fix lives in the printer, not here: Auto Power Off = None (the factory
default is 60 min) in Brother's Printer Setting Tool -> Device Settings. That is
Windows/Mac and USB-only — Bluetooth and the Design&Print mobile app cannot set it,
and neither can ptouch-print. It is stored in the printer's own NVRAM, so it is a
one-time change that survives reboots and re-cabling.

**Applied 2026-07-28**, so the 60-minute power-off described above should no longer
happen. A factory reset of the printer would revert it. The keepalive's state-change
logging is now the cheapest way to verify: no "printer unreachable" line across a
>60 min idle window means the setting is holding.
"""
import base64
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

PORT = int(os.environ.get("BRIDGE_PORT", "9180"))
PTOUCH = os.environ.get("PTOUCH_BIN", "/usr/local/bin/ptouch-print")
# A batch carries one PNG per label, so the body is much larger than /print's.
MAX_BODY = 24 * 1024 * 1024
MAX_COPIES = 20
# Total pages (labels x copies) in one batch. The whole run is a single job held
# under the USB lock, so this also bounds how long one request can occupy the
# printer.
MAX_PAGES = 60
THRESHOLD = 160
DPI = 180            # PT-P710BT print resolution
TAPE_MM_PER_S = 20   # approximate feed speed, used to estimate job duration
LEADER_MM = 25       # blank leader fed + trimmed once per job, not per page

# One lock for ALL USB access: any USB command (even a status read) issued while
# a job is printing can abort it (red LED). The lock is held for the whole
# physical print, not just while ptouch-print streams the data.
PRINT_LOCK = threading.Lock()
LAST_INFO = {}

# The Cube ignores status queries for a few seconds after a job completes. Batch
# printing sends the next job almost immediately, so wait this long after the
# previous job before touching the bus again.
SETTLE_SECONDS = 6.0
LAST_PRINT_DONE = [0.0]

# Keep-alive: seconds between idle status reads (0 disables). This only samples
# reachability for /health — it does NOT defer the printer's auto-power-off (60 min
# from the last print job on the PT-P710BT; see the module docstring).
KEEPALIVE_SECONDS = int(os.environ.get("KEEPALIVE_SECONDS", "300"))
KEEPALIVE_STATE = {"last": 0.0, "ok": None, "skipped": 0}
LAST_USB_ACTIVITY = time.time()


def ptouch_supports_pages() -> bool:
    """Is the installed ptouch-print carrying patches/ptouch-print-batch-pages.patch?

    Without it there is no way to put two *different* labels in one job, so a
    batch degrades to one job (and one 25mm scrap leader) per label.
    """
    try:
        r = subprocess.run([PTOUCH, "--help"], capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001
        return False
    return "--page" in ((r.stdout or "") + (r.stderr or ""))


HAS_PAGE = ptouch_supports_pages()


def printer_info():
    """Query the printer. Returns (ok, {tapeMm, maxPx, raw})."""
    global LAST_USB_ACTIVITY
    LAST_USB_ACTIVITY = time.time()
    try:
        r = subprocess.run([PTOUCH, "--info"], capture_output=True, text=True, timeout=15)
    except Exception as e:  # noqa: BLE001
        return False, {"error": str(e)}
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        return False, {"error": out.strip() or f"ptouch-print exited {r.returncode}"}
    tape = re.search(r"media width = (\d+) mm", out)
    maxpx = re.search(r"maximum printing width for this tape is (\d+)px", out)
    err = re.search(r"error = 0x([0-9a-fA-F]+)", out)
    info = {
        "printer": "PT-P710BT",
        "tapeMm": int(tape.group(1)) if tape else None,
        "maxPx": int(maxpx.group(1)) if maxpx else None,
    }
    if err and int(err.group(1), 16) != 0:
        info["error"] = f"printer error 0x{err.group(1)}"
        return False, info
    return True, info


def printer_info_settled(attempts: int = 4, delay: float = 3.0):
    """printer_info() that tolerates the post-job settling window.

    For several seconds after finishing a job the Cube stops answering the status
    query ("timeout (1 sec) while waiting for status response"), which is what
    broke back-to-back batch prints: label 2's pre-print check read that as a dead
    printer. Only a status read that never succeeds across the whole window is a
    real failure. A *printer error* (readable cassette, non-zero error register)
    is returned immediately — it's a real state, not a flaky read.
    """
    ok, info = printer_info()
    for _ in range(attempts - 1):
        if ok or info.get("maxPx"):
            break
        time.sleep(delay)
        ok, info = printer_info()
    return ok, info


def wait_for_settle():
    """Sleep out whatever remains of the post-print settling window."""
    remaining = SETTLE_SECONDS - (time.time() - LAST_PRINT_DONE[0])
    if remaining > 0:
        time.sleep(min(SETTLE_SECONDS, remaining))


def prepare_image(data_url: str, max_px: int) -> Image.Image:
    if "," in data_url:
        data_url = data_url.split(",", 1)[1]
    img = Image.open(io.BytesIO(base64.b64decode(data_url)))
    img = img.convert("L")
    if img.height != max_px:
        w = max(1, round(img.width * max_px / img.height))
        img = img.resize((w, max_px), Image.LANCZOS)
    return img.point(lambda p: 0 if p < THRESHOLD else 255, mode="1")


def label_length_mm(label_px: int) -> float:
    """Length of one label along the tape, from the raster actually sent."""
    return label_px * 25.4 / DPI


def estimate_print_seconds(page_px: list) -> float:
    """Physical time for a run of pages, each `page_px` wide along the tape."""
    feed = sum(label_length_mm(px) for px in page_px) / TAPE_MM_PER_S
    return feed + 1.2 * len(page_px) + LEADER_MM / TAPE_MM_PER_S + 1.5  # cuts + leader + margin


def _group_consecutive(paths: list):
    """[a, a, b] -> [(a, 2), (b, 1)] — the fallback's --copies groupings."""
    groups = []
    for p in paths:
        if groups and groups[-1][0] == p:
            groups[-1][1] += 1
        else:
            groups.append([p, 1])
    return groups


def run_print_job(paths: list, precut: bool = True):
    """Stream one job. `paths` is the page sequence, already expanded for copies.

    With a patched ptouch-print this is a single invocation: every page after the
    first is introduced by --page, so the run costs one blank leader in total.
    Without the patch, fall back to one invocation per distinct label (grouping
    consecutive duplicates into --copies), which prints the same labels but feeds
    a leader per group.

    Returns (ok, error_message, leaders) — leaders is how many blank leaders the
    run actually cost, for the tape accounting in the reply.
    """
    if HAS_PAGE:
        cmd = [PTOUCH]
        if precut:
            cmd.append("--precut")
        for p in paths:
            cmd += ["--image", p, "--page"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30 + 10 * len(paths))
        except subprocess.TimeoutExpired:
            return False, "print timed out", 1
        if r.returncode != 0:
            return False, (r.stdout + r.stderr).strip() or f"ptouch-print exited {r.returncode}", 1
        return True, "", 1

    groups = _group_consecutive(paths)
    for path, copies in groups:
        cmd = [PTOUCH, "--copies", str(copies), "--image", path]
        if precut:
            cmd.insert(1, "--precut")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30 + 10 * copies)
        except subprocess.TimeoutExpired:
            return False, "print timed out", len(groups)
        if r.returncode != 0:
            return False, (r.stdout + r.stderr).strip() or f"ptouch-print exited {r.returncode}", len(groups)
    return True, "", len(groups)


def verify_after_print(est_seconds: float):
    """Wait out the physical print, then read the printer's error register.

    Returns (ok, info). ok=False means the printer aborted the job (red LED).
    """
    time.sleep(min(70.0, est_seconds))
    # A flaky/timed-out status read isn't proof of a failed print — keep looking
    # until the printer answers, so we never resend a label that did print.
    return printer_info_settled()


def keepalive_loop():
    """Poke the printer with a status read so its auto-power-off timer never expires.

    Deliberately conservative: it takes PRINT_LOCK non-blockingly, so a tick during
    a print job is dropped rather than queued (a status read mid-job aborts it), and
    a tick is skipped entirely if any other USB traffic happened recently.
    """
    while True:
        time.sleep(min(60, KEEPALIVE_SECONDS))
        if time.time() - LAST_USB_ACTIVITY < KEEPALIVE_SECONDS:
            continue
        if not PRINT_LOCK.acquire(blocking=False):
            KEEPALIVE_STATE["skipped"] += 1
            continue
        try:
            was = KEEPALIVE_STATE["ok"]
            ok, info = printer_info()
            reachable = ok or bool(info.get("maxPx"))
            KEEPALIVE_STATE["last"] = time.time()
            KEEPALIVE_STATE["ok"] = reachable
            if ok:
                LAST_INFO.update(info)
            # Printer off or unplugged is normal; log only on transitions so the
            # journal doesn't fill up with one line per tick overnight.
            if reachable != was:
                sys.stderr.write(
                    f"keepalive: printer {'reachable' if reachable else 'unreachable'}"
                    f"{'' if reachable else ' (' + str(info.get('error')) + ')'}\n"
                )
                sys.stderr.flush()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"keepalive error: {e}\n")
            sys.stderr.flush()
        finally:
            PRINT_LOCK.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "wms-print-bridge/1.6"

    def _send(self, code: int, payload: dict):
        if code >= 400 or self.path.startswith("/print"):
            sys.stderr.write(f"{self.command} {self.path} -> {code} {json.dumps(payload)[:300]}\n")
            sys.stderr.flush()
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path != "/health":
            return self._send(404, {"error": "not found"})
        if time.time() - LAST_PRINT_DONE[0] < SETTLE_SECONDS:
            # Still in the post-job settling window: a status read here would time
            # out and look like a dead printer. It just printed — report last info.
            return self._send(200, {"ok": True, "busy": True, "batchPages": HAS_PAGE, **LAST_INFO})
        if not PRINT_LOCK.acquire(timeout=5):
            # A print job is in progress — the printer is clearly alive; answer
            # from the last known info rather than colliding on the USB bus.
            return self._send(200, {"ok": True, "busy": True, "batchPages": HAS_PAGE, **LAST_INFO})
        try:
            ok, info = printer_info()
            # A recoverable printer-error state (red LED) still counts as available:
            # the next print request clears it automatically. Only report down when
            # the printer/tape can't be seen at all.
            if not ok and info.get("maxPx"):
                ok = True
                info["warning"] = info.pop("error", None)
            if ok:
                LAST_INFO.update(info)
        finally:
            PRINT_LOCK.release()
        if KEEPALIVE_SECONDS > 0:
            info["keepalive"] = {
                "everySeconds": KEEPALIVE_SECONDS,
                "lastAgo": round(time.time() - KEEPALIVE_STATE["last"]) if KEEPALIVE_STATE["last"] else None,
                "ok": KEEPALIVE_STATE["ok"],
            }
        # batchPages tells the caller whether /print-batch really costs one leader
        # or is silently degrading to one job per label.
        return self._send(200 if ok else 503, {"ok": ok, "batchPages": HAS_PAGE, **info})

    def do_POST(self):  # noqa: N802
        if self.path not in ("/print", "/print-batch"):
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self._send(413, {"error": "bad body size"})
        try:
            req = json.loads(self.rfile.read(length))
        except Exception:  # noqa: BLE001
            return self._send(400, {"error": "invalid request"})

        # Both endpoints reduce to the same thing: an ordered list of labels, each
        # with a copy count. /print is the one-label case.
        try:
            if self.path == "/print":
                labels = [{"imageDataUrl": req["imageDataUrl"], "copies": req.get("copies", 1)}]
            else:
                labels = req["labels"]
                if not isinstance(labels, list) or not labels:
                    raise ValueError("labels must be a non-empty array")
            labels = [
                {
                    "imageDataUrl": lb["imageDataUrl"],
                    "copies": min(MAX_COPIES, max(1, int(lb.get("copies", 1)))),
                }
                for lb in labels
            ]
        except Exception:  # noqa: BLE001
            return self._send(400, {"error": "invalid request"})

        total_pages = sum(lb["copies"] for lb in labels)
        if total_pages > MAX_PAGES:
            return self._send(400, {"error": f"too many pages ({total_pages} > {MAX_PAGES})"})

        with PRINT_LOCK:
            wait_for_settle()
            ok, info = printer_info_settled()
            # A lingering "printer error" state (red LED, typically 0x0100 after a
            # failed precut job) is recoverable: a plain job clears it and prints.
            # Only a hard failure (no USB device / no tape info) is a real 503.
            stuck_error = bool(not ok and info.get("maxPx"))
            if not info.get("maxPx"):
                return self._send(503, {"error": info.get("error", "printer unavailable"), **info})
            LAST_INFO.update({k: v for k, v in info.items() if k != "error"})

            # Render every label once, then expand copies by repeating its page —
            # ptouch-print pages are just a sequence, so N copies is N pages and
            # needs no --copies flag.
            paths = []
            page_px = []
            labels_mm = []
            tmpdir = tempfile.mkdtemp(prefix="wms-print-")
            try:
                try:
                    for i, lb in enumerate(labels):
                        img = prepare_image(lb["imageDataUrl"], info["maxPx"])
                        path = os.path.join(tmpdir, f"label-{i}.png")
                        img.save(path, "PNG")
                        labels_mm.append(round(label_length_mm(img.width), 1))
                        for _ in range(lb["copies"]):
                            paths.append(path)
                            page_px.append(img.width)
                except Exception as e:  # noqa: BLE001
                    return self._send(400, {"error": f"bad image: {e}"})

                est = estimate_print_seconds(page_px)
                sys.stderr.write(
                    f"printing {len(labels)} label{'' if len(labels) == 1 else 's'} "
                    f"as {len(paths)} page{'' if len(paths) == 1 else 's'} "
                    f"({'one job' if HAS_PAGE else 'no --page support: one job per label'}), "
                    f"tape {info.get('tapeMm')}mm, ~{est:.0f}s\n"
                )
                sys.stderr.flush()
                try:
                    retried = False
                    attempts = 1
                    if stuck_error:
                        sys.stderr.write(f"printer in error state ({info.get('error')}); attempting print anyway\n")
                        sys.stderr.flush()
                    ok, err, leaders = run_print_job(paths)
                    if ok:
                        ok, post = verify_after_print(est)
                        err = post.get("error", "printer aborted the job")
                    if not ok:
                        sys.stderr.write(f"print aborted ({err}); retrying once\n")
                        sys.stderr.flush()
                        retried = True
                        attempts = 2
                        time.sleep(3)
                        ok, err, leaders = run_print_job(paths)
                        if ok:
                            ok, post = verify_after_print(est)
                            err = post.get("error", "printer aborted the job")
                    if not ok:
                        # 0x0100 with a readable cassette is almost always contention:
                        # the Cube refuses USB jobs while the Brother phone app holds
                        # a Bluetooth session (root-caused 2026-07-21).
                        if "0x0100" in err:
                            err += " — if the Brother phone app is connected via Bluetooth, close it and retry"
                        return self._send(500, {"error": f"print failed: {err}"})
                finally:
                    LAST_PRINT_DONE[0] = time.time()
            finally:
                for name in os.listdir(tmpdir):
                    os.unlink(os.path.join(tmpdir, name))
                os.rmdir(tmpdir)

        # Geometry of what was actually printed. tapeUsedMm covers the delivered
        # job only — a retried job burned roughly another leader-plus-labels that
        # is not counted here, so treat the total as a floor when attempts > 1.
        tape_used = sum(label_length_mm(px) for px in page_px) + LEADER_MM * leaders
        if self.path == "/print":
            payload = {
                "ok": True,
                "copies": labels[0]["copies"],
                "tapeMm": info.get("tapeMm"),
                "labelMm": labels_mm[0],
                "tapeUsedMm": round(tape_used, 1),
                "rasterPx": [page_px[0], info["maxPx"]],
                "attempts": attempts,
            }
        else:
            payload = {
                "ok": True,
                "labels": len(labels),
                "pages": len(paths),
                "leaders": leaders,
                "tapeMm": info.get("tapeMm"),
                "labelsMm": labels_mm,
                "tapeUsedMm": round(tape_used, 1),
                "attempts": attempts,
            }
        if retried:
            payload["retried"] = True
        return self._send(200, payload)

    def log_message(self, fmt, *args):  # quiet default access log
        pass


if __name__ == "__main__":
    sys.stderr.write(
        f"batch pages: {'supported' if HAS_PAGE else 'NOT supported by ' + PTOUCH}"
        f"{'' if HAS_PAGE else ' — apply patches/ptouch-print-batch-pages.patch to stop wasting a leader per label'}\n"
    )
    if KEEPALIVE_SECONDS > 0:
        sys.stderr.write(f"keepalive: status read every {KEEPALIVE_SECONDS}s\n")
    sys.stderr.flush()
    if KEEPALIVE_SECONDS > 0:
        threading.Thread(target=keepalive_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

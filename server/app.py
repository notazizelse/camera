#!/usr/bin/env python3
"""Kitchen queue server.

Zero third-party dependencies: it runs on a stock Python 3.11+ install, on the
same Raspberry Pi as the counter or on a $5 VPS.

Data flow:
    edge counter --HTTPS POST--> /api/ingest --> Store --> SSE /api/stream
                                                       --> GET /api/state etc.

The edge device only ever makes *outbound* connections, which is what makes
this work behind a school firewall without any port forwarding.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import queue
import secrets
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from media import MediaRegistry
from store import Store

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(os.path.dirname(ROOT), "web")

DEFAULTS = {
    "site_name": "School Kitchen",
    "queue_name": "Main counter",
    # Seconds it takes the till to serve one person. Measure this once with a
    # stopwatch at your own canteen; it is the single biggest factor in the
    # wait estimate being believable. Once there is enough history the server
    # replaces it with a rate measured from how fast the queue actually drains.
    "service_seconds_per_person": 11,
    # count -> level thresholds (upper bound of each level)
    "levels": [
        {"name": "No queue", "max": 2, "tone": "clear"},
        {"name": "Short", "max": 7, "tone": "good"},
        {"name": "Moderate", "max": 15, "tone": "warn"},
        {"name": "Long", "max": 28, "tone": "bad"},
        {"name": "Packed", "max": 10000, "tone": "worst"},
    ],
    "stale_after_seconds": 45,
    "offline_after_seconds": 300,
    "snapshot_enabled": False,
    "open_hours": {"start": "07:30", "end": "16:00"},
    # Live video is a separate decision from the count, and a separate
    # audience: the count is public, the picture is not. See docs/forwarding.md.
    "video_enabled": True,
    "video_note": "Live view is limited to staff and students with the access code.",
    # Cameras known before anything connects. A pusher holding the device key
    # can add more at runtime; a viewer never can.
    "cameras": [{"id": "cafeteria", "label": "Cafeteria queue"}],
}


class Hub:
    """Fan-out of state updates to connected browsers over SSE."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: set[queue.Queue] = set()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=16)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, payload: dict) -> None:
        data = json.dumps(payload, separators=(",", ":"))
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass  # slow client; it resyncs on its next poll

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._subs)


class App:
    def __init__(self, config: dict, store: Store, device_key: str,
                 view_code: str | None = None):
        self.config = config
        self.store = store
        self.device_key = device_key
        self.hub = Hub()
        self.snapshot: tuple[float, bytes] | None = None
        self.cameras = MediaRegistry(config.get("cameras"))

        # None means live video is switched off entirely - the safe default,
        # so an operator who never thought about who can watch never
        # accidentally publishes a cafeteria to the open internet.
        self.view_code = view_code
        self._gate_secret = hashlib.sha256(
            f"{device_key}:{view_code}".encode()
        ).digest()
        self._failures: dict[str, list[float]] = {}
        self._fail_lock = threading.Lock()

    # ---------- viewer gate ----------

    @property
    def video_on(self) -> bool:
        return bool(self.config.get("video_enabled")) and self.view_code is not None

    def viewer_token(self) -> str:
        return hmac.new(self._gate_secret, b"viewer", hashlib.sha256).hexdigest()

    def check_code(self, code: str, remote: str) -> bool:
        """Constant-time check with a crude per-IP lockout.

        The access code is short enough to type on a phone, which means it is
        short enough to guess at speed. Ten wrong tries buys a cool-off.
        """
        now = time.time()
        with self._fail_lock:
            recent = [t for t in self._failures.get(remote, []) if now - t < 300]
            self._failures[remote] = recent
            if len(recent) >= 10:
                return False
        ok = secrets.compare_digest(code, self.view_code or "")
        if not ok:
            with self._fail_lock:
                self._failures.setdefault(remote, []).append(now)
        return ok

    def locked_out(self, remote: str) -> bool:
        now = time.time()
        with self._fail_lock:
            return len([t for t in self._failures.get(remote, []) if now - t < 300]) >= 10

    # ---------- domain logic ----------

    def level_for(self, count: float) -> dict:
        for lvl in self.config["levels"]:
            if count <= lvl["max"]:
                return {"name": lvl["name"], "tone": lvl["tone"]}
        last = self.config["levels"][-1]
        return {"name": last["name"], "tone": last["tone"]}

    def service_seconds(self) -> tuple[float, float | None]:
        """Seconds per person, and the measured rate if one was usable.

        The measured rate only ever *refines* the stopwatch figure from the
        config - it is clamped to half..double it. An inference drawn from a
        handful of noisy samples should not be able to tell a student the wait
        is 40 minutes when the till has never taken longer than 10.
        """
        configured = float(self.config["service_seconds_per_person"])
        measured = self.store.service_rate_hint()
        if not measured or measured <= 0.2:
            return configured, None
        seconds = 60.0 / measured
        return min(max(seconds, configured * 0.5), configured * 2.0), measured

    def state(self) -> dict:
        latest = self.store.latest
        now = time.time()
        if latest is None:
            return {
                "status": "offline",
                "count": None,
                "wait_seconds": None,
                "level": {"name": "No data", "tone": "unknown"},
                "updated_at": None,
                "age_seconds": None,
                "trend": 0,
                "has_snapshot": False,
                "viewers": self.hub.count,
            }

        age = now - latest["ts"]
        if age > self.config["offline_after_seconds"]:
            status = "offline"
        elif age > self.config["stale_after_seconds"]:
            status = "stale"
        else:
            status = "live"

        count = float(latest.get("count", 0))
        service_seconds, measured = self.service_seconds()

        # Trend: compare the first and last third of the past ~3 minutes.
        window = self.store.recent(3)
        trend = 0
        third = len(window) // 3
        if third >= 2:
            head = sum(s["count"] for s in window[:third]) / third
            tail = sum(s["count"] for s in window[-third:]) / third
            if tail - head > 1.5:
                trend = 1
            elif head - tail > 1.5:
                trend = -1

        return {
            "status": status,
            "count": round(count),
            "wait_seconds": round(count * service_seconds),
            "service_seconds_per_person": round(service_seconds, 1),
            # The rate the wait estimate actually used, after clamping - showing
            # the raw measurement here would contradict the number beside it.
            "service_rate_effective": round(60.0 / service_seconds, 1) if measured else None,
            "level": self.level_for(count),
            "updated_at": latest["ts"],
            "age_seconds": round(age, 1),
            "trend": trend,
            "device": latest.get("device"),
            "has_snapshot": self.snapshot is not None,
            "viewers": self.hub.count,
        }

    def ingest(self, body: dict) -> dict:
        raw_count = body.get("count")
        if raw_count is None:
            raise ValueError("missing 'count'")
        count = float(raw_count)
        if not 0 <= count <= 5000:
            raise ValueError("'count' out of range")

        ts = float(body.get("ts") or time.time())
        # Reject clock-skewed samples rather than poisoning the history.
        if abs(ts - time.time()) > 3600:
            ts = time.time()

        sample = {
            "ts": ts,
            "count": count,
            "raw": body.get("raw"),
            "device": str(body.get("device", "edge"))[:40],
            "fps": body.get("fps"),
        }
        self.store.add(sample)

        img = body.get("image_jpeg_b64")
        if img and self.config["snapshot_enabled"]:
            try:
                self.snapshot = (ts, base64.b64decode(img))
            except (ValueError, TypeError):
                pass

        state = self.state()
        self.hub.publish(state)
        return state


# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "queue/1.0"
    protocol_version = "HTTP/1.1"
    app: App = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):
        if self.path.startswith("/api/ingest"):
            return  # one line every few seconds is just noise
        super().log_message(fmt, *args)

    # ---------- helpers ----------

    def _send_json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        """True for the pusher/counter, which holds the device key."""
        return secrets.compare_digest(
            self.headers.get("X-Device-Key", ""), self.app.device_key
        )

    def _viewer_ok(self) -> bool:
        """True for a browser that has entered the access code.

        The count is public; only the picture is behind this.
        """
        app = self.app
        if not app.video_on:
            return False
        if app.view_code == "open":
            return True
        wanted = app.viewer_token()
        for part in self.headers.get("Cookie", "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == "qv" and secrets.compare_digest(value, wanted):
                return True
        return False

    def _hls_name(self, path: str) -> str | None:
        """Segment/playlist name from the URL, rejecting anything path-like."""
        name = path.rsplit("/", 1)[-1]
        if not name or "/" in name or ".." in name or len(name) > 80:
            return None
        if not name.endswith((".m3u8", ".ts", ".m4s", ".mp4")):
            return None
        return name

    def _stream_headers(self, ctype: str) -> None:
        """Headers for a response with no length, ended by closing the socket."""
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    # ---------- routes ----------

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Device-Key")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _hls_target(self, path: str, create: bool):
        """Split /api/hls/<camera>/<name> into a relay and a file name."""
        rest = path[len("/api/hls/"):]
        if "/" not in rest:
            return None, None
        camera_id, _, name = rest.partition("/")
        if not MediaRegistry.valid(camera_id):
            return None, None
        return self.app.cameras.relay(camera_id, create=create), self._hls_name(name)

    def do_PUT(self):
        """ffmpeg pushes the playlist and each segment here."""
        path = urlparse(self.path).path
        if not path.startswith("/api/hls/"):
            return self._send_json({"error": "not found"}, 404)
        if not self._authorized():
            return self._send_json({"error": "bad device key"}, 401)

        relay, name = self._hls_target(path, create=True)
        if not relay or not name:
            return self._send_json({"error": "bad camera or segment name"}, 400)

        length = int(self.headers.get("Content-Length", 0))
        if length > 16_000_000:
            return self._send_json({"error": "segment too large"}, 413)
        data = self.rfile.read(length)

        if name.endswith(".m3u8"):
            relay.put_playlist(data)
        else:
            relay.put_segment(name, data)
        return self._send_json({"ok": True})

    def do_DELETE(self):
        """ffmpeg rolls old segments off the playlist and deletes them."""
        path = urlparse(self.path).path
        if not path.startswith("/api/hls/") or not self._authorized():
            return self._send_json({"error": "not found"}, 404)
        relay, name = self._hls_target(path, create=False)
        if relay and name:
            relay.drop_segment(name)
        return self._send_json({"ok": True})

    def do_POST(self):
        path = urlparse(self.path).path

        if path.startswith("/api/frame/"):
            if not self._authorized():
                return self._send_json({"error": "bad device key"}, 401)
            camera_id = path[len("/api/frame/"):]
            relay = (self.app.cameras.relay(camera_id, create=True)
                     if MediaRegistry.valid(camera_id) else None)
            if not relay:
                return self._send_json({"error": "bad camera id"}, 400)
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 4_000_000:
                return self._send_json({"error": "bad frame size"}, 400)
            relay.put_frame(self.rfile.read(length))
            return self._send_json({"ok": True})

        if path == "/api/access":
            return self._access()

        if path != "/api/ingest":
            return self._send_json({"error": "not found"}, 404)
        if not self._authorized():
            return self._send_json({"error": "bad device key"}, 401)
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 4_000_000:
                return self._send_json({"error": "payload too large"}, 413)
            body = json.loads(self.rfile.read(length) or b"{}")
            state = self.app.ingest(body)
        except ValueError as exc:
            return self._send_json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        return self._send_json({"ok": True, "state": state})

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        if path == "/api/config":
            return self._send_json(self.app.config)

        if path == "/api/state":
            return self._send_json(self.app.state())

        if path == "/api/history":
            minutes = min(int(query.get("minutes", ["120"])[0]), 1440)
            return self._send_json(
                {"minutes": minutes, "samples": self.app.store.recent(minutes)}
            )

        if path == "/api/pattern":
            weekday = query.get("weekday", [None])[0]
            wd = int(weekday) if weekday is not None else datetime.now().weekday()
            return self._send_json(
                {"weekday": wd, "buckets": self.app.store.pattern(wd)}
            )

        if path == "/api/snapshot.jpg":
            return self._snapshot()

        if path == "/api/stream":
            return self._sse()

        # Public: says which cameras exist and whether you may watch them.
        # Says nothing about what is in them.
        if path == "/api/media-state":
            return self._send_json({
                "enabled": self.app.video_on,
                "authorized": self._viewer_ok(),
                "note": self.app.config.get("video_note", ""),
                "default": self.app.cameras.default_id(),
                "cameras": self.app.cameras.listing(),
            })

        if path.startswith("/live/") or path.startswith("/api/live/"):
            if not self.app.video_on:
                return self._send_json({"error": "live video is disabled"}, 503)
            if not self._viewer_ok():
                return self._send_json({"error": "access code required"}, 403)
            if path.startswith("/api/live/"):
                target = path[len("/api/live/"):]
                camera_id, _, kind = target.rpartition(".")
                relay = self._viewer_relay(camera_id)
                if not relay:
                    return self._send_json({"error": "no such camera"}, 404)
                if kind == "jpg":
                    return self._live_jpg(relay)
                if kind == "mjpg":
                    return self._live_mjpg(relay)
                return self._send_json({"error": "not found"}, 404)
            return self._live_hls(path)

        return self._static(path)

    # ---------- live video ----------

    def _viewer_relay(self, camera_id: str):
        if not MediaRegistry.valid(camera_id):
            return None
        # create=False: a viewer must never be able to conjure a camera.
        return self.app.cameras.relay(camera_id, create=False)

    def _live_hls(self, path: str):
        rest = path[len("/live/"):]
        camera_id, _, raw_name = rest.partition("/")
        relay = self._viewer_relay(camera_id)
        name = self._hls_name(raw_name)
        if not relay or not name:
            return self._send_json({"error": "not found"}, 404)
        if name.endswith(".m3u8"):
            data = relay.get_playlist()
            ctype = "application/vnd.apple.mpegurl"
        else:
            data = relay.get_segment(name)
            ctype = "video/mp2t"
        if data is None:
            return self._send_json({"error": "not available"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _live_jpg(self, relay):
        frame = relay.get_frame()
        if not frame:
            return self._send_json({"error": "no frame"}, 404)
        ts, data = frame
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Age", str(round(time.time() - ts, 1)))
        self.end_headers()
        self.wfile.write(data)

    def _live_mjpg(self, relay):
        """An endless multipart response - plays in a plain <img> tag."""
        q = relay.subscribe_frames()
        self._stream_headers("multipart/x-mixed-replace; boundary=frame")
        current = relay.get_frame()
        try:
            if current:
                self._write_part(current[1])
            while True:
                try:
                    self._write_part(q.get(timeout=30))
                except queue.Empty:
                    break  # pusher stopped; let the browser reconnect
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            relay.unsubscribe_frames(q)

    def _write_part(self, jpeg: bytes) -> None:
        header = (
            "--frame\r\n"
            "Content-Type: image/jpeg\r\n"
            f"Content-Length: {len(jpeg)}\r\n\r\n"
        )
        self.wfile.write(header.encode() + jpeg + b"\r\n")
        self.wfile.flush()

    def _access(self):
        """Exchange the access code for a cookie."""
        app = self.app
        if not app.video_on:
            return self._send_json({"error": "live video is disabled"}, 503)
        remote = self.client_address[0]
        if app.locked_out(remote):
            return self._send_json(
                {"error": "too many attempts, try again in a few minutes"}, 429
            )
        try:
            length = int(self.headers.get("Content-Length", 0))
            code = json.loads(self.rfile.read(min(length, 4096)) or b"{}").get("code", "")
        except (ValueError, json.JSONDecodeError):
            return self._send_json({"error": "bad request"}, 400)

        if not app.check_code(str(code), remote):
            return self._send_json({"error": "that code is not right"}, 403)

        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Set-Cookie",
            f"qv={app.viewer_token()}; Path=/; Max-Age=43200; "
            f"HttpOnly; SameSite=Lax{secure}",
        )
        self.end_headers()
        self.wfile.write(body)

    def _snapshot(self):
        snap = self.app.snapshot
        if not snap or not self.app.config["snapshot_enabled"]:
            return self._send_json({"error": "no snapshot"}, 404)
        ts, data = snap
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Snapshot-Age", str(round(time.time() - ts, 1)))
        self.end_headers()
        self.wfile.write(data)

    def _sse(self):
        q = self.app.hub.subscribe()
        # No Content-Length and no chunked encoding, so the response is framed
        # by closing the socket - which HTTP/1.1 requires us to announce.
        # Without this some reverse proxies hold the stream open and buffer it.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(f"data: {json.dumps(self.app.state())}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    chunk = f"data: {q.get(timeout=20)}\n\n"
                except queue.Empty:
                    chunk = ": ping\n\n"  # keep-alive through proxies
                self.wfile.write(chunk.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.hub.unsubscribe(q)

    def _static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        target = os.path.normpath(os.path.join(WEB_DIR, path.lstrip("/")))
        if not target.startswith(WEB_DIR) or not os.path.isfile(target):
            return self._send_json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        with open(target, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)


class QueueServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """Closing a tab mid-stream is normal, not an incident.

        Every SSE client that navigates away aborts its connection, and the
        default handler prints a full traceback for each one. Left alone that
        buries real errors in the log.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def load_config(path: str | None) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser(description="Kitchen queue server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument(
        "--data", default=os.path.join(os.path.dirname(ROOT), "data", "samples.jsonl")
    )
    ap.add_argument("--retention-days", type=int, default=28)
    args = ap.parse_args()

    device_key = os.environ.get("DEVICE_KEY")
    if not device_key:
        device_key = "dev-key-change-me"
        print("WARNING: DEVICE_KEY not set, using 'dev-key-change-me' (development only)")

    # Live video is off unless someone decides who may watch it. VIEW_CODE=open
    # turns the gate off deliberately; leaving it unset turns video off.
    view_code = os.environ.get("VIEW_CODE")

    config = load_config(args.config)
    store = Store(args.data, args.retention_days)
    Handler.app = App(config, store, device_key, view_code)

    if not config.get("video_enabled"):
        print("live video: disabled in config.json")
    elif view_code is None:
        print("live video: OFF (set VIEW_CODE to a code, or 'open' for no gate)")
    elif view_code == "open":
        print("live video: ON with NO access code - anyone with the URL can watch")
    else:
        print("live video: ON, gated by access code")

    httpd = QueueServer((args.host, args.port), Handler)
    print(f"queue server listening on http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")


if __name__ == "__main__":
    main()

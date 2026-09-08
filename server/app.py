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
    def __init__(self, config: dict, store: Store, device_key: str):
        self.config = config
        self.store = store
        self.device_key = device_key
        self.hub = Hub()
        self.snapshot: tuple[float, bytes] | None = None

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
        return secrets.compare_digest(
            self.headers.get("X-Device-Key", ""), self.app.device_key
        )

    # ---------- routes ----------

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Device-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if urlparse(self.path).path != "/api/ingest":
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

        return self._static(path)

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
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
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

    config = load_config(args.config)
    store = Store(args.data, args.retention_days)
    Handler.app = App(config, store, device_key)

    httpd = QueueServer((args.host, args.port), Handler)
    print(f"queue server listening on http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")


if __name__ == "__main__":
    main()

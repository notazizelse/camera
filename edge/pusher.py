#!/usr/bin/env python3
"""Forward the cafeteria camera to the queue website.

Runs on the PC that can already see the footage. It reads the camera and
pushes the stream *outbound* to the server over ordinary HTTP(S), so the
school firewall needs no changes: nothing listens, nothing is forwarded in,
and the PC keeps no recording.

    python pusher.py --test                 # prove the chain with a test pattern
    python pusher.py --pick                 # find the camera's RTSP URL
    python pusher.py                        # run for real

Three ways to send, chosen with --mode:

    hls       ffmpeg uploads a rolling playlist and 2-second segments.
              ~6-8s behind live. Plays in every browser. The default.

    snapshot  one JPEG every couple of seconds. A few KB each, survives the
              most restrictive proxy, and for judging a queue length it is
              honestly hard to beat.

    rtmp      push to a MediaMTX/nginx-rtmp you run yourself, when you want
              sub-second latency and can open port 1935 outbound.

Three ways to read the camera, chosen with --source:

    rtsp://user:pass@nvr/...   straight from the NVR - always prefer this
    screen                     capture the whole desktop
    window:Camera Viewer       capture one window by title (Windows only),
                               for NVR software that will not give up an
                               RTSP URL
    dshow:Integrated Camera    a locally attached camera
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse, urlunparse

ROOT = os.path.dirname(os.path.abspath(__file__))
IS_WINDOWS = platform.system() == "Windows"

DEFAULT_CONFIG = {
    "source": "rtsp://user:pass@10.0.12.40:554/Streaming/Channels/102",
    "server_url": "https://queue.yourschool.example",
    "device_key": "CHANGE_ME_LONG_RANDOM_STRING",
    "mode": "hls",
    # Leave transcode off if you can: copying the camera's existing H.264 uses
    # almost no CPU, so the cafeteria PC stays usable. Turn it on only if the
    # stream is too big to upload or the browser refuses to play it.
    "transcode": False,
    "height": 360,          # only used when transcoding, or in snapshot mode
    "bitrate": "700k",
    "segment_seconds": 2,
    "snapshot_interval": 2.0,
    "snapshot_quality": 6,  # ffmpeg -q:v, 2 = best, 31 = worst
    "rtmp_url": "rtmp://your-server:1935/live/cafeteria",
    "ffmpeg": "ffmpeg",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def load_config(path: str) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    return cfg


def redact(url: str) -> str:
    """Never print camera credentials - these logs get pasted into emails."""
    parsed = urlparse(url)
    if not parsed.password:
        return url
    netloc = f"{parsed.username}:***@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def input_args(source: str, cfg: dict) -> list[str]:
    """ffmpeg input flags for each kind of source."""
    if source == "test":
        return ["-re", "-f", "lavfi", "-i",
                "testsrc2=size=640x360:rate=12,format=yuv420p"]

    if source == "screen":
        if IS_WINDOWS:
            return ["-f", "gdigrab", "-framerate", "10", "-i", "desktop"]
        if platform.system() == "Darwin":
            return ["-f", "avfoundation", "-framerate", "10", "-i", "1"]
        return ["-f", "x11grab", "-framerate", "10", "-i", os.environ.get("DISPLAY", ":0")]

    if source.startswith("window:"):
        if not IS_WINDOWS:
            sys.exit("window: capture is Windows-only; use 'screen' instead")
        # The fallback for NVR software that hides its RTSP URL: point ffmpeg
        # at the viewer window itself. Title must match exactly.
        return ["-f", "gdigrab", "-framerate", "10", "-i", f"title={source[7:]}"]

    if source.startswith("dshow:"):
        return ["-f", "dshow", "-i", f"video={source[6:]}"]

    # RTSP over TCP: UDP loses packets on school wifi and the picture tears.
    # No -stimeout here on purpose: the option was renamed between ffmpeg
    # versions and guessing wrong makes ffmpeg refuse to start. The watchdog
    # below is a better answer anyway - it notices every way a stream can
    # die, including the ones where ffmpeg sits there looking healthy.
    return ["-rtsp_transport", "tcp", "-i", source]


def video_args(cfg: dict, for_hls: bool) -> list[str]:
    if not cfg["transcode"]:
        return ["-c:v", "copy"]
    args = [
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-profile:v", "main", "-pix_fmt", "yuv420p",
        "-vf", f"scale=-2:{cfg['height']}",
        "-b:v", cfg["bitrate"], "-maxrate", cfg["bitrate"],
        "-bufsize", cfg["bitrate"].replace("k", "") + "000",
    ]
    if for_hls:
        # Keyframe every segment, or the player stalls at segment boundaries.
        fps = 12
        args += ["-r", str(fps), "-g", str(fps * cfg["segment_seconds"]),
                 "-sc_threshold", "0"]
    return args


def build_command(cfg: dict, source: str) -> list[str]:
    base = cfg["server_url"].rstrip("/")
    ff = [cfg["ffmpeg"], "-hide_banner", "-loglevel", "warning", "-nostdin"]
    ff += input_args(source, cfg)
    # Audio is dropped, always. A recording of what people say in a queue is a
    # far bigger intrusion than a picture of the queue, and nobody needs it.
    ff += ["-an"]

    if cfg["mode"] == "rtmp":
        return ff + video_args(cfg, False) + ["-f", "flv", cfg["rtmp_url"]]

    if cfg["mode"] == "snapshot":
        return ff + [
            "-vf", f"fps=1/{cfg['snapshot_interval']},scale=-2:{cfg['height']}",
            "-f", "image2pipe", "-c:v", "mjpeg",
            "-q:v", str(cfg["snapshot_quality"]), "-",
        ]

    return ff + video_args(cfg, True) + [
        "-f", "hls",
        "-hls_time", str(cfg["segment_seconds"]),
        "-hls_list_size", "6",
        "-hls_flags", "delete_segments+omit_endlist+independent_segments",
        "-hls_allow_cache", "0",
        "-hls_segment_type", "mpegts",
        "-hls_segment_filename", f"{base}/api/hls/seg%03d.ts",
        "-method", "PUT",
        # One connection per segment. Slightly less efficient than persistent
        # connections, and considerably less likely to wedge behind a proxy.
        "-http_persistent", "0",
        "-headers", f"X-Device-Key: {cfg['device_key']}\r\n",
        f"{base}/api/hls/stream.m3u8",
    ]


# ---------------------------------------------------------------------------
# snapshot mode
# ---------------------------------------------------------------------------


class Watchdog(threading.Thread):
    """Restart ffmpeg when the *server* stops seeing media.

    ffmpeg can look perfectly alive while nothing arrives: the NVR stops
    sending, the wifi black-holes, a proxy swallows the uploads. Asking the
    far end "are you actually receiving this?" catches all of those, which
    no local timeout does.
    """

    def __init__(self, server_url: str, proc: subprocess.Popen,
                 grace: float = 45.0, stale_after: float = 30.0):
        super().__init__(daemon=True)
        self.url = server_url.rstrip("/") + "/api/media-state"
        self.proc = proc
        self.grace = grace
        self.stale_after = stale_after
        self.running = True
        self.tripped = False

    def run(self):
        started = time.time()
        misses = 0
        while self.running and self.proc.poll() is None:
            time.sleep(10)
            if time.time() - started < self.grace:
                continue
            try:
                with urllib.request.urlopen(self.url, timeout=8) as resp:
                    state = json.load(resp)
            except (urllib.error.URLError, OSError, ValueError):
                continue  # the check itself failed; do not blame the stream

            age = state.get("age_seconds")
            fresh = state.get("available") and age is not None and age < self.stale_after
            misses = 0 if fresh else misses + 1
            if misses >= 3:
                print(f"  watchdog: server has seen nothing for ~{age}s, restarting ffmpeg")
                self.tripped = True
                self.proc.terminate()
                return


class FrameUploader(threading.Thread):
    """POSTs JPEGs, dropping old ones rather than falling behind."""

    def __init__(self, url: str, key: str):
        super().__init__(daemon=True)
        self.url = url
        self.key = key
        self.q: queue.Queue = queue.Queue(maxsize=2)
        self.running = True
        self.sent = 0

    def send(self, jpeg: bytes) -> None:
        try:
            self.q.put_nowait(jpeg)
        except queue.Full:
            try:
                self.q.get_nowait()
            except queue.Empty:
                pass
            # A stale frame of a queue is worthless - always prefer the newest.
            self.q.put_nowait(jpeg)

    def run(self):
        while self.running:
            try:
                jpeg = self.q.get(timeout=1)
            except queue.Empty:
                continue
            req = urllib.request.Request(
                self.url, data=jpeg,
                headers={"Content-Type": "image/jpeg", "X-Device-Key": self.key},
            )
            try:
                with urllib.request.urlopen(req, timeout=10):
                    self.sent += 1
            except (urllib.error.URLError, OSError) as exc:
                print(f"  frame upload failed: {exc}")


def pump_snapshots(proc: subprocess.Popen, uploader: FrameUploader) -> None:
    """Split ffmpeg's MJPEG stdout into individual JPEGs."""
    buf = bytearray()
    while True:
        chunk = proc.stdout.read(65536)
        if not chunk:
            return
        buf += chunk
        while True:
            start = buf.find(b"\xff\xd8")
            if start < 0:
                buf.clear()
                break
            end = buf.find(b"\xff\xd9", start + 2)
            if end < 0:
                del buf[:start]     # keep the partial frame, drop the junk
                break
            uploader.send(bytes(buf[start:end + 2]))
            del buf[:end + 2]


# ---------------------------------------------------------------------------
# supervision
# ---------------------------------------------------------------------------


def drain_stderr(proc: subprocess.Popen) -> None:
    for line in iter(proc.stderr.readline, b""):
        text = line.decode("utf-8", "replace").rstrip()
        if text:
            print(f"  ffmpeg: {text}")


def run_forever(cfg: dict, source: str, once: bool = False) -> None:
    if not shutil.which(cfg["ffmpeg"]):
        sys.exit(
            f"ffmpeg not found (looked for {cfg['ffmpeg']!r}).\n"
            "  Windows: winget install Gyan.FFmpeg\n"
            "  Debian:  sudo apt install ffmpeg\n"
            "  macOS:   brew install ffmpeg"
        )

    uploader = None
    if cfg["mode"] == "snapshot":
        uploader = FrameUploader(
            cfg["server_url"].rstrip("/") + "/api/frame", cfg["device_key"]
        )
        uploader.start()

    backoff = 2.0
    while True:
        command = build_command(cfg, source)
        print(f"starting {cfg['mode']} push from {redact(source)}")
        started = time.time()

        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE if cfg["mode"] == "snapshot" else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        threading.Thread(target=drain_stderr, args=(proc,), daemon=True).start()

        watchdog = None
        if cfg["mode"] != "rtmp":
            watchdog = Watchdog(cfg["server_url"], proc)
            watchdog.start()

        try:
            if cfg["mode"] == "snapshot":
                pump_snapshots(proc, uploader)
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            raise
        finally:
            if watchdog:
                watchdog.running = False

        ran_for = time.time() - started
        print(f"ffmpeg exited ({proc.returncode}) after {ran_for:.0f}s")
        if once:
            return

        # A stream that ran fine for a while probably hit a blip; reconnect at
        # once. One that dies immediately is misconfigured, so back off and
        # stop hammering the NVR. A watchdog restart is always a blip.
        healthy = ran_for > 30 or (watchdog and watchdog.tripped)
        backoff = 2.0 if healthy else min(backoff * 2, 60)
        print(f"reconnecting in {backoff:.0f}s")
        time.sleep(backoff)


# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Forward a camera to the queue website",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Three ways to send")[0],
    )
    ap.add_argument("--config", default=os.path.join(ROOT, "pusher.json"))
    ap.add_argument("--source", help="rtsp URL, 'screen', 'window:Title', 'dshow:Name'")
    ap.add_argument("--mode", choices=["hls", "snapshot", "rtmp"])
    ap.add_argument("--server", help="base URL of the queue server")
    ap.add_argument("--key", help="device key (matches the server's DEVICE_KEY)")
    ap.add_argument("--transcode", action="store_true",
                    help="re-encode instead of copying the camera's stream")
    ap.add_argument("--test", action="store_true",
                    help="push a test pattern - proves the chain with no camera")
    ap.add_argument("--pick", action="store_true",
                    help="search the network for the camera's RTSP URL")
    ap.add_argument("--print-command", action="store_true",
                    help="show the ffmpeg command and exit")
    ap.add_argument("--once", action="store_true", help="do not restart ffmpeg")
    args = ap.parse_args()

    if args.pick:
        sys.path.insert(0, ROOT)
        import discover

        return discover.main([])

    cfg = load_config(args.config)
    if args.mode:
        cfg["mode"] = args.mode
    if args.server:
        cfg["server_url"] = args.server
    if args.key:
        cfg["device_key"] = args.key
    if args.transcode:
        cfg["transcode"] = True

    source = args.source or cfg["source"]
    if args.test:
        source = "test"
        # A test pattern has no H.264 to copy.
        cfg["transcode"] = True

    if args.print_command:
        print(" ".join(redact(part) for part in build_command(cfg, source)))
        return

    if cfg["device_key"] == DEFAULT_CONFIG["device_key"]:
        sys.exit("set a real device_key in pusher.json (must match the server's DEVICE_KEY)")

    try:
        run_forever(cfg, source, once=args.once)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    main()

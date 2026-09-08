#!/usr/bin/env python3
"""Pretend to be ffmpeg pushing HLS, without needing ffmpeg or a camera.

Exercises exactly the requests the real pusher makes - rolling PUTs of a
playlist and its segments, plus DELETEs as segments age out - so you can test
the server, the access gate and the deployment before any camera exists.

    python scripts/fake_stream.py --url http://127.0.0.1:8080 --key KEY

The bytes are not real video, so a player will not decode them. This checks
the plumbing, not the picture.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request

SEGMENT_BYTES = 188 * 400  # roughly one MPEG-TS packet run


def request(url: str, method: str, key: str, data: bytes | None = None) -> int:
    req = urllib.request.Request(
        url, data=data, method=method, headers={"X-Device-Key": key}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError) as exc:
        print(f"  {method} {url} failed: {exc}")
        return 0


def playlist(seq: int, window: list[int], duration: int) -> bytes:
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{duration}",
        f"#EXT-X-MEDIA-SEQUENCE:{seq}",
        "#EXT-X-INDEPENDENT-SEGMENTS",
    ]
    for n in window:
        lines += [f"#EXTINF:{duration}.0,", f"seg{n:03d}.ts"]
    return ("\n".join(lines) + "\n").encode()


def main() -> None:
    ap = argparse.ArgumentParser(description="Fake HLS pusher")
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--key", default=os.environ.get("DEVICE_KEY", "dev-key-change-me"))
    ap.add_argument("--duration", type=int, default=2, help="segment seconds")
    ap.add_argument("--window", type=int, default=6, help="segments in the playlist")
    ap.add_argument("--count", type=int, default=0, help="stop after N segments (0 = forever)")
    args = ap.parse_args()

    base = args.url.rstrip("/") + "/api/hls"
    live: list[int] = []
    n = 0
    print(f"pushing fake segments to {base} every {args.duration}s")

    try:
        while args.count == 0 or n < args.count:
            body = bytes([0x47]) + os.urandom(SEGMENT_BYTES - 1)
            status = request(f"{base}/seg{n:03d}.ts", "PUT", args.key, body)
            live.append(n)

            while len(live) > args.window:
                old = live.pop(0)
                request(f"{base}/seg{old:03d}.ts", "DELETE", args.key)

            seq = live[0]
            pl = request(f"{base}/stream.m3u8", "PUT", args.key,
                         playlist(seq, live, args.duration))
            print(f"  seg{n:03d}.ts -> {status}   playlist -> {pl}   "
                  f"window={len(live)}")
            n += 1
            time.sleep(args.duration)
    except KeyboardInterrupt:
        print("\nstopped")

    try:
        with urllib.request.urlopen(args.url.rstrip("/") + "/api/media-state", timeout=5) as r:
            print("server sees:", json.load(r))
    except (urllib.error.URLError, OSError):
        pass


if __name__ == "__main__":
    main()

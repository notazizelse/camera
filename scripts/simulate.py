#!/usr/bin/env python3
"""Fake edge device.

Two jobs:
  --backfill N   write N days of plausible history straight into the store,
                 so the "typical for this time" chart has something to show
  --live         behave like a real counter and POST every few seconds

Useful for building the UI, for demoing the site to staff before a single
camera is mounted, and for load-testing the server.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))
from store import Store  # noqa: E402

# (start_minute, end_minute, peak_count) for a typical school day
RUSHES = [
    (7 * 60 + 40, 8 * 60 + 20, 6),    # before first period
    (10 * 60 + 25, 10 * 60 + 55, 14),  # morning break
    (12 * 60 + 0, 13 * 60 + 10, 30),   # lunch
    (15 * 60 + 20, 15 * 60 + 50, 5),   # end of day
]


def expected_count(when: datetime) -> float:
    """Smooth demand curve with a sharp leading edge, like a real bell rings."""
    if when.weekday() >= 5:
        return 0.0
    minute = when.hour * 60 + when.minute + when.second / 60
    total = 0.0
    for start, end, peak in RUSHES:
        if not start - 15 < minute < end + 20:
            continue
        span = end - start
        pos = (minute - start) / span
        # Fast rise, slow decay: the queue appears the moment the bell goes.
        shape = math.exp(-((pos - 0.18) ** 2) / (0.09 if pos < 0.18 else 0.30))
        total += peak * shape
    # Slow weekly drift so the history is not perfectly self-similar.
    total *= 0.85 + 0.3 * random.random()
    return max(0.0, total)


def backfill(store: Store, days: int, step_seconds: int = 30) -> int:
    """Fill `days` of past history plus today up to the current moment."""
    now = datetime.now().replace(second=0, microsecond=0)
    written = 0
    for day in range(days, -1, -1):
        date = now - timedelta(days=day)
        cursor = date.replace(hour=7, minute=0)
        end = date.replace(hour=16, minute=0)
        if day == 0:
            end = min(end, now)
        level = 0.0
        while cursor < end:
            target = expected_count(cursor)
            level += (target - level) * 0.25 + random.gauss(0, 0.7)
            level = max(0.0, level)
            store.add(
                {
                    "ts": cursor.timestamp(),
                    "count": round(level, 1),
                    "raw": round(level + random.gauss(0, 1.2), 1),
                    "device": "sim",
                    "fps": 4.0,
                }
            )
            written += 1
            cursor += timedelta(seconds=step_seconds)
    return written


def live(url: str, key: str, interval: float, speed: float) -> None:
    level = 0.0
    virtual = datetime.now()
    print(f"posting to {url} every {interval}s (time x{speed})")
    while True:
        target = expected_count(virtual)
        level += (target - level) * 0.25 + random.gauss(0, 0.7)
        level = max(0.0, level)
        payload = {
            "count": round(level, 1),
            "raw": round(level + random.gauss(0, 1.2), 1),
            "device": "sim",
            "fps": 4.0,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "X-Device-Key": key},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                state = json.load(resp)["state"]
            print(
                f"{virtual:%H:%M:%S}  count={state['count']:>3}  "
                f"wait={state['wait_seconds'] // 60}m  {state['level']['name']}"
            )
        except (urllib.error.URLError, OSError) as exc:
            print(f"post failed: {exc}")
        time.sleep(interval)
        virtual += timedelta(seconds=interval * speed)


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description="Simulated queue counter")
    ap.add_argument("--backfill", type=int, metavar="DAYS", help="write N days of history")
    ap.add_argument("--live", action="store_true", help="POST live samples to the server")
    ap.add_argument("--url", default="http://127.0.0.1:8080/api/ingest")
    ap.add_argument("--key", default=os.environ.get("DEVICE_KEY", "dev-key-change-me"))
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--speed", type=float, default=1.0, help="virtual clock multiplier")
    ap.add_argument("--data", default=os.path.join(root, "data", "samples.jsonl"))
    args = ap.parse_args()

    if args.backfill:
        store = Store(args.data, retention_days=args.backfill + 1)
        n = backfill(store, args.backfill)
        print(f"wrote {n} samples covering {args.backfill} days to {args.data}")
    if args.live:
        live(args.url, args.key, args.interval, args.speed)
    if not args.backfill and not args.live:
        ap.print_help()


if __name__ == "__main__":
    main()

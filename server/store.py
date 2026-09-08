"""Sample storage and statistics for the kitchen queue service.

Storage is an append-only JSONL file. One sample every few seconds is a few
hundred KB per day, so everything for the retention window is kept in memory
and the file is only ever appended to (and pruned once, at boot).
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from datetime import datetime

SECONDS_PER_DAY = 86400


class Store:
    def __init__(self, path: str, retention_days: int = 28):
        self.path = path
        self.retention = retention_days * SECONDS_PER_DAY
        self._lock = threading.Lock()
        self._samples: deque[dict] = deque()
        self._latest: dict | None = None
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._load()

    # ---------- persistence ----------

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        cutoff = time.time() - self.retention
        kept: list[dict] = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    s = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if s.get("ts", 0) >= cutoff:
                    kept.append(s)
        kept.sort(key=lambda s: s["ts"])
        self._samples = deque(kept)
        self._latest = kept[-1] if kept else None
        # Rewrite the file so pruned samples actually leave the disk.
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for s in kept:
                fh.write(json.dumps(s, separators=(",", ":")) + "\n")
        os.replace(tmp, self.path)

    def add(self, sample: dict) -> None:
        with self._lock:
            # An edge device that buffered through a network outage flushes
            # older samples after newer ones. Keep the deque ordered by ts, and
            # never let a replayed sample become "the current queue length" -
            # otherwise the live number jumps backwards in time when the wifi
            # comes back.
            if self._samples and sample["ts"] < self._samples[-1]["ts"]:
                at = len(self._samples)
                while at > 0 and self._samples[at - 1]["ts"] > sample["ts"]:
                    at -= 1
                self._samples.insert(at, sample)
            else:
                self._samples.append(sample)

            if self._latest is None or sample["ts"] >= self._latest["ts"]:
                self._latest = sample

            cutoff = time.time() - self.retention
            while self._samples and self._samples[0]["ts"] < cutoff:
                self._samples.popleft()
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(sample, separators=(",", ":")) + "\n")

    # ---------- queries ----------

    @property
    def latest(self) -> dict | None:
        return self._latest

    def recent(self, minutes: int, max_points: int = 240) -> list[dict]:
        """Samples from the last `minutes`, thinned to at most `max_points`."""
        cutoff = time.time() - minutes * 60
        with self._lock:
            window = [s for s in self._samples if s["ts"] >= cutoff]
        if len(window) <= max_points:
            return window
        step = len(window) / max_points
        return [window[int(i * step)] for i in range(max_points)]

    def pattern(self, weekday: int, bucket_minutes: int = 10) -> list[dict]:
        """Average count by time of day for a given weekday (0 = Monday).

        This is the part students actually plan around: not "how long is the
        line now" but "how long is it usually at 12:15 on a Tuesday".
        """
        buckets: dict[int, list[float]] = {}
        with self._lock:
            samples = list(self._samples)
        for s in samples:
            dt = datetime.fromtimestamp(s["ts"])
            if dt.weekday() != weekday:
                continue
            slot = (dt.hour * 60 + dt.minute) // bucket_minutes * bucket_minutes
            buckets.setdefault(slot, []).append(float(s.get("count", 0)))
        out = []
        for slot in sorted(buckets):
            vals = buckets[slot]
            out.append(
                {
                    "minute": slot,
                    "avg": round(sum(vals) / len(vals), 2),
                    "peak": round(max(vals), 2),
                    "n": len(vals),
                }
            )
        return out

    def service_rate_hint(self, minutes: int = 30, min_intervals: int = 8) -> float | None:
        """People served per minute, inferred from how fast the queue drains.

        Only intervals where the queue shrank are considered - growth tells you
        about arrivals, not about the till. Even then each interval understates
        the till, because people keep joining while it drains: observed drop =
        service - arrivals. So take a high percentile rather than the mean; the
        fastest drains are the ones where arrivals happened to be near zero, and
        those are the ones that reveal actual service capacity.

        Returns None until there is enough evidence to beat the configured
        constant, which the caller then falls back to.
        """
        window = self.recent(minutes, max_points=10_000)
        rates: list[float] = []
        for a, b in zip(window, window[1:]):
            dt = b["ts"] - a["ts"]
            if not 0 < dt <= 60:
                continue
            drop = a.get("count", 0) - b.get("count", 0)
            if drop > 0:
                rates.append(drop / (dt / 60.0))
        if len(rates) < min_intervals:
            return None
        rates.sort()
        p75 = rates[int(len(rates) * 0.75)]
        return round(p75, 2)

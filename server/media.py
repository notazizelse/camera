"""Live video relay.

The cafeteria PC pushes the stream *out* to this server; browsers pull it from
here. That direction matters: the school network allows outbound HTTPS and
nothing else, so nothing here ever dials back into school.

Everything lives in RAM and expires. There is no path in this file that writes
video to disk, which is what lets the deployment honestly claim it does not
record. Restarting the server destroys every frame it was holding.

Two shapes of stream are supported:

  HLS       ffmpeg PUTs a rolling playlist plus 2-second segments. Browsers
            play it with hls.js (natively on Safari). ~6-8s behind live.

  Snapshot  a JPEG every second or two, served as one image or as an
            endless multipart stream. Survives the most hostile proxies,
            costs almost nothing, and for looking at a queue it is usually
            enough.
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict


class MediaRelay:
    def __init__(self, max_segments: int = 10, ttl_seconds: float = 90.0):
        self.max_segments = max_segments
        self.ttl = ttl_seconds
        self._lock = threading.Lock()
        self._playlist: tuple[float, bytes] | None = None
        self._segments: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._frame: tuple[float, bytes] | None = None
        self._frame_subs: set = set()
        self._last_push = 0.0

    # ---------- HLS ----------

    def put_playlist(self, data: bytes) -> None:
        with self._lock:
            self._playlist = (time.time(), data)
            self._last_push = time.time()

    def put_segment(self, name: str, data: bytes) -> None:
        with self._lock:
            self._segments[name] = (time.time(), data)
            self._segments.move_to_end(name)
            while len(self._segments) > self.max_segments:
                self._segments.popitem(last=False)
            self._last_push = time.time()

    def drop_segment(self, name: str) -> None:
        # ffmpeg issues DELETE for rolled-off segments when it is writing to an
        # HTTP target. Honour it, but never fail if it arrives twice.
        with self._lock:
            self._segments.pop(name, None)

    def get_playlist(self) -> bytes | None:
        with self._lock:
            if not self._playlist:
                return None
            ts, data = self._playlist
            return None if time.time() - ts > self.ttl else data

    def get_segment(self, name: str) -> bytes | None:
        with self._lock:
            item = self._segments.get(name)
            return item[1] if item else None

    # ---------- snapshots ----------

    def put_frame(self, jpeg: bytes) -> None:
        with self._lock:
            self._frame = (time.time(), jpeg)
            self._last_push = time.time()
            subs = list(self._frame_subs)
        for q in subs:
            try:
                q.put_nowait(jpeg)
            except Exception:
                pass  # a viewer that cannot keep up simply misses frames

    def get_frame(self) -> tuple[float, bytes] | None:
        with self._lock:
            return self._frame

    def subscribe_frames(self):
        import queue

        q: queue.Queue = queue.Queue(maxsize=2)
        with self._lock:
            self._frame_subs.add(q)
        return q

    def unsubscribe_frames(self, q) -> None:
        with self._lock:
            self._frame_subs.discard(q)

    # ---------- status ----------

    def state(self) -> dict:
        """What the page needs to decide whether to show a player.

        Deliberately says nothing about the content of the stream - this
        endpoint is public, the video itself is not.
        """
        with self._lock:
            age = time.time() - self._last_push if self._last_push else None
            has_hls = self._playlist is not None and bool(self._segments)
            has_frame = self._frame is not None
        live = age is not None and age < 20
        return {
            "available": live and (has_hls or has_frame),
            "mode": "hls" if has_hls else "snapshot" if has_frame else None,
            "age_seconds": round(age, 1) if age is not None else None,
            "viewers": len(self._frame_subs),
        }


CAMERA_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


class MediaRegistry:
    """One relay per camera.

    An NVR has many channels and a school has many rooms, so the stream a
    viewer wants is addressed by name rather than assumed. Ids are validated
    against a strict pattern because they become URL path segments.
    """

    def __init__(self, cameras: list[dict] | None = None):
        self._lock = threading.Lock()
        self._relays: dict[str, MediaRelay] = {}
        self._labels: dict[str, str] = {}
        self._order: list[str] = []
        for camera in cameras or []:
            self.register(camera["id"], camera.get("label", camera["id"]))

    @staticmethod
    def valid(camera_id: str) -> bool:
        return bool(camera_id and CAMERA_ID.match(camera_id))

    def register(self, camera_id: str, label: str | None = None) -> None:
        if not self.valid(camera_id):
            raise ValueError(f"bad camera id: {camera_id!r}")
        with self._lock:
            if camera_id not in self._relays:
                self._relays[camera_id] = MediaRelay()
                self._order.append(camera_id)
            if label:
                self._labels[camera_id] = label

    def relay(self, camera_id: str, create: bool = False) -> MediaRelay | None:
        """Look up a camera's relay, optionally creating it on first push.

        Creating on push means a new camera appears on the site the moment
        the pusher starts, with no server restart - but only an ingest
        holding the device key can do it, never a viewer.
        """
        with self._lock:
            relay = self._relays.get(camera_id)
        if relay or not create:
            return relay
        self.register(camera_id)
        with self._lock:
            return self._relays[camera_id]

    def label(self, camera_id: str) -> str:
        return self._labels.get(camera_id, camera_id)

    def default_id(self) -> str | None:
        """The first camera that is actually sending, else the first known."""
        with self._lock:
            order = list(self._order)
        for camera_id in order:
            if self._relays[camera_id].state()["available"]:
                return camera_id
        return order[0] if order else None

    def listing(self) -> list[dict]:
        with self._lock:
            order = list(self._order)
        return [
            {"id": camera_id, "label": self.label(camera_id),
             **self._relays[camera_id].state()}
            for camera_id in order
        ]

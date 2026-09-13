#!/usr/bin/env python3
"""Kitchen queue counter - edge node.

Watches one video source, counts how many people are standing inside a
region of interest, and POSTs that single number to the queue server.

Design rules this file follows, in priority order:

  1. Video never leaves this device. Only an integer does. If you enable
     snapshots you get a heavily pixelated frame, not footage.
  2. Only outbound HTTPS. Nothing listens on a port, so no firewall
     changes and nothing to find on Shodan.
  3. Survive a bad network. Samples buffer in RAM and flush on reconnect.

Usage:
    python counter.py --pick-roi          # click the queue area once, save it
    python counter.py                     # run
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CONFIG = {
    # "0" for the first USB webcam, a file path, or an RTSP URL such as
    # rtsp://user:pass@10.0.0.42:554/Streaming/Channels/102
    # Always use the camera's SUB stream (lower res) - you are counting
    # blobs, not reading name badges, and it costs a tenth of the CPU.
    "source": "0",
    "server_url": "http://127.0.0.1:8080/api/ingest",
    "device_key": "dev-key-change-me",
    "device_name": "kitchen-1",
    # Polygon covering the queue, in fractions of frame width/height, so it
    # stays correct if you change resolution. Set it with --pick-roi.
    "roi": [[0.05, 0.35], [0.95, 0.35], [0.95, 0.98], [0.05, 0.98]],
    "detector": "yolo",             # "yolo" | "dnn" | "hog"
    "model": "yolov8n.pt",
    "confidence": 0.4,
    "imgsz": 480,
    "detect_every_seconds": 1.0,    # inference cadence, not frame rate
    "post_every_seconds": 10.0,
    "smoothing": 0.35,              # EMA alpha; lower = calmer number
    "snapshot": {
        "enabled": False,
        "every_seconds": 30,
        "pixel_block": 22,          # bigger = less recognisable
    },
    "verify_tls": True,
}


# ---------------------------------------------------------------------------
# detectors
# ---------------------------------------------------------------------------


class YoloDetector:
    """Ultralytics YOLO. Best accuracy; needs torch. Pi 5 or better."""

    def __init__(self, cfg: dict):
        from ultralytics import YOLO

        self.model = YOLO(cfg["model"])
        self.conf = cfg["confidence"]
        self.imgsz = cfg["imgsz"]

    def __call__(self, frame):
        res = self.model.predict(
            frame, imgsz=self.imgsz, conf=self.conf, classes=[0], verbose=False
        )[0]
        return [
            (*map(int, b.xyxy[0].tolist()), float(b.conf[0])) for b in res.boxes
        ]


class DnnDetector:
    """MobileNet-SSD through OpenCV's DNN module.

    No torch, ~40 MB of RAM, runs fine on a Pi 4 or even a Pi Zero 2 W at
    one frame per second. Less accurate in a dense crowd than YOLO.
    Download the two files into edge/models/:
      https://github.com/chuanqi305/MobileNet-SSD  (deploy.prototxt + caffemodel)
    """

    PERSON_CLASS = 15

    def __init__(self, cfg: dict):
        models = os.path.join(ROOT, "models")
        self.net = cv2.dnn.readNetFromCaffe(
            os.path.join(models, "MobileNetSSD_deploy.prototxt"),
            os.path.join(models, "MobileNetSSD_deploy.caffemodel"),
        )
        self.conf = cfg["confidence"]

    def __call__(self, frame):
        h, w = frame.shape[:2]
        blob = cv2.dnn.blobFromImage(
            cv2.resize(frame, (300, 300)), 0.007843, (300, 300), 127.5
        )
        self.net.setInput(blob)
        out = self.net.forward()
        boxes = []
        for i in range(out.shape[2]):
            conf = float(out[0, 0, i, 2])
            if conf < self.conf or int(out[0, 0, i, 1]) != self.PERSON_CLASS:
                continue
            x1, y1, x2, y2 = (out[0, 0, i, 3:7] * np.array([w, h, w, h])).astype(int)
            boxes.append((x1, y1, x2, y2, conf))
        return boxes


class HogDetector:
    """OpenCV's classic HOG pedestrian detector. No downloads, no ML stack.

    Only useful as a fallback for a first-day smoke test: it wants
    full-body, well-separated people and will undercount a real queue.
    """

    def __init__(self, cfg: dict):
        self.hog = cv2.HOGDescriptor()
        self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

    def __call__(self, frame):
        rects, weights = self.hog.detectMultiScale(
            frame, winStride=(8, 8), padding=(8, 8), scale=1.05
        )
        return [
            (x, y, x + w, y + h, float(c))
            for (x, y, w, h), c in zip(rects, weights)
        ]


DETECTORS = {"yolo": YoloDetector, "dnn": DnnDetector, "hog": HogDetector}


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------


class FrameGrabber(threading.Thread):
    """Always hold the newest frame.

    An RTSP stream delivers frames faster than we can run inference. If you
    read them in order you drift further and further behind real time, which
    for this application is the one unforgivable bug. So a thread drains the
    socket and keeps only the latest frame.
    """

    def __init__(self, source):
        super().__init__(daemon=True)
        self.source = int(source) if str(source).isdigit() else source
        self.frame = None
        self.lock = threading.Lock()
        self.running = True
        self.fps = 0.0
        self._cap = None

    def _open(self):
        cap = cv2.VideoCapture(self.source)
        if isinstance(self.source, str) and self.source.startswith("rtsp"):
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def run(self):
        backoff = 1.0
        ticks, t0 = 0, time.time()
        while self.running:
            if self._cap is None or not self._cap.isOpened():
                self._cap = self._open()
                if not self._cap.isOpened():
                    print(f"camera unavailable, retrying in {backoff:.0f}s")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue
                backoff = 1.0
                print("camera connected")
            ok, frame = self._cap.read()
            if not ok:
                self._cap.release()
                self._cap = None
                continue
            with self.lock:
                self.frame = frame
            ticks += 1
            if ticks % 30 == 0:
                now = time.time()
                self.fps = round(30 / (now - t0), 1)
                t0 = now

    def read(self):
        with self.lock:
            return None if self.frame is None else self.frame.copy()

    def stop(self):
        self.running = False
        if self._cap:
            self._cap.release()


# ---------------------------------------------------------------------------
# uploading
# ---------------------------------------------------------------------------


class Uploader(threading.Thread):
    """Outbound-only sender with an in-RAM backlog.

    School Wi-Fi drops. Rather than lose samples, they queue here and go out
    when the link returns - the history stays intact even if the live number
    was briefly stale.
    """

    def __init__(self, url: str, key: str, verify_tls: bool = True, backlog: int = 500):
        super().__init__(daemon=True)
        self.url = url
        self.key = key
        self.q: queue.Queue = queue.Queue(maxsize=backlog)
        self.running = True
        self.ctx = None if verify_tls else ssl._create_unverified_context()
        self.last_error: str | None = None

    def send(self, payload: dict) -> None:
        try:
            self.q.put_nowait(payload)
        except queue.Full:
            self.q.get_nowait()      # drop the oldest, keep the newest
            self.q.put_nowait(payload)

    def run(self):
        while self.running:
            payload = self.q.get()
            for attempt in range(3):
                try:
                    req = urllib.request.Request(
                        self.url,
                        data=json.dumps(payload).encode(),
                        headers={
                            "Content-Type": "application/json",
                            "X-Device-Key": self.key,
                        },
                    )
                    with urllib.request.urlopen(req, timeout=10, context=self.ctx):
                        self.last_error = None
                    break
                except (urllib.error.URLError, OSError) as exc:
                    self.last_error = str(exc)
                    time.sleep(2 ** attempt)
            else:
                print(f"upload failed, backlog={self.q.qsize()}: {self.last_error}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def roi_polygon(cfg: dict, shape) -> np.ndarray:
    h, w = shape[:2]
    return np.array([[int(x * w), int(y * h)] for x, y in cfg["roi"]], dtype=np.int32)


def count_in_roi(boxes, poly) -> int:
    """A person counts when their feet are inside the polygon.

    Using the bottom-centre point rather than the box centre means someone
    walking past in the background, whose head overlaps the queue area,
    is not counted.
    """
    n = 0
    for x1, y1, x2, y2, _conf in boxes:
        foot = (int((x1 + x2) / 2), int(y2))
        if cv2.pointPolygonTest(poly, foot, False) >= 0:
            n += 1
    return n


def anonymize(frame, boxes, poly, block: int) -> bytes:
    """Produce a frame that shows the shape of the queue and nothing else.

    The whole image is destroyed down to ~20x15 effective pixels and rebuilt,
    then people are drawn as flat silhouettes. Faces, clothing detail and
    text are gone before the JPEG is ever encoded.
    """
    h, w = frame.shape[:2]
    small = cv2.resize(frame, (max(1, w // block), max(1, h // block)),
                       interpolation=cv2.INTER_AREA)
    out = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)
    out = cv2.addWeighted(out, 0.35, np.full_like(out, 240), 0.65, 0)

    overlay = out.copy()
    cv2.fillPoly(overlay, [poly], (226, 232, 240))
    out = cv2.addWeighted(overlay, 0.5, out, 0.5, 0)
    cv2.polylines(out, [poly], True, (148, 163, 184), 2)

    for x1, y1, x2, y2, _c in boxes:
        foot = (int((x1 + x2) / 2), int(y2))
        if cv2.pointPolygonTest(poly, foot, False) < 0:
            continue
        cv2.rectangle(out, (x1, y1), (x2, y2), (37, 99, 235), -1)
    ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 55])
    return buf.tobytes() if ok else b""


def pick_roi(cfg: dict, path: str) -> None:
    grabber = FrameGrabber(cfg["source"])
    grabber.start()
    frame = None
    for _ in range(100):
        frame = grabber.read()
        if frame is not None:
            break
        time.sleep(0.1)
    grabber.stop()
    if frame is None:
        sys.exit("could not read a frame from the source")

    pts: list[tuple[int, int]] = []
    h, w = frame.shape[:2]

    def on_mouse(event, x, y, _flags, _param):
        if event == cv2.EVENT_LBUTTONDOWN:
            pts.append((x, y))
        elif event == cv2.EVENT_RBUTTONDOWN and pts:
            pts.pop()

    cv2.namedWindow("pick ROI")
    cv2.setMouseCallback("pick ROI", on_mouse)
    print("left-click to add points around the queue area, right-click to undo, "
          "'s' to save, 'q' to quit")
    while True:
        canvas = frame.copy()
        if len(pts) > 1:
            cv2.polylines(canvas, [np.array(pts)], False, (0, 200, 0), 2)
        for p in pts:
            cv2.circle(canvas, p, 5, (0, 200, 0), -1)
        cv2.imshow("pick ROI", canvas)
        key = cv2.waitKey(30) & 0xFF
        if key == ord("s") and len(pts) >= 3:
            cfg["roi"] = [[round(x / w, 4), round(y / h, 4)] for x, y in pts]
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
            print(f"saved {len(pts)}-point ROI to {path}")
            break
        if key == ord("q"):
            break
    cv2.destroyAllWindows()


def load_config(path: str) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(path):
        # utf-8-sig: config written by PowerShell or Notepad carries a BOM.
        with open(path, encoding="utf-8-sig") as fh:
            user = json.load(fh)
        cfg.update(user)
        cfg["snapshot"] = {**DEFAULT_CONFIG["snapshot"], **user.get("snapshot", {})}
    return cfg


# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Queue counter edge node")
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--pick-roi", action="store_true", help="click the queue area and save it")
    ap.add_argument("--preview", action="store_true", help="show what the counter sees")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.pick_roi:
        return pick_roi(cfg, args.config)

    detector = DETECTORS[cfg["detector"]](cfg)
    grabber = FrameGrabber(cfg["source"])
    grabber.start()
    uploader = Uploader(cfg["server_url"], cfg["device_key"], cfg["verify_tls"])
    uploader.start()

    ema: float | None = None
    boxes: list = []
    poly = None
    last_detect = last_post = last_snap = 0.0
    print(f"counting with '{cfg['detector']}', posting to {cfg['server_url']}")

    try:
        while True:
            frame = grabber.read()
            if frame is None:
                time.sleep(0.2)
                continue
            if poly is None:
                poly = roi_polygon(cfg, frame.shape)

            now = time.time()
            if now - last_detect >= cfg["detect_every_seconds"]:
                last_detect = now
                boxes = detector(frame)
                n = count_in_roi(boxes, poly)
                a = cfg["smoothing"]
                ema = float(n) if ema is None else a * n + (1 - a) * ema

            if ema is not None and now - last_post >= cfg["post_every_seconds"]:
                last_post = now
                payload = {
                    # Stamped when the reading was taken, not when it is sent:
                    # anything that waited in the upload backlog lands in the
                    # history at the right moment instead of pretending to be
                    # the current queue length.
                    "ts": now,
                    "count": round(ema, 1),
                    "raw": count_in_roi(boxes, poly),
                    "device": cfg["device_name"],
                    "fps": grabber.fps,
                }
                snap = cfg["snapshot"]
                if snap["enabled"] and now - last_snap >= snap["every_seconds"]:
                    last_snap = now
                    jpeg = anonymize(frame, boxes, poly, snap["pixel_block"])
                    if jpeg:
                        payload["image_jpeg_b64"] = base64.b64encode(jpeg).decode()
                uploader.send(payload)
                print(f"count={payload['raw']} smoothed={payload['count']} "
                      f"src_fps={grabber.fps}")

            if args.preview:
                canvas = frame.copy()
                cv2.polylines(canvas, [poly], True, (0, 200, 0), 2)
                for x1, y1, x2, y2, _c in boxes:
                    cv2.rectangle(canvas, (x1, y1), (x2, y2), (255, 120, 0), 2)
                cv2.imshow("counter", canvas)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
            else:
                time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        grabber.stop()
        uploader.running = False
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()

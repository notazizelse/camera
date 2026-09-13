# Kitchen Queue

Shows students how long the canteen line is, so they can decide when to go.

A camera watches the queue. The website turns that into a live count, a wait
estimate, and — the part people actually use — a "typical for this weekday"
chart that says whether waiting fifteen minutes helps. It can also carry the
live picture, behind an access code.

```
 camera ──RTSP──▶ PC or Pi ──HTTPS, outbound only──▶ server ──▶ website
                     │                                            count: public
                     ├── counter.py  →  {"count": 14}             video: access code
                     └── pusher.py   →  HLS segments / JPEGs
```

Two things can run on whatever machine already sees the camera, together or
separately: **`counter.py`** publishes the queue length, and **`pusher.py`**
forwards the picture. Both only ever dial out, so the school network needs no
changes and nothing is exposed.

## Count, picture, or both

They are separate switches, and they have genuinely different profiles:

| | Count (`counter.py`) | Picture (`pusher.py`) |
|---|---|---|
| Leaves the building | one integer per 10 s | 0.02–1 Mbit/s of video |
| Who can see it | everyone | access-code holders |
| Kept anywhere | 28 days of numbers | nothing; ~12 s in RAM |
| Works on a slow phone | yes | mostly |
| Answers "should I go now?" | directly, with a forecast | only while you watch |
| Approval needed | usually a conversation | a real one, in writing |

Running the count alongside the video is worth it even if the picture is the
point: it keeps working when the stream is down, it is what a phone on a
locked screen can show, and it produces the busy-times forecast that video
cannot. Both read the same camera, so there is no extra hardware.

Before mounting anything, read [`docs/architecture.md`](docs/architecture.md)
for the camera and network constraints, and
[`docs/forwarding.md`](docs/forwarding.md) for getting the picture out of a
school network.

## Run it locally in two minutes

No installs. Python 3.11+ only.

```bash
python scripts/simulate.py --backfill 21
```

```bash
python server/app.py --port 8080
```

```bash
python scripts/simulate.py --live
```

Open <http://127.0.0.1:8080>. The simulator invents a plausible school day, so
the whole site works before any hardware exists — which is also how you demo it
to staff.

## Layout

| Path | What it is |
|---|---|
| `server/app.py` | HTTP + SSE server. Stdlib only, no pip install, no build step. |
| `server/store.py` | Append-only JSONL storage, weekday patterns, service-rate inference. |
| `edge/counter.py` | Counts people in a region and POSTs the number. |
| `edge/pusher.py` | Forwards the live picture. ffmpeg supervision, watchdog, three transports. |
| `edge/discover.py` | Finds the camera's RTSP URL when the NVR software won't tell you. |
| `edge/hikvision.py` | Hikvision/iVMS-4200 devices: channel list, snapshots, ready-made config. |
| `web/` | The student-facing page. Vanilla JS, hand-built SVG charts, no dependencies. |
| `scripts/simulate.py` | Fake counter for development and demos. |
| `scripts/fake_stream.py` | Fake video pusher - tests the relay with no ffmpeg and no camera. |
| `deploy/` | systemd units and a Caddy config for a real deployment. |

## Forwarding the live camera

If a PC can already see the footage, that PC is the whole edge.

**On a Hikvision / iVMS-4200 system** — read the IP and login out of iVMS,
then ignore iVMS entirely and talk to the NVR directly:

```bash
python edge/hikvision.py --host 10.0.12.40 --user queue --password THEPASSWORD
```

```bash
python edge/pusher.py --source isapi --camera cafeteria
```

That second command needs **no ffmpeg at all** — it pulls JPEGs straight from
the NVR over HTTP. Step-by-step: [`docs/ivms.md`](docs/ivms.md).

**Any other system** — find the RTSP URL, then push HLS:

```bash
python edge/discover.py --user viewer --password THEPASSWORD
```

```bash
python edge/pusher.py --test --camera cafeteria
```

`--test` pushes a test pattern, so you can confirm the entire path before
involving the camera. Full guide, including what to do when the NVR software
refuses to expose a stream: [`docs/forwarding.md`](docs/forwarding.md).

Live video is **off** until you set `VIEW_CODE` on the server, and the count
stays public either way — only the picture sits behind the code.

## Setting up the real counter

1. **Get a video source.** Either a sub-stream from the existing NVR
   (`rtsp://user:pass@nvr-ip:554/...`, ask IT for a read-only account) or your
   own camera pointed at the queue. See `docs/architecture.md` for which.

2. **Install on the Pi.**
   ```bash
   pip install -r edge/requirements.txt
   ```

3. **Configure.**
   ```bash
   cp edge/config.example.json edge/config.json
   ```
   Set `source`, `server_url`, and a long random `device_key` that matches the
   server's `DEVICE_KEY`.

4. **Draw the queue area** — once, by clicking round it:
   ```bash
   python edge/counter.py --pick-roi
   ```
   Draw the floor area where the *line* forms, not the whole room. People are
   counted by their feet, so someone walking past behind the queue is excluded.

5. **Watch it work** before trusting it:
   ```bash
   python edge/counter.py --preview
   ```

6. **Calibrate the wait estimate.** Stand at the till with a stopwatch at lunch
   and time 20 people. Divide. Put that in `service_seconds_per_person` in
   `server/config.json`. The server refines it from live drain rates afterwards,
   but only within ±2× of your figure — an inference from noisy data should not
   be allowed to claim a 40-minute wait at a till that has never taken more than
   ten seconds a head.

## Server configuration

```bash
cp server/config.example.json server/config.json
export DEVICE_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python server/app.py --port 8080
```

`DEVICE_KEY` is the shared secret every edge device sends in `X-Device-Key`.
Without it, anyone who finds your URL can post fake queue lengths or fake video.

`VIEW_CODE` controls the live picture. Unset, live video is off entirely and
only the count is served. Set it to a code students can type, or to the literal
string `open` to remove the gate (the server prints a warning when you do).

### API

| Endpoint | Purpose |
|---|---|
| `POST /api/ingest` | Edge → server. Needs `X-Device-Key`. Body: `{"count": 14}`. |
| `GET /api/state` | Current count, level, wait estimate, freshness. |
| `GET /api/stream` | Server-Sent Events; pushes state on every ingest. |
| `GET /api/history?minutes=90` | Recent samples for the live chart. |
| `GET /api/pattern?weekday=1` | Average count by time of day. |
| `GET /api/snapshot.jpg` | Pixelated frame, only if `snapshot_enabled`. |
| `PUT /api/hls/<name>` | Pusher → server. Playlist and segments. Needs `X-Device-Key`. |
| `POST /api/frame` | Pusher → server. One JPEG, for snapshot mode. |
| `GET /api/media-state` | Public. Whether a picture exists and whether you may see it. |
| `POST /api/access` | Exchange the access code for a 12-hour cookie. |
| `GET /live/stream.m3u8` | The stream. Requires the cookie. |
| `GET /api/live.mjpg` | JPEG frames as one stream. Requires the cookie. |

## Deploying

See `deploy/`. The short version: put the server on a small VPS behind Caddy
(automatic HTTPS), run both processes under systemd, and let the Pi reach the
server outbound over HTTPS. Do **not** port-forward anything into the school
network, and do not expose the camera itself to the internet.

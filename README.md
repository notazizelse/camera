# Kitchen Queue

Shows students how long the canteen line is, so they can decide when to go.

A camera watches the queue. A small computer next to it counts people **and sends
one integer**. No video leaves the kitchen. The website turns that integer into a
count, a wait estimate, a live trend, and — the part people actually use — a
"typical for this weekday" chart that says whether waiting fifteen minutes helps.

```
 camera ──RTSP/USB──▶ edge counter ──HTTPS POST {"count": 14}──▶ server ──▶ website
 (kitchen, stays put)  (Pi, in the kitchen)      outbound only    (VPS)     (phones)
```

## Why it sends a number and not video

This is the single most important design decision in the project, and the one
that will decide whether your school lets you build it.

Publishing CCTV of a school canteen to a website is a different activity from
running CCTV for security. It is a new purpose for footage of identifiable
minors, on a public URL, that anyone can screen-record. Expect that to be
refused, and expect it to be refused correctly.

Counting people and publishing the count is not that. Nothing identifiable is
transmitted, nothing is stored, and the output is exactly what students wanted
in the first place — *how long is the line* — with none of what they didn't ask
for. It is also about 30,000× less bandwidth.

Read [`docs/architecture.md`](docs/architecture.md) before you build anything:
it covers the camera options, the sensor alternatives that avoid cameras
entirely, the network constraints you will hit inside a school, and what to put
in front of the people who have to approve this.

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
| `edge/counter.py` | The thing beside the camera. Counts people in a region, POSTs the number. |
| `web/` | The student-facing page. Vanilla JS, hand-built SVG charts, no dependencies. |
| `scripts/simulate.py` | Fake edge device for development and demos. |
| `deploy/` | systemd units and a Caddy config for a real deployment. |

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

`DEVICE_KEY` is the shared secret the edge device sends in `X-Device-Key`.
Without it, anyone who finds your URL can post fake queue lengths.

### API

| Endpoint | Purpose |
|---|---|
| `POST /api/ingest` | Edge → server. Needs `X-Device-Key`. Body: `{"count": 14}`. |
| `GET /api/state` | Current count, level, wait estimate, freshness. |
| `GET /api/stream` | Server-Sent Events; pushes state on every ingest. |
| `GET /api/history?minutes=90` | Recent samples for the live chart. |
| `GET /api/pattern?weekday=1` | Average count by time of day. |
| `GET /api/snapshot.jpg` | Pixelated frame, only if `snapshot_enabled`. |

## Deploying

See `deploy/`. The short version: put the server on a small VPS behind Caddy
(automatic HTTPS), run both processes under systemd, and let the Pi reach the
server outbound over HTTPS. Do **not** port-forward anything into the school
network, and do not expose the camera itself to the internet.

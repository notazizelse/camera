# Forwarding the cafeteria camera to the website

You have a camera and a PC that can already see it. This is how the picture
gets from that PC to a phone, without opening anything on the school network.

```
 NVR / camera ──RTSP──▶ cafeteria PC ──HTTPS PUT──▶ queue server ──HLS──▶ phones
   (unchanged)          (pusher.py +               (VPS, holds a few    (access
                         ffmpeg)                    seconds in RAM)      code)
```

The whole design turns on one fact: **the PC dials out.** Nothing listens, no
port is forwarded, no firewall rule changes, and the NVR is never exposed. If
the school's outbound HTTPS works — and it does, or nobody could browse — this
works.

---

## 1. Get ffmpeg on that PC

```bash
winget install Gyan.FFmpeg
```

Close and reopen the terminal afterwards so `ffmpeg` is on `PATH`. On Debian
it's `sudo apt install ffmpeg`; on macOS `brew install ffmpeg`.

## 2. Find the camera's RTSP URL

Pull from the NVR rather than the PC's screen if you possibly can: it is
sharper, cheaper, and does not break when someone minimises a window.

```bash
python edge/discover.py --user viewer --password THEPASSWORD
```

It sends an ONVIF discovery probe, then tries the RTSP paths the major vendors
use and reports which ones answer. Ask IT for a **read-only viewer account** —
not the NVR admin login, which should never sit in a config file on a PC in a
cafeteria.

If discovery finds nothing (common — many NVRs disable it, and multicast
rarely crosses a VLAN), pass the address from the NVR software directly:

```bash
python edge/discover.py --host 10.0.12.40 --user viewer --password THEPASSWORD
```

**Ask for the sub-stream, not the main stream.** Every camera publishes both.
The sub-stream is typically 640×360 and the main stream 2688×1520 — for looking
at a queue they are equally useful, and one of them is twenty times the upload.
On Hikvision that is channel `102` rather than `101`; on Dahua `subtype=1`.

### If the NVR software refuses to give you a stream

Some proprietary clients simply will not. Capture the viewer window instead:

```bash
python edge/pusher.py --source "window:Camera Viewer" --transcode
```

The title must match the window exactly. It works, but treat it as the
fallback: the PC has to stay logged in with that window open and not minimised
(Windows will not render a minimised window for capture), and you inherit
whatever the viewer draws, timestamps and all.

## 3. Point the pusher at your server

```bash
cp edge/pusher.example.json edge/pusher.json
```

Set `source`, `server_url`, and `device_key` — the key must equal the server's
`DEVICE_KEY` exactly. Prove the chain before involving the camera:

```bash
python edge/pusher.py --test
```

That pushes a test pattern. Load the site; if you can see colour bars, every
part of the path works and anything that fails next is the camera. Then:

```bash
python edge/pusher.py
```

`--print-command` shows the exact ffmpeg command without running it, which is
what to paste into a question when something misbehaves. Camera passwords are
redacted from all output.

## 4. Turn on the viewer gate

Live video is **off** until you decide who may watch. On the server:

```bash
export DEVICE_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export VIEW_CODE="riverside-lunch"
python3 server/app.py
```

- `VIEW_CODE` unset → live video is disabled entirely; the count still works.
- `VIEW_CODE=open` → no gate at all. The server prints a warning. Do not do
  this on a public URL.
- anything else → viewers type it once and get a 12-hour cookie.

The count stays public; only the picture is behind the code. That split is
deliberate — it is also the thing to say first when someone asks whether the
cafeteria is "on the internet".

---

## Choosing a mode

| | `hls` (default) | `snapshot` | `rtmp` |
|---|---|---|---|
| Latency | 6–8 s | 2–3 s | < 1 s |
| Upload | 0.3–1 Mbit/s | ~20 kbit/s | 0.5–2 Mbit/s |
| Ports out | 443 | 443 | 1935 (often blocked) |
| Extra server | none | none | MediaMTX |
| Survives a strict proxy | usually | almost always | rarely |

Start with `hls`. If the school proxy mangles it, or the upload is too slow, or
you want the smallest possible footprint:

```bash
python edge/pusher.py --mode snapshot
```

One JPEG every two seconds is about 15 KB. For judging how long a line is, it
is genuinely hard to beat, and it is the mode most likely to survive whatever
sits between you and the internet.

Use `rtmp` only if you actually need sub-second latency and can run MediaMTX
yourself. Nobody needs sub-second latency to decide whether to go to lunch.

### Transcoding

Off by default — copying the camera's existing H.264 costs the PC almost no
CPU. Turn it on with `--transcode` if the stream is too big to upload, if only
the main stream is available, or if the browser will not play what the camera
produces (some cameras emit H.265, which browsers largely do not support).

```bash
python edge/pusher.py --transcode          # 360p, ~700 kbit/s
```

## What the server does with the video

- Holds roughly the last 12 seconds in RAM and overwrites continuously.
- Writes **no video to disk**, ever. There is no code path that does.
- Serves it only to a browser holding the access-code cookie.
- Drops audio at the camera — `-an` is not optional in the pusher. A recording
  of what people say in a queue is a far bigger intrusion than a picture of
  the queue, and nobody asked for it.

Restarting the server destroys everything it was holding. That is the honest
answer to "what happens to the footage", and it is worth being able to say.

## Running it unattended

The PC will reboot. On Windows, register the pusher as a scheduled task that
starts at boot and restarts on failure:

```powershell
schtasks /create /tn "QueuePusher" /sc onstart /ru SYSTEM /rl HIGHEST /f ^
  /tr "python C:\path\to\edge\pusher.py"
```

The pusher supervises ffmpeg itself: it restarts on exit with backoff, and a
watchdog asks the *server* every ten seconds whether media is actually
arriving. That second check matters — ffmpeg can sit there looking perfectly
healthy while the NVR has stopped sending or a proxy is swallowing the
uploads, and no local timeout notices.

## When it doesn't work

| Symptom | Cause | Fix |
|---|---|---|
| `ffmpeg not found` | not on PATH | reopen the terminal after installing |
| ffmpeg exits instantly, `401` | wrong camera credentials | recheck with `discover.py` |
| `Connection refused` on RTSP | wrong port or VLAN | can the PC ping the NVR? |
| Site says "camera offline" | uploads not arriving | run with `--print-command`, try `--test` |
| Player black, then switches to stills | browser can't decode the stream | camera is probably H.265 — add `--transcode` |
| Everything works, picture tears | RTSP over UDP | already forced to TCP; check wifi |
| 413 on segment upload | proxy body limit | raise `max_size` in the Caddyfile |

The page degrades on purpose: HLS → hls.js → JPEG frames. If a viewer ends up
on stills, something upstream refused the video, and the site keeps working
rather than showing a black rectangle.

## Two things worth doing anyway

**Run the counter on the same PC.** It already has the stream. `edge/counter.py`
turns it into the queue number, the wait estimate, and the busy-times chart —
which keep working when the video is down, work on a locked phone, and are
what most students will actually look at. Same source, no extra hardware.

**Get it in writing.** Streaming a cafeteria to a URL is a different activity
from running CCTV for security, whoever holds the camera. A page of who can see
it, what is kept (nothing), and who can switch it off will take an afternoon
and saves the project later.

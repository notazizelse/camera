# Forwarding from an iVMS-4200 setup

iVMS-4200 is Hikvision's client application. It is **not** the source of the
video — it is a viewer connected to a Hikvision NVR or camera over the SDK port
(8000). That same device also speaks two open protocols, and going to it
directly is much better than capturing the iVMS window:

| | Capture the iVMS window | Go to the device |
|---|---|---|
| Picture | whatever is on screen, with iVMS overlays | clean camera feed |
| CPU on the PC | high — screen capture plus encoding | near zero |
| Breaks if… | window minimised, PC locked, layout changed | nothing |
| Multiple cameras | one grid, all or nothing | pick any channel |

So: read the connection details out of iVMS-4200, then ignore iVMS entirely.

---

## 1. Get the details out of iVMS-4200

In iVMS-4200 (v3.x): **Maintenance and Management → Device Management**. Older
versions put **Device Management** on the control panel home screen.

The list gives you:

- **IP address** — the NVR or camera, e.g. `10.0.12.40`
- **Port** — usually `8000`. That is the SDK port, which this project does
  **not** use. You want HTTP (`80`) and RTSP (`554`).
- **User name** — often `admin`

iVMS-4200 stores the password but will not show it. If nobody knows it, IT will
have to supply it or make you a new account (see step 2).

> **Before you start guessing passwords:** Hikvision devices lock out an IP
> after a handful of failed logins — Configuration → System → Security →
> Illegal Login Lock, typically 30 minutes. Lock out the cafeteria PC and you
> have broken the school's own CCTV access until it expires. The tools here
> stop after two refusals for exactly this reason. Get the password right
> before you run anything repeatedly.

## 2. Ask for a read-only account

Do not use the `admin` login. In the NVR's web interface: **Configuration →
System → User Management → Add**, level **Operator**, with live view permission
for the cafeteria channel and nothing else. Name it something like `queue`.

That account goes in a config file on a PC in a cafeteria. Assume it will be
read by somebody eventually, and make sure it cannot do anything worth doing.

## 3. Confirm the device answers

```bash
python edge/hikvision.py --host 10.0.12.40 --user queue --password THEPASSWORD
```

This prints the model and firmware, every channel with its resolution and
codec, and a config block to paste into `edge/pusher.json`.

Don't know the IP? `python edge/hikvision.py --scan` sweeps this PC's subnet
looking for Hikvision devices. It is an unauthenticated probe, so it cannot
trigger a login lock.

### Channel numbering

Hikvision numbers streaming channels as **camera × 100 + stream**, where stream
`1` is the main stream and `2` is the sub-stream:

| Channel | Meaning |
|---|---|
| `101` | camera 1, main stream (e.g. 2688×1520) |
| `102` | camera 1, sub-stream (e.g. 640×360) |
| `302` | camera 3, sub-stream |

**Always use the sub-stream.** A queue does not need 4K, and the sub-stream is
roughly a tenth of the upload and a tenth of the CPU. The same number works for
both the RTSP path and the snapshot URL:

```
rtsp://queue:PASSWORD@10.0.12.40:554/Streaming/Channels/102
http://10.0.12.40/ISAPI/Streaming/channels/102/picture
```

### If ISAPI or RTSP is switched off

Installers sometimes disable them. In the NVR web UI:

- **Configuration → Network → Advanced Settings → Integration Protocol** —
  make sure ISAPI/ONVIF is enabled.
- **Configuration → Network → Basic Settings → Port** — check RTSP is 554 and
  HTTP is 80.

## 4. Start with snapshots — no ffmpeg needed

The fastest route to a working live view. Nothing to install:

```bash
cp edge/pusher.example.json edge/pusher.json
```

Fill in the `isapi` block from step 3, set `device_key` to match the server's
`DEVICE_KEY`, then:

```bash
python edge/pusher.py --source isapi --camera cafeteria
```

That pulls a JPEG from the NVR every 1.5 seconds and posts it to the site.
About 15 KB a frame, works through any proxy, and costs the PC almost nothing.
For judging how long a queue is, it is genuinely hard to beat.

## 5. Upgrade to smooth video when you want it

```bash
winget install Gyan.FFmpeg
```

Reopen the terminal so `ffmpeg` is on `PATH`, then prove the whole path with a
test pattern before involving the camera:

```bash
python edge/pusher.py --test --camera cafeteria
```

Colour bars on the site mean everything works. Then switch to the real stream —
`source` in `pusher.json` is already the RTSP URL from step 3:

```bash
python edge/pusher.py --camera cafeteria
```

If the picture will not play, the camera is probably producing H.265, which
browsers largely do not support. Add `--transcode`.

## 6. More than one camera

Run one pusher per camera, each with its own `--camera` id. The site grows a
switcher automatically once a second camera appears — no server restart, and no
config change:

```bash
python edge/pusher.py --camera cafeteria --source rtsp://queue:PW@10.0.12.40:554/Streaming/Channels/102
```

```bash
python edge/pusher.py --camera hall --source rtsp://queue:PW@10.0.12.40:554/Streaming/Channels/202
```

Give them readable names in `server/config.json`:

```json
"cameras": [
  {"id": "cafeteria", "label": "Cafeteria queue"},
  {"id": "hall", "label": "Main hall"}
]
```

Only a pusher holding the device key can create a camera. A viewer cannot,
whatever they type in the URL.

### Watch the NVR's stream limit

A Hikvision NVR allows a fixed number of simultaneous outgoing streams, and
iVMS-4200 on that PC is already using some. Each RTSP pusher takes one more.
If streams start failing when you add cameras, that is usually the cause —
sub-streams count for less, and `--source isapi` snapshots use none at all,
which is another reason to start there.

## 7. Keep it running

The PC will reboot. Register the pusher as a scheduled task that starts at boot:

```powershell
schtasks /create /tn "QueuePusher" /sc onstart /ru SYSTEM /rl HIGHEST /f /tr "python C:\path\to\edge\pusher.py --camera cafeteria"
```

The pusher supervises ffmpeg itself — restart with backoff, plus a watchdog
that asks the *server* every ten seconds whether frames are actually arriving
for this camera. That second check matters: ffmpeg can sit there looking
healthy while the NVR has stopped sending.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `401 Unauthorized` | wrong password, or account is local-only | check in iVMS Device Management; give the account remote access |
| `403 Forbidden` | account cannot see that channel | grant live view for it |
| Nothing on `--scan` | NVR on another VLAN, or non-standard HTTP port | read the IP from iVMS and pass `--host` |
| `did not return XML` | ISAPI disabled | Network → Advanced → Integration Protocol |
| Snapshot works, RTSP doesn't | RTSP port closed or moved | Network → Basic → Port |
| Worked, now every login fails | illegal login lock tripped | wait 30 minutes; fix credentials before retrying |
| Streams fail as you add cameras | NVR stream limit reached | use sub-streams, or `--source isapi` |
| Player black, falls back to stills | camera is H.265 | add `--transcode` |

## One thing worth doing anyway

Run `edge/counter.py` against the same RTSP URL. It turns the stream into the
queue number, the wait estimate, and the busy-times forecast — which keep
working when the video is down, are readable on a locked phone, and are what
most students will actually check. Same camera, no extra hardware.

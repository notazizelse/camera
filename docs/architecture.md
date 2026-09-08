# Architecture and decisions

Everything worth knowing before you mount anything. Read this once end to end;
most of the cost of this project is in decisions, not code.

---

## 1. What you are actually building

Students want the answer to one question: *should I go now or in twenty
minutes?* That question is answered by a number and a pattern, not by footage.

This matters because "forward the CCTV to a website" and "tell students how long
the line is" look like the same project but have completely different cost,
risk, and approval profiles:

| | Forward the footage | Publish the count |
|---|---|---|
| Bandwidth out of school | ~1–4 Mbit/s, continuous | ~40 bytes / 10 s |
| Approval needed | Data protection sign-off, probably refused | Usually a conversation |
| What leaks if it's abused | Identifiable minors, on video | An integer |
| Server cost | Transcoding + egress | Rounds to zero |
| Answers the question | Only if you keep watching | Directly |

Build the second one. Sections 2–4 cover the hardware; section 8 covers what to
do if someone insists on real video anyway.

---

## 2. Getting a video source

### 2a. Tapping the school's existing CCTV

Find out which of two worlds you are in:

**IP cameras + NVR (modern).** Cameras are PoE, speak ONVIF, and the NVR exposes
RTSP. You want a *sub-stream* — every camera publishes a main stream (e.g.
2688×1520) and a sub-stream (usually 640×360 or 704×576). Use the sub-stream:
you are counting blobs, not reading name badges, and it costs a tenth of the CPU.

```
Hikvision  rtsp://user:pass@IP:554/Streaming/Channels/102     (102 = ch1 substream)
Dahua      rtsp://user:pass@IP:554/cam/realmonitor?channel=1&subtype=1
Axis       rtsp://user:pass@IP/axis-media/media.amp?resolution=640x360
Generic    use ONVIF Device Manager or `onvif-cli` to discover the real path
```

Test with VLC (`Media → Open Network Stream`) before writing any code. If VLC
can't play it, nothing downstream will.

**Analog DVR (HD-TVI / CVI / AHD, common in older schools).** Cameras are coax
into a DVR. Most DVRs from the last decade still expose RTSP over Ethernet, so
you may be fine. If not, your options are a cheap HDMI/CVBS capture dongle on
the DVR's spot-monitor output, or — far simpler — skip the DVR entirely and put
your own camera up.

**What you need from the school either way:** a read-only viewer account (never
the admin credentials), the camera's IP, and a port on the VLAN the cameras live
on. IT will usually keep CCTV on an isolated VLAN, and that is the right call —
plan for your Pi to have two networks (camera VLAN in, general network out) or
for IT to allow one narrow route.

### 2b. Your own camera (recommended for a first build)

Politically and technically easier. You are not touching a security system, you
choose the angle, and you can point it at the floor where the line forms rather
than at faces.

| Option | Notes |
|---|---|
| **Pi Camera Module 3** | Best pick. Ribbon to the Pi, no network, wide-angle version covers a corridor. |
| **USB webcam** | Works instantly with OpenCV. Cable length is the constraint (~5 m without a powered extender). |
| **ESP32-CAM (OV2640)** | Cheap, wireless, but see §3 — treat it as a camera, not a counter. |
| **Old Android phone** | Genuinely good: decent sensor, IP Webcam app gives you an MJPEG/RTSP URL, and it's free. Excellent for prototyping. |

**Mounting matters more than the algorithm.** A camera looking down the line at
a shallow angle gives you heavy occlusion — people hide behind each other and
you undercount badly at exactly the busy times you care about. Mount high
(2.5–3 m) and look *across* or *down* onto the queue. An overhead-ish view is
worth more accuracy than any model upgrade.

---

## 3. Where the counting runs — and why not on an ESP32

You listed STM32, ESP32, Arduino and Raspberry Pi. Direct answers:

**STM32 / Arduino: not for vision here.** An STM32H7 can technically run a tiny
person detector on a small frame, but you would spend the whole project fighting
memory layouts to get worse results than a $60 Pi delivers in an afternoon. They
are the right chips for the *sensor* alternatives in §4.

**ESP32 / ESP32-CAM: a great camera, a bad counter.** The AI-Thinker ESP32-CAM
streams MJPEG happily and costs almost nothing. On-device person detection
(ESP-DL/`person_detect` on an ESP32-S3) works on a 96×96 grayscale frame and
answers "is there a person" — it will not reliably count nine people in a
crowded queue. Two sound ways to use one:

- **ESP32-CAM as camera, Pi as counter.** The ESP32 streams MJPEG on the local
  network; the Pi pulls it and counts. Cheap wireless camera placement.
- **ESP32-CAM posts JPEGs, server counts.** Simplest wiring, but now images
  leave the building — you have given up the main privacy advantage. Only do
  this if the server is also inside the school.

**Raspberry Pi: the right tool.** Not "only if necessary" — it is the piece that
makes the privacy story true, because it is what lets the counting happen inside
the kitchen.

| Board | Model | Realistic rate | Verdict |
|---|---|---|---|
| Pi Zero 2 W | MobileNet-SSD (`dnn`) | ~1–2 fps | Works. You sample every 1–2 s anyway. |
| Pi 4 (4 GB) | MobileNet-SSD | ~4–6 fps | Comfortable. |
| Pi 4 | YOLOv8n @ 480 | ~1.5–3 fps | Fine, but hot. |
| Pi 5 | YOLOv8n @ 480 | ~6–10 fps | The sweet spot. |
| Pi 5 + Hailo-8L | YOLOv8s | 30+ fps | Overkill for one queue. |
| Any old x86 mini PC | YOLOv8n | 15+ fps | If you have one, use it. Free. |

**You do not need frame rate.** A queue does not change meaningfully in one
second. Inference once per second, smoothed with an EMA over ~30 s, is more
stable *and* cheaper than running flat out. The counter in `edge/counter.py`
decouples the three rates deliberately: capture as fast as the camera sends,
infer once a second, publish once every ten.

---

## 4. Alternatives that avoid cameras entirely

Worth taking seriously — several are cheaper, and one might get approved in a
day when a camera takes a term. These are where your STM32/ESP32 skills pay off.

| Approach | Hardware | Cost | Honest assessment |
|---|---|---|---|
| **Thermal array** | MLX90640 (32×24) + ESP32 | ~$50 | Strong compromise. Counts warm bodies; physically incapable of identifying anyone, which ends the privacy conversation immediately. Overhead mount. |
| **mmWave radar** | LD2450 / MR60 + ESP32 | ~$15 | Tracks several moving targets, no imaging at all. Struggles with a *stationary* queue — people standing still are what you're counting. Test before committing. |
| **Break-beam in/out** | 2× IR gates + ESP32 | ~$10 | Counts entries minus exits. Drifts all day and needs a nightly reset to zero. Fine for a room's occupancy, poor for a queue. |
| **ToF depth line** | VL53L5CX array | ~$25 | Good over a doorway, weak over an open queue area. |
| **Till / POS rate** | Software integration | free | The best *service-rate* signal you can get, and it's exact. Won't tell you the queue length, but combined with a counter it makes the wait estimate genuinely accurate. Ask the canteen manager. |
| **Wi-Fi probe counting** | ESP32 in monitor mode | ~$5 | Don't. MAC randomisation broke the accuracy and it's the most legally exposed option on this list. |

If you want the shortest path to a working system that nobody objects to:
**MLX90640 overhead + ESP32 + this server**. Replace `edge/counter.py` with a
sketch that POSTs the same `{"count": N}` payload — the rest of the stack is
unchanged, because the interface between edge and server is one integer.

---

## 5. Getting data out of a school network

This is where most student projects die, and it has nothing to do with cameras.

**Assume all of these are true**, because they usually are: no inbound ports, no
port forwarding, no static IP, no control over DNS, a firewall that blocks
unusual outbound ports, possibly a proxy, and a separate VLAN for anything
CCTV-adjacent.

**The one pattern that works: outbound-only.** The edge device *initiates* every
connection. Nothing listens. This project uses plain HTTPS POST because it
survives proxies and needs no special ports; MQTT over TLS (8883) is the other
good answer if you'd rather have a broker.

Design consequences already handled in `edge/counter.py`:

- Samples buffer in RAM and flush when the link returns, so a Wi-Fi drop costs
  you live freshness but not history.
- Retries use exponential backoff, so a server outage doesn't hammer the network.
- The device holds no inbound surface at all.

**Do not port-forward the camera or the NVR.** Internet-exposed DVRs are how
Mirai built a botnet; they are indexed on Shodan within hours and school CCTV
credentials are frequently the default ones. If you need remote access to
something inside, use an outbound tunnel — Cloudflare Tunnel or Tailscale — and
tell IT you're doing it.

**Where to put the server.** Either is fine:

- *Outside* (a $5 VPS, or a free tier): works on phones on mobile data, off
  campus, at home. Needs the school to allow outbound HTTPS, which it does.
- *Inside* (on the Pi itself, or a school box): zero data leaves the building at
  all, which is the easiest possible approval conversation — but the site only
  works on school Wi-Fi.

Start inside if approval is the bottleneck; the code is identical.

---

## 6. Turning a count into a useful answer

A raw number is less useful than it looks. Three things make it usable:

**Smoothing.** Detection is noisy frame to frame. An exponential moving average
(`smoothing: 0.35` in the edge config) stops the headline number flickering
between 11 and 14 and making the site look broken.

**Wait time — Little's Law.** For a queue in steady state, `W = L / λ`: waiting
time equals queue length divided by throughput. In practice:

```
wait_seconds = people_in_queue × seconds_to_serve_one_person
```

Measure `seconds_to_serve_one_person` with a stopwatch — 20 people at lunch,
divide. School canteens typically land at 8–15 s. The server also infers it from
how fast the queue drains, but the inference is *clamped to ±2×* your measured
value: a shrinking queue reflects service minus new arrivals, so a naive
inference systematically overestimates the wait, and an overestimate sends
students away hungry.

**The weekday pattern is the killer feature.** "14 people right now" is mildly
useful. "It's always mobbed at 12:05 and clear by 12:25" changes behaviour, and
it works even when the counter is offline. It needs about two weeks of history
before it means anything, which is a good reason to deploy the counter quietly
before you announce the website.

Second-order effect worth knowing: if this works, it flattens the peak. Students
shift away from the rush, which makes the rush smaller, which makes the
prediction wrong. That is a success, not a bug — but don't be surprised when
your beautiful histogram flattens out after a month.

---

## 7. The approval conversation

You will need the head, the IT lead, and the canteen manager. What actually
gets a yes:

- **Lead with the count, not the camera.** "A sensor that counts how many people
  are in the line" is a different sentence from "put the CCTV on a website."
- **Bring the number in writing.** One page: what is captured (nothing kept),
  what is transmitted (one integer every ten seconds), where it goes, who can
  see it, and how to switch it off.
- **Name the legal basis.** Depending on where you are this is GDPR (UK/EU), the
  DPDP Act (India), FERPA-adjacent policy (US), or your national equivalent, and
  the school will have a data protection officer or a designated staff member.
  Ask them rather than guessing. Counting is a much easier case than filming,
  and pointing that out yourself buys a lot of credibility.
- **Put up a sign.** In the kitchen: what the device does, that it stores no
  images, and who to ask. Cheap, and it prevents the rumour that you installed
  spy cameras.
- **Offer a kill switch.** A named staff member who can unplug it, no discussion
  needed.
- **Say what happens to the data.** This project keeps 28 days of counts and
  overwrites the file. Say that out loud.

If you enable the pixelated snapshot (`snapshot_enabled`), treat that as a
separate conversation, not something bundled into the first approval. It is a
~20×15-pixel frame, but it is still an image, and people are right to ask.

---

## 8. If someone insists on real video

Occasionally the answer is "we want to see it." Here is the honest technical
picture, so you can implement it properly rather than badly.

**Cameras speak RTSP. Browsers don't.** Something must translate:

| Protocol | Latency | Complexity | When |
|---|---|---|---|
| **MJPEG** | ~1 s | Trivial (`<img src>`) | 1–3 fps, one viewer, internal only. Bandwidth is brutal at scale. |
| **HLS** | 6–20 s | Low (ffmpeg → `.m3u8`) | Many viewers, CDN-friendly. Latency is fine for a queue. |
| **LL-HLS** | 2–5 s | Medium | Better, fiddlier. |
| **WebRTC** | <1 s | High (STUN/TURN) | Sub-second. You do not need sub-second to look at a queue. |

**Don't build the translator.** Use [MediaMTX](https://github.com/bluenviron/mediamtx)
or [go2rtc](https://github.com/AlexxIT/go2rtc) — single Go binary, ingests RTSP,
publishes HLS/WebRTC/MJPEG, handles reconnects. One config file.

If you do this, you must also: put it behind school SSO or at minimum a shared
password, serve it over HTTPS only, disable recording, and get explicit written
sign-off. And note that the count is *still* the better product — consider
shipping video only to a staff dashboard and the count to students.

**Middle ground worth knowing about:** the anonymised snapshot this project
supports. The frame is destroyed to ~20×15 effective pixels and people are drawn
as flat silhouettes *before* JPEG encoding, so the shape of the queue survives
and nothing else does. It satisfies "I want to see it" for most people at a
fraction of the risk.

---

## 9. Cost and parts

| Item | Approx |
|---|---|
| Raspberry Pi 5 (4 GB) | $60 |
| Pi Camera Module 3 (wide) | $35 |
| PSU, case, SD card | $30 |
| Mount / enclosure / cable | $20 |
| VPS (or free tier / on-prem) | $0–5 / month |
| Domain | ~$12 / year |
| **Total** | **~$150 + change** |

The MLX90640 thermal build comes in around $70 all-in and needs no Pi.

---

## 10. Build order

1. Run the simulator and the website locally. Show it to people. Get a reaction
   before you buy anything. *(You are here.)*
2. Get one camera working with `--preview` on a laptop, pointed at any queue.
3. Write the one-page proposal from §7. Get a yes.
4. Mount the Pi, draw the ROI, run for two weeks with the site unpublished.
   Sanity-check the counts against reality by standing there a few times.
5. Calibrate `service_seconds_per_person` with a stopwatch.
6. Publish. Put a QR code on the canteen wall.
7. Watch the peak flatten.

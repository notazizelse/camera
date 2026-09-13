# Getting the site online

## Netlify will not work for this, and it is worth knowing why

Netlify hosts static files plus serverless functions. This project needs
neither of those things for its hard part.

The relay has to accept a video segment every two seconds, hold roughly the
last twelve seconds **in memory**, and hand those exact bytes back to whoever
is watching. Then it has to hold a Server-Sent Events connection open for the
live count, and often an MJPEG connection too. Serverless functions are the
wrong shape for all of it:

| What this needs | What a serverless function does |
|---|---|
| Memory shared between requests | Each invocation is isolated; nothing persists |
| Connections held open for minutes | Synchronous functions are capped at seconds |
| Continuous ingest, all day | Billed and quota'd per invocation |

That last row is the one that settles it. HLS ingest alone is a playlist PUT
plus a segment PUT every two seconds — around 86,000 invocations a day, before
a single student loads the page, and each viewer then fetches a segment every
two seconds of their own. A free-tier monthly allowance (on the order of
100,000 invocations) is gone in about a day. You could push segments into
Netlify Blobs instead of memory, but you would be paying storage latency and
per-operation cost to rebuild something a single small process does for free.

**Netlify is a good fit for a static site. This is not a static site.**

Below are three deployments that do work, cheapest and simplest first.

---

## Option A — run it on the cafeteria PC, publish with a tunnel

**Recommended.** The PC is already always on, already sees the camera, and
already has an outbound internet connection. Nothing else is needed: no VPS,
no hosting account, no monthly cost, no credit card.

```powershell
.\deploy\setup-pc.ps1 -NvrHost 10.0.12.40 -NvrUser queue -Tunnel -RegisterTasks
```

A Cloudflare Tunnel dials **out** from the PC to Cloudflare, which gives you a
public HTTPS URL that forwards back down that connection. No inbound port, no
firewall change, nothing exposed on the school network — the same outbound-only
property the rest of this project is built around.

```powershell
.\deploy\run-tunnel.ps1
```

It prints a URL like `https://random-words-here.trycloudflare.com`. That free
quick-tunnel URL changes every restart, which is fine for testing. For a stable
address, register a named tunnel on a domain you control — Cloudflare's docs
cover it, and the free plan is enough.

**Trade-off:** if the PC is off or the school internet is down, the site is
down. For a canteen queue, that is the correct amount of reliability.

## Option B — server on a small host, pusher on the PC

Use this if the site must stay up when the PC does not, or if IT would rather
nothing inbound-ish ran on a school machine at all.

The server is a single stdlib Python process with no build step, so almost
anything runs it: a $4–5/month VPS, Fly.io, Railway, or Render. Two things to
check before picking:

- **It must not sleep.** Free tiers that idle a service after inactivity will
  cut the ingest stream. Check this first; it disqualifies several free plans.
- **The proxy must not buffer.** SSE and MJPEG both break if it does. The
  `deploy/Caddyfile` sets `flush_interval -1` for exactly this reason.

Then point the PC at it:

```powershell
.\deploy\setup-pc.ps1 -NvrHost 10.0.12.40 -NvrUser queue
```

and edit `server_url` in `edge/pusher.json` to the public address. Use
`https://` — the device key travels in a header.

## Option C — Netlify for the front-end only

Technically possible: put `web/` on Netlify and run the server from Option A or
B for the API. But the page is four small files served by the same process that
holds the video, and splitting them means CORS, plus `SameSite=None; Secure` on
the access cookie so it survives a cross-origin request.

You would add a second deployment, a cookie policy change, and a CORS surface,
to move 30 KB of static files. It is not worth it.

---

## Before you publish

Putting a school cafeteria on a public URL is the step that needs a
conversation, not a config change.

- **Set an access code.** `setup-pc.ps1` generates one. Without `VIEW_CODE` the
  server refuses to serve video at all — that is deliberate.
- **Decide who gets the code**, and how it is changed if it spreads.
- **Put it in writing.** One page: what is captured, what is kept (nothing —
  the relay holds seconds in RAM and overwrites), who can see it, and who can
  switch it off. `docs/architecture.md` §7 covers what actually gets a yes.
- **Name someone who can stop it.** A staff member who can close the tunnel, no
  discussion needed.
- **Consider publishing only the count at first.** Run `edge/counter.py` and
  leave `VIEW_CODE` unset. The count is public, needs no approval conversation
  of the same weight, and is what most students will actually check. Add the
  picture later if people ask for it.

## Checking a deployment

```bash
curl -s https://your-url/api/state
```

```bash
curl -sN https://your-url/api/stream | head -c 300
```

The second should print a `data: {...}` line immediately and hold the
connection open. If it returns and exits straight away, something upstream is
buffering and the live count will appear frozen to every viewer.

```bash
curl -s https://your-url/api/media-state
```

`enabled: false` means `VIEW_CODE` is unset. `available: false` means the
pusher is not reaching the server.

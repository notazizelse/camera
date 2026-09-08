# Deploying

```bash
sudo useradd --system --home /opt/kitchen-queue queue
sudo mkdir -p /opt/kitchen-queue && sudo chown queue: /opt/kitchen-queue
# copy the repo to /opt/kitchen-queue
sudo cp deploy/queue-server.service /etc/systemd/system/
sudo systemctl enable --now queue-server
```

Put `DEVICE_KEY` in the unit file (or better, an `EnvironmentFile` with mode
0600) and the *same* value in `edge/config.json` on the Pi.

## HTTPS

Caddy, because it obtains and renews certificates by itself:

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile   # edit the hostname first
sudo systemctl reload caddy
```

`flush_interval -1` is not optional. Without it the proxy buffers the SSE
stream and the live count silently stops updating.

## If you have no public server

Run everything on the Pi and reach it on the school network at
`http://<pi-ip>:8080`, or expose just the server with an outbound tunnel:

```bash
cloudflared tunnel --url http://localhost:8080
```

An outbound tunnel needs no firewall change and opens no inbound port. Tell IT
you are running one anyway - discovering it later looks much worse than
mentioning it now.

## Checks after deploying

```bash
curl -s https://queue.yourschool.example/api/state | python3 -m json.tool
curl -sN https://queue.yourschool.example/api/stream | head -c 400
```

The second should print a `data: {...}` line immediately and keep the
connection open. If it returns and exits, SSE buffering is still on.

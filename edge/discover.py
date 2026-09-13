#!/usr/bin/env python3
"""Find the RTSP URL of the camera you can already see.

NVR software almost never tells you the URL it is using, so this asks the
network instead:

  1. ONVIF WS-Discovery - a multicast probe most IP cameras and recorders
     answer with their own address.
  2. For each address found (or the ones you pass in), try the RTSP paths the
     major vendors use, with a real DESCRIBE request including digest auth.

Anything that answers 200 is a URL you can hand straight to pusher.py.

    python discover.py --user viewer --password secret
    python discover.py --host 10.0.12.40 --user viewer --password secret

Ask IT for a *read-only viewer* account. Never put the NVR admin password in
a config file on a PC in a cafeteria.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import socket
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

WS_DISCOVERY = ("239.255.255.250", 3702)

PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
 <e:Header>
  <w:MessageID>uuid:{mid}</w:MessageID>
  <w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
  <w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
 </e:Header>
 <e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>
</e:Envelope>"""

# Channel 1 sub-stream first: it is the one you want for a queue, and the one
# that will not saturate the upload.
PATHS = [
    ("Hikvision", "/Streaming/Channels/102"),
    ("Hikvision", "/Streaming/Channels/101"),
    ("Dahua", "/cam/realmonitor?channel=1&subtype=1"),
    ("Dahua", "/cam/realmonitor?channel=1&subtype=0"),
    ("Axis", "/axis-media/media.amp?resolution=640x360"),
    ("Axis", "/axis-media/media.amp"),
    ("Uniview", "/media/video2"),
    ("Uniview", "/media/video1"),
    ("Reolink", "/h264Preview_01_sub"),
    ("Amcrest", "/cam/realmonitor?channel=1&subtype=1"),
    ("Foscam/generic", "/videoSub"),
    ("Generic", "/live/ch1"),
    ("Generic", "/live/ch00_1"),
    ("Generic", "/11"),
    ("Generic", "/stream1"),
    ("Generic", "/h264"),
    ("Generic", "/"),
]


# ---------------------------------------------------------------- discovery


def ws_discover(timeout: float = 3.0) -> list[str]:
    """Multicast probe for ONVIF devices. Returns bare host addresses."""
    message = PROBE.format(mid=uuid.uuid4()).encode()
    hosts: set[str] = set()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(timeout)
    try:
        sock.sendto(message, WS_DISCOVERY)
        while True:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                break
            hosts.add(addr[0])
            for match in re.findall(rb"https?://([^/\s:]+)", data):
                hosts.add(match.decode())
    except OSError as exc:
        print(f"  (multicast probe failed: {exc})")
    finally:
        sock.close()
    return sorted(hosts)


# --------------------------------------------------------------------- rtsp


def _digest(user: str, password: str, header: str, uri: str) -> str:
    fields = dict(re.findall(r'(\w+)="([^"]*)"', header))
    realm, nonce = fields.get("realm", ""), fields.get("nonce", "")
    ha1 = hashlib.md5(f"{user}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"DESCRIBE:{uri}".encode()).hexdigest()
    response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    return (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{response}"')


def describe(host: str, port: int, path: str, user: str, password: str,
             timeout: float = 2.5) -> tuple[int, str]:
    """One RTSP DESCRIBE, retried once with whatever auth the device demands."""
    uri = f"rtsp://{host}:{port}{path}"
    auth = ""
    for attempt in range(2):
        request = (f"DESCRIBE {uri} RTSP/1.0\r\nCSeq: {attempt + 1}\r\n"
                   f"Accept: application/sdp\r\nUser-Agent: queue-discover\r\n")
        if auth:
            request += f"Authorization: {auth}\r\n"
        request += "\r\n"
        try:
            with socket.create_connection((host, port), timeout) as sock:
                sock.settimeout(timeout)
                sock.sendall(request.encode())
                reply = b""
                while b"\r\n\r\n" not in reply and len(reply) < 65536:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    reply += chunk
        except OSError:
            return 0, ""

        text = reply.decode("utf-8", "replace")
        status = int(text.split(" ")[1]) if text.startswith("RTSP/1.0 ") else 0
        if status != 401 or attempt:
            return status, text

        challenge = ""
        for line in text.splitlines():
            if line.lower().startswith("www-authenticate:"):
                challenge = line.split(":", 1)[1].strip()
                break
        if challenge.lower().startswith("digest"):
            auth = _digest(user, password, challenge, uri)
        else:
            import base64
            token = base64.b64encode(f"{user}:{password}".encode()).decode()
            auth = f"Basic {token}"
    return 0, ""


def codec_of(sdp: str) -> str:
    codecs = re.findall(r"a=rtpmap:\d+ ([A-Za-z0-9\-]+)/", sdp)
    video = [c for c in codecs if c.upper() in ("H264", "H265", "HEVC", "JPEG", "MP4V-ES")]
    return video[0] if video else (codecs[0] if codecs else "?")


def probe_host(host: str, port: int, user: str, password: str,
               max_auth_failures: int = 2) -> list[tuple[str, str, str]]:
    """Try the known paths, but stop early if the credentials are being refused.

    Hikvision devices lock out an IP address after a handful of failed logins
    (Configuration -> System -> Security -> Illegal Login Lock), typically for
    30 minutes. Walking all seventeen paths with a wrong password would lock
    the cafeteria PC out of the school's own CCTV. So the moment it looks like
    an auth problem rather than a path problem, back off and say so.
    """
    found = []
    auth_failures = 0
    for vendor, path in PATHS:
        status, text = describe(host, port, path, user, password)
        if status == 200:
            found.append((f"rtsp://{host}:{port}{path}", vendor, codec_of(text)))
        elif status == 401:
            # The path exists but the credentials were refused - worth saying
            # so, because it means you have the right URL and the wrong login.
            found.append((f"rtsp://{host}:{port}{path}", vendor, "AUTH FAILED"))
            auth_failures += 1
            if auth_failures >= max_auth_failures:
                print(f"  {host}: credentials refused twice - stopping before the "
                      "device locks this IP out. Fix --user/--password first.")
                break
    return found


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Find a camera's RTSP URL")
    ap.add_argument("--host", action="append", default=[],
                    help="skip discovery and probe this address (repeatable)")
    ap.add_argument("--port", type=int, default=554)
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="")
    ap.add_argument("--timeout", type=float, default=3.0)
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    hosts = args.host
    if not hosts:
        print("probing the network for ONVIF devices...")
        hosts = ws_discover(args.timeout)
        if not hosts:
            print("\nNothing answered. That is common - many NVRs have discovery\n"
                  "switched off, and multicast rarely crosses a VLAN boundary.\n"
                  "Get the camera's IP from the NVR software and pass it:\n"
                  "    python discover.py --host 10.0.12.40 --user viewer --password ...")
            return
        print(f"found {len(hosts)}: {', '.join(hosts)}")

    print(f"\ntrying {len(PATHS)} known paths on each, as user {args.user!r}...\n")
    results: list[tuple[str, str, str]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for batch in pool.map(
            lambda h: probe_host(h, args.port, args.user, args.password), hosts
        ):
            results.extend(batch)

    working = [r for r in results if r[2] != "AUTH FAILED"]
    refused = [r for r in results if r[2] == "AUTH FAILED"]

    if working:
        print("WORKING - put one of these in pusher.json as \"source\":\n")
        for url, vendor, codec in working:
            print(f"  {url}\n      {vendor}, {codec}")
    if refused:
        print("\nRIGHT URL, WRONG LOGIN - retry with correct --user/--password:\n")
        for url, vendor, _ in refused:
            print(f"  {url}   ({vendor})")
    if not results:
        print("No RTSP path answered.\n"
              "If the NVR software will not expose a stream at all, capture its\n"
              "window instead:  python pusher.py --source \"window:<exact title>\"")


if __name__ == "__main__":
    main()

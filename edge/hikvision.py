#!/usr/bin/env python3
"""Talk to the Hikvision NVR that iVMS-4200 is already connected to.

iVMS-4200 is a client, not a source. It connects to a Hikvision NVR or camera
over the SDK port (8000), and that same device also speaks two open protocols
this project can use directly:

    ISAPI  (port 80)   HTTP + digest auth. Device info, channel list, and
                       single JPEG snapshots. No ffmpeg needed at all.
    RTSP   (port 554)  the live H.264 stream.

Going to the device directly beats screen-grabbing the iVMS window: the
picture is sharper, it costs a fraction of the CPU, and it does not break
when somebody minimises a window or logs the PC out.

    python hikvision.py --scan
    python hikvision.py --host 10.0.12.40 --user viewer --password secret

The second prints the device, every channel, and config you can paste
straight into pusher.json.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import re
import socket
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

DEFAULT_TIMEOUT = 6.0


class HikvisionError(Exception):
    pass


def _strip_namespaces(root: ET.Element) -> ET.Element:
    """Hikvision tags carry a schema namespace that varies by firmware.

    Stripping it means the parsing below does not have to care which
    firmware generation answered.
    """
    for element in root.iter():
        if isinstance(element.tag, str) and "}" in element.tag:
            element.tag = element.tag.split("}", 1)[1]
    return root


class Hikvision:
    """Minimal ISAPI client. Standard library only."""

    def __init__(self, host: str, user: str, password: str,
                 port: int = 80, rtsp_port: int = 554,
                 scheme: str = "http", timeout: float = DEFAULT_TIMEOUT):
        self.host = host
        self.user = user
        self.password = password
        self.port = port
        self.rtsp_port = rtsp_port
        self.scheme = scheme
        self.timeout = timeout

        self.base = f"{scheme}://{host}:{port}"
        manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        manager.add_password(None, self.base, user, password)
        # Cameras vary: older firmware wants Basic, newer insists on Digest.
        # Installing both handlers lets urllib answer whichever is demanded.
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPDigestAuthHandler(manager),
            urllib.request.HTTPBasicAuthHandler(manager),
        )

    # ---------- transport ----------

    def get(self, path: str) -> bytes:
        """GET an ISAPI path, answering whichever auth scheme is demanded.

        The opener negotiates from the device's challenge, which on current
        Hikvision firmware is always Digest. Nothing may be sent pre-emptively
        here: a header set on the Request outranks the one urllib's digest
        handler adds on retry, so a pre-emptive Basic header silently defeats
        digest auth entirely. Pre-emptive Basic is only a fallback, for the
        older cameras that accept it without ever issuing a challenge.
        """
        url = f"{self.base}{path}"
        try:
            with self.opener.open(
                urllib.request.Request(url), timeout=self.timeout
            ) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                try:
                    return self._get_preemptive_basic(url)
                except urllib.error.HTTPError:
                    pass
                raise HikvisionError(
                    "401 Unauthorized - wrong username or password, or this "
                    "account lacks remote access"
                ) from exc
            if exc.code == 403:
                raise HikvisionError(
                    "403 Forbidden - the account exists but is not allowed to "
                    "view this channel"
                ) from exc
            raise HikvisionError(f"HTTP {exc.code} from {path}") from exc
        except urllib.error.URLError as exc:
            raise HikvisionError(f"cannot reach {self.host}:{self.port} - {exc.reason}") from exc

    def _get_preemptive_basic(self, url: str) -> bytes:
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        request = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read()

    def get_xml(self, path: str) -> ET.Element:
        raw = self.get(path)
        try:
            return _strip_namespaces(ET.fromstring(raw))
        except ET.ParseError as exc:
            raise HikvisionError(f"{path} did not return XML (ISAPI disabled?)") from exc

    # ---------- device ----------

    def device_info(self) -> dict:
        root = self.get_xml("/ISAPI/System/deviceInfo")
        text = lambda tag: (root.findtext(tag) or "").strip()  # noqa: E731
        return {
            "name": text("deviceName"),
            "model": text("model"),
            "serial": text("serialNumber"),
            "firmware": f"{text('firmwareVersion')} {text('firmwareReleasedDate')}".strip(),
            "type": text("deviceType"),
        }

    def channels(self) -> list[dict]:
        """Every streaming channel the device offers.

        The ids are the useful part: Hikvision numbers them as
        channel * 100 + stream, where stream 1 is the main stream and 2 the
        sub-stream. Camera 3's sub-stream is 302. The same id works for both
        the RTSP path and the ISAPI snapshot URL.
        """
        root = self.get_xml("/ISAPI/Streaming/channels")
        found = []
        for channel in root.findall("StreamingChannel"):
            cid = (channel.findtext("id") or "").strip()
            if not cid.isdigit():
                continue
            video = channel.find("Video")
            width = height = None
            if video is not None:
                width = video.findtext("videoResolutionWidth")
                height = video.findtext("videoResolutionHeight")
            found.append({
                "id": int(cid),
                "camera": int(cid) // 100,
                "stream": "main" if int(cid) % 100 == 1 else "sub",
                "name": (channel.findtext("channelName") or "").strip(),
                "enabled": (channel.findtext("enabled") or "true").strip() == "true",
                "codec": (video.findtext("videoCodecType") if video is not None else "") or "",
                "resolution": f"{width}x{height}" if width and height else "",
            })
        return sorted(found, key=lambda c: c["id"])

    def camera_names(self) -> dict[int, str]:
        """Friendly names, which on an NVR live with the proxied inputs."""
        names: dict[int, str] = {}
        try:
            root = self.get_xml("/ISAPI/ContentMgmt/InputProxy/channels")
        except HikvisionError:
            return names
        for proxy in root.findall("InputProxyChannel"):
            cid = (proxy.findtext("id") or "").strip()
            name = (proxy.findtext("name") or "").strip()
            if cid.isdigit() and name:
                names[int(cid)] = name
        return names

    # ---------- media ----------

    def snapshot(self, channel_id: int) -> bytes:
        """One JPEG, straight from the device. No ffmpeg in sight.

        This is the quickest way to get a working live view: a couple of
        these a second is all a queue page needs, and it needs nothing
        installed on the PC.
        """
        data = self.get(f"/ISAPI/Streaming/channels/{channel_id}/picture")
        if not data.startswith(b"\xff\xd8"):
            raise HikvisionError(
                f"channel {channel_id} did not return a JPEG "
                "(is the channel online?)"
            )
        return data

    def rtsp_url(self, channel_id: int, with_credentials: bool = True) -> str:
        auth = f"{self.user}:{self.password}@" if with_credentials else ""
        return f"rtsp://{auth}{self.host}:{self.rtsp_port}/Streaming/Channels/{channel_id}"

    def rtsp_reachable(self) -> bool:
        try:
            with socket.create_connection((self.host, self.rtsp_port), 3):
                return True
        except OSError:
            return False


# ---------------------------------------------------------------------------
# finding the device
# ---------------------------------------------------------------------------


def local_subnet() -> str | None:
    """The /24 this PC sits on, which is where the NVR almost always is."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))     # no packets sent; just picks a route
        address = probe.getsockname()[0]
        probe.close()
        return address.rsplit(".", 1)[0]
    except OSError:
        return None


def looks_hikvision(host: str, timeout: float = 1.0) -> dict | None:
    """Unauthenticated probe: does something Hikvision-shaped answer here?

    ISAPI returns 401 with a Hikvision-flavoured challenge before it will
    tell you anything, and that 401 is itself the fingerprint.
    """
    try:
        with socket.create_connection((host, 80), timeout) as sock:
            sock.sendall(
                f"GET /ISAPI/System/deviceInfo HTTP/1.1\r\nHost: {host}\r\n"
                "Connection: close\r\n\r\n".encode()
            )
            sock.settimeout(timeout)
            data = b""
            while len(data) < 2048:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                data += chunk
    except OSError:
        return None

    text = data.decode("utf-8", "replace")
    if "401" not in text.split("\r\n")[0] and "200" not in text.split("\r\n")[0]:
        return None
    if not re.search(r"hikvision|realm=\"[^\"]*DS-|ISAPI", text, re.I):
        return None
    realm = re.search(r'realm="([^"]*)"', text)
    return {"host": host, "realm": realm.group(1) if realm else ""}


def scan(subnet: str, workers: int = 64) -> list[dict]:
    hosts = [f"{subnet}.{n}" for n in range(1, 255)]
    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(looks_hikvision, hosts):
            if result:
                found.append(result)
    return found


# ---------------------------------------------------------------------------


def report(device: Hikvision) -> int:
    print(f"connecting to {device.host}:{device.port} as {device.user!r}\n")
    try:
        info = device.device_info()
    except HikvisionError as exc:
        print(f"  FAILED: {exc}\n")
        print("Things to check, in order:")
        print("  - the IP, username and password shown in iVMS-4200 under")
        print("    Maintenance and Management -> Device Management")
        print("  - that the account is not restricted to the local console")
        print("  - Configuration -> Network -> Advanced -> Integration Protocol,")
        print("    where ISAPI/ONVIF can be switched off by an installer")
        return 1

    for key, value in info.items():
        if value:
            print(f"  {key:9} {value}")

    names = device.camera_names()
    try:
        channels = device.channels()
    except HikvisionError as exc:
        print(f"\n  could not list channels: {exc}")
        return 1

    print(f"\n{len(channels)} channel(s):\n")
    for channel in channels:
        label = names.get(channel["camera"], channel["name"]) or f"camera {channel['camera']}"
        flags = "" if channel["enabled"] else "  (disabled)"
        detail = " ".join(x for x in (channel["codec"], channel["resolution"]) if x)
        print(f"  {channel['id']:>4}  {label:<24} {channel['stream']:<5} {detail}{flags}")

    print(f"\nRTSP port 554 reachable: {'yes' if device.rtsp_reachable() else 'NO'}")

    subs = [c for c in channels if c["stream"] == "sub" and c["enabled"]]
    pick = subs[0] if subs else (channels[0] if channels else None)
    if not pick:
        return 1

    label = names.get(pick["camera"], pick["name"]) or f"camera {pick['camera']}"
    print("\n" + "-" * 68)
    print("Pick the channel showing the cafeteria queue, then paste into")
    print("edge/pusher.json (sub-streams are the right choice - a queue does")
    print("not need 4K, and the sub-stream is a tenth of the upload):\n")
    print(json.dumps({
        "source": device.rtsp_url(pick["id"]),
        "isapi": {
            "host": device.host,
            "port": device.port,
            "user": device.user,
            "password": device.password,
            "channel": pick["id"],
        },
        "camera_id": "cafeteria",
        "camera_label": label,
    }, indent=2))
    print("\nNo ffmpeg installed yet? This works right now, with nothing to install:")
    print(f"  python edge/pusher.py --source isapi --camera cafeteria")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect the Hikvision device behind iVMS-4200")
    parser.add_argument("--host", help="NVR/camera IP, from iVMS-4200 Device Management")
    parser.add_argument("--port", type=int, default=80, help="ISAPI/HTTP port")
    parser.add_argument("--rtsp-port", type=int, default=554)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="")
    parser.add_argument("--https", action="store_true")
    parser.add_argument("--scan", action="store_true",
                        help="search this PC's subnet for Hikvision devices")
    parser.add_argument("--snapshot", metavar="FILE",
                        help="save one JPEG from --channel and exit")
    parser.add_argument("--channel", type=int, default=102)
    parser.add_argument("--json", action="store_true",
                        help="machine-readable device + channel list, for scripts")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if args.scan:
        subnet = local_subnet()
        if not subnet:
            print("could not work out this PC's subnet; pass --host instead")
            return 1
        print(f"scanning {subnet}.1-254 for Hikvision devices...\n")
        found = scan(subnet)
        if not found:
            print("none found.\n"
                  "The NVR may be on another VLAN, or on a non-standard HTTP port.\n"
                  "Read the IP straight out of iVMS-4200: Maintenance and Management\n"
                  "-> Device Management. Then re-run with --host.")
            return 1
        for entry in found:
            print(f"  {entry['host']}   {entry['realm']}")
        print("\nNow run:")
        print(f"  python edge/hikvision.py --host {found[0]['host']} "
              "--user admin --password THEPASSWORD")
        return 0

    if not args.host:
        parser.error("pass --host, or --scan to search for it")

    device = Hikvision(
        args.host, args.user, args.password,
        port=args.port, rtsp_port=args.rtsp_port,
        scheme="https" if args.https else "http",
    )

    if args.json:
        try:
            payload = {
                "ok": True,
                "device": device.device_info(),
                "names": {str(k): v for k, v in device.camera_names().items()},
                "channels": device.channels(),
                "rtsp_reachable": device.rtsp_reachable(),
            }
        except HikvisionError as exc:
            payload = {"ok": False, "error": str(exc)}
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.snapshot:
        try:
            data = device.snapshot(args.channel)
        except HikvisionError as exc:
            print(f"snapshot failed: {exc}")
            return 1
        with open(args.snapshot, "wb") as fh:
            fh.write(data)
        print(f"wrote {len(data)} bytes to {args.snapshot}")
        return 0

    return report(device)


if __name__ == "__main__":
    sys.exit(main())

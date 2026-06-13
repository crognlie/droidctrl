# droidctrl

Browser-accessible remote control of an Android phone plugged into a Linux
host over USB. Streams the phone's screen to any browser tab in real time,
with tap, drag, scroll, and keyboard input forwarded back to the phone.

## How it works

```
[phone] --USB-ADB--> [droidctrl container]
                          |  adb shell screenrecord (H.264)
                          |  stream_server.py (aiohttp)
                          |      :6080  player page + WebSocket stream
                          |      :6081  swipe / key endpoints
                          |
                     [Caddy] --HTTPS--> [browser]
                                            WebCodecs VideoDecoder (hardware H.264)
```

The phone's display is captured with `adb shell screenrecord --output-format=h264`
and streamed as raw Annex-B H.264 over a WebSocket. The browser decodes it in
hardware using the [WebCodecs API](https://developer.mozilla.org/en-US/docs/Web/API/WebCodecs_API).
No Xvfb, x11vnc, websockify, or noVNC involved.

## Requirements

- Linux host with Docker + Docker Compose v2.
- Android phone with USB debugging enabled and already authorized (`adb devices`
  on the host once, accept the prompt on the phone).
- USB cable capable of data (not just charging).
- Chrome 94+ or Edge 94+ in the browser (WebCodecs API required).
- Optional: Caddy or another reverse proxy for HTTPS and a friendly URL.

## Setup

```bash
git clone <this-repo> droidctrl
cd droidctrl

cp .env.example .env
$EDITOR .env   # set ADB_KEY_DIR, SCREEN_WIDTH, SCREEN_HEIGHT at minimum

# Confirm host can see phone over USB:
adb devices    # should list your device as "device"

docker compose up -d
```

Open `http://your-host:6080/` (or your Caddy URL) in Chrome.

## Behind Caddy

A full working Caddyfile block is in [`examples/Caddyfile.example`](examples/Caddyfile.example).
Skeleton:

```caddyfile
yourhost.example.com {
    tls /path/to/cert.pem /path/to/key.pem

    @droidctrl_bare path /droidctrl
    redir @droidctrl_bare /droidctrl/

    # Swipe endpoint served on the sidecar port
    handle /droidctrl/swipe {
        rewrite * /swipe
        reverse_proxy droidctrl:6081
    }

    # Everything else: player page, WebSocket stream, all other endpoints
    handle_path /droidctrl/* {
        reverse_proxy droidctrl:6080
    }
}
```

Both containers must share a Docker network so `droidctrl:6080/6081` resolves.
See [`compose.override.yml`](compose.override.yml) for the network configuration
and [`compose.yml`](compose.yml) for the `default_bridge` external network reference.

## Configuration (`.env`)

| Variable | Meaning | Default |
|---|---|---|
| `TZ` | Container timezone | `UTC` |
| `ADB_KEY_DIR` | Host path containing `adbkey` + `adbkey.pub` | `$HOME/.android` |
| `SCREEN_WIDTH` | Phone screen width in pixels (`adb shell wm size`) | `1080` |
| `SCREEN_HEIGHT` | Phone screen height in pixels | `2400` |
| `PORT` | Host-side port to expose the player on | `6080` |
| `USB_RESET_VID` | USB vendor ID for wedge recovery (hex, no `0x`). `18d1` = Google Pixel | `18d1` |
| `REVERSE_TETHER` | Route phone internet through the container via gnirehtet | `false` |
| `BIT_RATE` | Screenrecord bitrate ceiling (e.g. `4M`, `8M`, `16M`) | `8M` |

The player UI also has a live **max bitrate** dropdown — changes apply immediately
and survive container restarts (saved in `./data/stream_settings.json`).

## Features

- **Native H.264 decode** — hardware-accelerated in the browser via WebCodecs.
  No server-side decode or re-encode; the phone's hardware encoder does the work.
- **Live stats** — fps and bandwidth in the status bar with 8-second rolling average,
  plus native → display resolution and codec string.
- **Mouse input** — click to tap, click-drag to swipe (duration matches your drag
  speed, so fast flicks register as flings on the phone), scroll wheel to scroll.
- **Keyboard input** — printable ASCII characters forwarded via `adb shell input text`.
- **Navigation bar** — Home (○) and Recent apps (□) buttons in a sidebar to the
  right of the screen. Automators can add custom buttons and toggles; see
  [Sidebar API](#sidebar-api--automator-hook).
- **Stays awake on USB** — `svc power stayon usb` is set at startup.
- **Auto-reconnect** — if the phone disconnects, the container waits and retries
  with USB wedge recovery (`USBDEVFS_RESET` + ADB server restart).
- **Reverse tethering** (opt-in) — routes phone internet through the container
  via [gnirehtet](https://github.com/Genymobile/gnirehtet).

## Sidebar API / Automator hook

The player page has a sidebar with Android navigation buttons. Automator
containers running alongside droidctrl can add their own buttons and
stateful toggles to this sidebar via HTTP.

See **[`docs/sidebar-api.md`](docs/sidebar-api.md)** for the full API reference.

Quick example — register a toggle from Python:

```python
import requests

BASE = "http://droidctrl:6080"

requests.get(f"{BASE}/toggle/register", params={
    "id":       "gem_mode",
    "tooltip":  "Gem collection on/off",
    "state":    "true",
    "callback": "http://my-automator:8080/on-toggle",
})
```

The toggle appears in the browser immediately. Clicking it POSTs
`{"id": "gem_mode", "state": false}` to the callback URL.

## Automation (optional)

Sidecar containers can poll the phone's screen and fire taps — useful for
recurring in-app actions (e.g., clicking "claim" when a reward shows up).

[`droidctrl-automator`](https://github.com/crognlie/droidctrl-automator) is the
reference implementation (OCR-based autoclicker for The Tower).

### Single automator (profile shortcut)

Clone into `./automator/` and start with the profile:

```bash
git clone https://github.com/crognlie/droidctrl-automator automator
docker compose --profile automator up -d
```

### Multiple automators

Each is its own compose project. Clone to a sibling directory, set
`ADB_SERVER_SOCKET=tcp:droidctrl:5037` in its environment, and
`docker compose up`. All join `default_bridge` and share the ADB server.

## Reverse tethering

Set `REVERSE_TETHER=true` in `.env` and restart to route the phone's internet
through the USB connection.

On first run the phone shows a VPN permission dialog — tap **OK**. Subsequent
connects are silent. Disable by setting `REVERSE_TETHER=false` and restarting.

Verify: `docker exec droidctrl adb shell ip route show` — look for `default via 10.0.2.2 dev tun0`.

## Troubleshooting

- **"connected — waiting for stream…" never clears**: confirm Chrome 94+ is
  being used (WebCodecs not supported in older browsers or Firefox).
- **ADB reconnect loop in logs**: the container automatically issues
  `USBDEVFS_RESET` and restarts ADB on each retry. If it stays stuck, replug
  the cable; Android sometimes reverts to charging-only mode after a host reboot.
- **Phone not detected after host reboot**: the container recovers automatically
  once the phone re-enumerates. Replug the cable if it stays offline.
- **Scroll wheel not working**: the scroll wheel sends `adb shell input swipe`
  via port 6081. Confirm the Caddy `handle /droidctrl/swipe` block is present
  and pointing at `droidctrl:6081`.

## Security

The container runs `privileged: true` with `/dev/bus/usb` passthrough — required
for ADB over USB. Treat the host as if anything on it can reach the phone. Do not
expose port 6080 publicly without a reverse proxy and authentication in front of it.

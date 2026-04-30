# droidctrl

Browser-accessible remote control of an Android phone plugged into a Linux
host over USB. Stream the phone's screen to any browser, click and type with
your mouse and keyboard, sync clipboards, and scroll with the mouse wheel
(even in apps that only handle touch gestures, like Unity games).

Built on top of [scrcpy](https://github.com/Genymobile/scrcpy), wrapped in a
small Docker stack that runs scrcpy into an `Xvfb` virtual display, exposes it
via `x11vnc` + `noVNC`, and layers a tiny Flask service for clipboard + wheel →
touch-swipe conversion.

## What it's for

- Using a phone from another room / another machine (home server setup).
- Remote control of a phone you can't physically reach (travel, lab bench).
- Scripted automation of phone-side tasks (see optional `automator` profile).

## Architecture

```
[browser] <--https--> [caddy (optional)] <--http/ws--> [droidctrl]
                                                              ├─ Xvfb :1
                                                              ├─ x11vnc  :5900
                                                              ├─ noVNC   :6080
                                                              ├─ Flask   :6081  (/clipboard, /swipe)
                                                              └─ scrcpy --> ADB --> [USB] --> [phone]
```

The browser talks noVNC (WebSocket) for the pixel stream and HTTP for
clipboard/scroll — the clipboard/scroll JS lives in the Caddy-served iframe
wrapper.

## Requirements

- Linux host with Docker + Docker Compose v2.
- An Android phone with USB debugging enabled and already authorized for the
  host's ADB key (run `adb devices` on the host once and accept the prompt
  on the phone).
- USB cable capable of data (not just charging).
- Optional: a reverse proxy (Caddy example in repo) if you want HTTPS / a
  friendly URL instead of `host:6080`.

## Setup

```bash
git clone <this-repo> droidctrl
cd droidctrl

cp .env.example .env
$EDITOR .env       # fill in NOVNC_HOST, ADB_KEY_DIR, SCREEN_WIDTH, SCREEN_HEIGHT

# Plug phone in, confirm host can see it:
adb devices        # should list your device as "device"

docker compose up -d
```

Open the URL printed by `docker compose logs scrcpy | grep Ready`, e.g.
`http://your-host:6080/vnc.html?autoconnect=true&resize=scale`.

### Behind Caddy — HTTPS is required

**Chrome refuses `navigator.clipboard.writeText()` in non-secure contexts.**
Over plain HTTP at a non-localhost URL, the phone → host-clipboard sync
silently fails. You must terminate TLS somewhere in front of droidctrl.

The wrapper page's HTML + JS lives in this repo at
[`web/index.html`](web/index.html) — bind-mount that directory into your
Caddy container (`- /path/to/droidctrl/web:/srv/droidctrl:ro`) and Caddy
serves it as a static file. A full working Caddyfile block is in
[`examples/Caddyfile.example`](examples/Caddyfile.example). Skeleton:

```caddyfile
yourhost.example.com {
    tls /path/to/cert.pem /path/to/key.pem

    # Normalize to trailing slash
    @droidctrl_bare path /droidctrl
    redir @droidctrl_bare /droidctrl/

    # Wrapper page (static file from the droidctrl repo's web/ dir)
    @droidctrl_index path /droidctrl/
    handle @droidctrl_index {
        root * /srv/droidctrl
        rewrite * /index.html
        file_server
    }

    # Flask endpoints on port 6081
    handle /droidctrl/clipboard { rewrite * /clipboard; reverse_proxy droidctrl:6081 }
    handle /droidctrl/swipe     { rewrite * /swipe;     reverse_proxy droidctrl:6081 }

    # noVNC static assets + WebSocket on port 6080
    handle_path /droidctrl/* { reverse_proxy droidctrl:6080 }
}
```

Why the wrapper JS lives at the top level (not inside the iframe):

- `navigator.clipboard.writeText()` needs a secure-context *user gesture* —
  so the listener that calls it must run on the page that has focus, which
  is the top document.
- The JS reaches into the iframe's DOM (same origin) to populate noVNC's
  clipboard textarea and attach wheel listeners on its canvas.

Both containers (Caddy and droidctrl) must share a Docker network so
`droidctrl:6080` and `droidctrl:6081` resolve. If you already run Caddy on an
external network, add `compose.override.yml` in droidctrl's directory
(see [Customization](#customization)).

URL prefix `/droidctrl/` is arbitrary — pick anything, but keep it consistent
through all four handlers *and* the absolute paths in the inline JS.

## Configuration (`.env`)

| Variable | Meaning | Default |
|----------|---------|---------|
| `NOVNC_HOST` | Hostname shown in the startup URL | `localhost` |
| `TZ` | Container timezone | `UTC` |
| `ADB_KEY_DIR` | Host path containing `adbkey` + `adbkey.pub` | `$HOME/.android` |
| `SCREEN_WIDTH` | Phone screen width in pixels | `1080` |
| `SCREEN_HEIGHT` | Phone screen height in pixels | `2400` |
| `NOVNC_PORT` | Host-side port to expose noVNC on | `6080` |
| `REVERSE_TETHER` | Route phone internet through the container via USB (see below) | `false` |
| `USB_RESET_VID` | USB vendor ID to reset on wedge (hex, no `0x`) | `18d1` (Google) |

Find your phone's resolution with: `adb shell wm size`.

## Features

- **Video stream**: 60fps, 8Mbps H.264, scales to browser window.
- **Phone screen stays off** while you control it (via scrcpy's
  `--turn-screen-off`), so you don't leak battery or visibly announce that the
  phone is being used.
- **Stays awake on USB**: `svc power stayon usb` is set at startup so sleep
  can't drop the connection.
- **Clipboard sync**: copy on the phone → poll picks up the Xvfb clipboard →
  posts to the noVNC panel and (on your next click in the window) writes it
  to your browser's clipboard.
- **Mouse wheel → touch swipe**: wheel events are intercepted in JS and
  converted to ADB touch swipes starting at your cursor's phone coordinate.
  Works in apps that ignore Android's `ACTION_SCROLL` (Unity games, etc.)
  and in partial-height scrollable panels.
- **Auto-reconnect**: when the phone disconnects, scrcpy exits cleanly and
  the container waits indefinitely for it to come back. Each retry attempt
  issues a `USBDEVFS_RESET` on the phone's USB device to clear wedged bulk
  endpoints before restarting the ADB server.
- **Reverse tethering** (opt-in): set `REVERSE_TETHER=true` in `.env` to
  route the phone's internet traffic through the container via USB using
  [gnirehtet](https://github.com/Genymobile/gnirehtet). No WiFi needed on
  the phone; the relay runs inside the container and reinitialises on every
  reconnect. See [Reverse tethering](#reverse-tethering) below.

## Automation (optional)

Sidecar containers can poll the phone's screen and fire taps — useful for
recurring in-app actions (e.g., clicking "claim" when a reward shows up).

[`droidctrl-automator`](https://github.com/crognlie/droidctrl-automator) is the reference implementation;
it's an OCR-based autoclicker for The Tower.

### Single automator (convenient shortcut)

Clone into `./automator/` and start with the profile:

```bash
git clone https://github.com/crognlie/droidctrl-automator automator
docker compose --profile automator up -d
```

### Multiple automators (or a cleaner separation)

Each automator is its own compose project — just clone to a sibling
directory and `docker compose up`. Each one joins
`droidctrl_default` and reaches ADB at `tcp:droidctrl:5037`.

```bash
# Layout:
~/code/
├── droidctrl/      # scrcpy + noVNC, this repo
├── tower-automator/      # claim gems + retry in The Tower
└── other-automator/      # something else — same pattern

cd ~/code/tower-automator && docker compose up -d
cd ~/code/other-automator && docker compose up -d
```

They each have their own `.env` (own `POLL_INTERVAL`, `TARGETS`, etc.) and
their own lifecycle. Note that multiple automators tapping the same phone
at overlapping times can collide — stagger `POLL_INTERVAL` values if you
hit this.

## Updates and restarts

Automators can be rebuilt and restarted independently — scrcpy stays up.

```bash
# Subfolder/profile mode: rebuild + restart only automator
docker compose up -d --build automator

# Or just restart the container without a rebuild:
docker compose restart automator

# Standalone mode: from inside the automator repo's directory
cd ~/code/tower-automator
docker compose up -d --build        # or: docker compose restart
```

Naming the service explicitly means Compose touches only that container.
`depends_on: scrcpy` just checks the scrcpy container is alive; it won't
recreate it. Python-only edits rebuild in sub-seconds thanks to Docker
layer caching.

## Customization

- Want a different network (e.g., to share one with a reverse-proxy
  container)? Create `compose.override.yml`:
  ```yaml
  networks:
    default:
      name: your_network
      external: true
  ```
  `compose.override.yml` is gitignored.
- Want to pin a different scrcpy version? Edit `ARG SCRCPY_VERSION=` in the
  `Dockerfile`, or override at build: `docker compose build
  --build-arg SCRCPY_VERSION=v3.4.0`.

## Reverse tethering

Set `REVERSE_TETHER=true` in `.env` and restart to give the phone internet
through the USB connection instead of (or in addition to) its own WiFi.

```bash
# .env
REVERSE_TETHER=true
```

How it works: [gnirehtet](https://github.com/Genymobile/gnirehtet) installs a
small APK on the phone and starts a relay inside the container. All phone
traffic tunnels over ADB to the relay, which forwards it through the
container's network interface. The phone occupies its system VPN slot while
tethering is active.

**First run**: the phone will show a VPN permission dialog — tap **OK**. After
that, authorization is remembered and future connects are silent.

**To check which path the phone is using:**

```bash
docker exec droidctrl adb shell ip route show
```

- Reverse tether active: `default via 10.0.2.2 dev tun0`
- WiFi only: route via `wlan0`, no `tun0` default

**Conflict**: gnirehtet uses Android's built-in VPN slot. It will be
incompatible with other always-on VPN apps running on the phone at the same
time. Set `REVERSE_TETHER=false` and restart to disable.

## Troubleshooting

- **noVNC stuck at "Connecting"**: check `docker logs droidctrl` for
  `EGL not initialized` or a stale `/tmp/.X1-lock`. The container recreates
  should handle this, but if you hit it, `docker compose restart`.
- **ADB in the container loops reconnecting** ("read failed: Success",
  "write terminated: Connection timed out"): the container automatically
  issues a `USBDEVFS_RESET` and restarts the ADB server on each retry, so
  most wedge conditions clear themselves when you replug. If it stays stuck,
  reset the USB port manually via sysfs:
  `echo 0 > /sys/bus/usb/devices/<bus>-<port>/authorized`, then `echo 1`.
- **Scroll wheel does nothing in-app**: confirm scroll works in Android's
  launcher or Settings first — if it works there but not in your app, the
  app is swallowing swipe at that screen region; try scrolling while
  hovering a different part of the scrollable panel.
- **Clipboard isn't writing to the host's browser clipboard**: Chrome needs
  a user gesture. Copy on phone → click once inside the VNC window → next
  poll will push to the host clipboard.

## Security notes

- The container runs `privileged: true` with `/dev/bus/usb` passthrough —
  needed for ADB over USB. Treat the host as if anything on it can reach
  the phone.
- `x11vnc` runs with `-nopw` (no VNC password) on the internal network
  only — the container binds VNC to localhost inside the container, so
  external access comes via noVNC HTTP, not raw VNC. Still: don't expose
  port 6080 publicly without a reverse proxy + auth in front of it.

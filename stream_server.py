#!/usr/bin/env python3
"""
Streams raw H.264 from Android screenrecord over WebSocket.
Uses `adb shell exec screenrecord` so that killing the container-side
process causes adbd to SIGHUP the phone-side process group, cleanly
terminating screenrecord with no zombie processes.
"""
import asyncio
import json
import os
import shlex
from aiohttp import web, ClientSession, ClientTimeout

SETTINGS_FILE = "/data/stream_settings.json"
BIT_RATE = os.environ.get("BIT_RATE", "8M")


def _load_settings():
    global BIT_RATE
    try:
        with open(SETTINGS_FILE) as f:
            s = json.load(f)
        if "bitrate" in s:
            BIT_RATE = s["bitrate"]
            print(f"[*] loaded settings: bitrate={BIT_RATE}", flush=True)
            return
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    # No settings file yet — write defaults now so it exists for next time
    print(f"[*] initialising settings: bitrate={BIT_RATE}", flush=True)
    _save_settings()

def _save_settings():
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump({"bitrate": BIT_RATE}, f)
    except OSError as e:
        print(f"[!] couldn't save settings: {e}", flush=True)

_load_settings()
SCREEN_W = os.environ.get("SCREEN_W", "0")
SCREEN_H = os.environ.get("SCREEN_H", "0")

clients: set[web.WebSocketResponse] = set()
stream_proc: asyncio.subprocess.Process | None = None
stream_gen = 0

# One-shot button registry — automator registers; browser loads and clicks.
# Each entry: {tooltip, adb_cmd, icon}
sidebar_buttons: dict[str, dict] = {}

# Toggle registry — automator registers entries; browser polls and clicks.
# Each entry: {tooltip, state, callback, icon}
toggles: dict[str, dict] = {}


async def _broadcast_toggles():
    """Push current toggle states to all WS clients."""
    msg = json.dumps({"toggles": {tid: t["state"] for tid, t in toggles.items()}})
    for ws in list(clients):
        try:
            await ws.send_str(msg)
        except Exception:
            pass


async def _fire_callback(url: str, tid: str, state: bool):
    try:
        async with ClientSession() as s:
            await s.post(url, json={"id": tid, "state": state},
                         timeout=ClientTimeout(total=5))
    except Exception as e:
        print(f"[!] toggle callback {url}: {e}", flush=True)


def _screenrecord_args():
    """Build the screenrecord argument list (runs on the phone via adb shell)."""
    bitrate = int(BIT_RATE.rstrip("Mm")) * 1_000_000
    size = f"--size={SCREEN_W}x{SCREEN_H}" if SCREEN_W != "0" and SCREEN_H != "0" else ""
    return " ".join(filter(None, [
        "screenrecord",
        "--output-format=h264",
        f"--bit-rate={bitrate}",
        size,
        "-",   # write to stdout (fd 1) directly, bypasses fopen
    ]))


async def broadcaster():
    """Fan out H.264 to all WebSocket clients. One screenrecord per session."""
    global stream_proc, stream_gen
    while True:
        try:
            while not clients:
                await asyncio.sleep(0.2)

            my_gen = stream_gen
            # `exec` replaces the shell so screenrecord IS the process that
            # gets SIGHUP when the adb shell connection drops — no zombies.
            shell_cmd = f"exec {_screenrecord_args()}"
            print(f"[*] starting gen={my_gen}", flush=True)

            stream_proc = await asyncio.create_subprocess_exec(
                "adb", "shell", shell_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            stream_proc.stdout.read(65536), timeout=10.0
                        )
                    except asyncio.TimeoutError:
                        print("[!] screenrecord stalled — restarting", flush=True)
                        break
                    if not chunk:
                        break
                    if stream_gen != my_gen:
                        break
                    dead = set()
                    for ws in list(clients):
                        try:
                            await ws.send_bytes(chunk)
                        except Exception:
                            dead.add(ws)
                    clients.difference_update(dead)
            finally:
                try:
                    stream_proc.terminate()   # SIGTERM → adbd gets a clean close signal
                except ProcessLookupError:
                    pass
                await asyncio.sleep(0.8)      # let phone-side SIGHUP propagate and screenrecord exit
                try:
                    stream_proc.kill()        # SIGKILL in case SIGTERM wasn't enough
                except ProcessLookupError:
                    pass
                try:
                    err = await asyncio.wait_for(stream_proc.stderr.read(), timeout=2.0)
                    if err:
                        print(f"[screenrecord] {err.decode(errors='replace').strip()}", flush=True)
                except asyncio.TimeoutError:
                    pass
                rc = await stream_proc.wait()
                print(f"[*] screenrecord exited rc={rc} gen={my_gen}", flush=True)
                stream_proc = None

            if clients and stream_gen == my_gen:
                await asyncio.sleep(0.5)

        except Exception as e:
            print(f"[!] broadcaster: {e}", flush=True)
            await asyncio.sleep(2)


async def ws_handler(request):
    global stream_gen
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    if not clients:
        stream_gen += 1   # new session — broadcaster will restart for fresh SPS/PPS
    clients.add(ws)
    print(f"[*] client connected ({len(clients)} total, gen={stream_gen})", flush=True)
    try:
        async for _ in ws:
            pass
    finally:
        clients.discard(ws)
        print(f"[*] client disconnected ({len(clients)} remaining)", flush=True)
    return ws


# ---------------------------------------------------------------------------
# Input handlers
# ---------------------------------------------------------------------------

PHONE_W = int(SCREEN_W) if SCREEN_W != "0" else 1080
PHONE_H = int(SCREEN_H) if SCREEN_H != "0" else 2400


async def _adb_input(cmd: str):
    """Fire an adb shell input command, discarding output. Times out in 3s."""
    proc = await asyncio.create_subprocess_exec(
        "adb", "shell", cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=3.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()


async def bitrate_handler(request):
    global BIT_RATE, stream_gen
    val = request.rel_url.query.get("value", "").upper().strip()
    if val and val.rstrip("M").isdigit():
        BIT_RATE = val if val.endswith("M") else val + "M"
        stream_gen += 1   # triggers broadcaster to restart with new bitrate
        print(f"[*] bitrate → {BIT_RATE}", flush=True)
        _save_settings()
        # Push updated settings to all connected sessions
        msg = json.dumps({"settings": {"bitrate": BIT_RATE}})
        for ws in list(clients):
            try:
                await ws.send_str(msg)
            except Exception:
                pass
    return web.Response(status=204)


async def tap_handler(request):
    q = request.rel_url.query
    x = max(0, min(int(q.get("x", 0)), PHONE_W))
    y = max(0, min(int(q.get("y", 0)), PHONE_H))
    asyncio.create_task(_adb_input(f"input tap {x} {y}"))
    return web.Response(status=204)


async def swipe_handler(request):
    q   = request.rel_url.query
    cx  = max(0, min(int(q.get("x",  PHONE_W // 2)), PHONE_W))
    cy  = max(0, min(int(q.get("y",  PHONE_H // 2)), PHONE_H))
    amt = max(100, min(int(q.get("amount", 400)), 1600))
    dur = int(q.get("duration", "80"))
    if q.get("dir", "up") == "up":
        x1, y1, x2, y2 = cx, cy, cx, max(0, cy - amt)
    else:
        x1, y1, x2, y2 = cx, cy, cx, min(PHONE_H, cy + amt)
    asyncio.create_task(_adb_input(f"input swipe {x1} {y1} {x2} {y2} {dur}"))
    return web.Response(status=204)


async def sidebar_button_register(request):
    """Automator registers a one-shot button: id, tooltip, adb_cmd, icon."""
    q = request.rel_url.query
    bid = q.get("id", "").strip()
    adb_cmd = q.get("adb_cmd", "").strip()
    if not bid or not adb_cmd:
        return web.Response(status=400)
    sidebar_buttons[bid] = {
        "tooltip": q.get("tooltip", bid),
        "adb_cmd": adb_cmd,
        "icon":    q.get("icon", ""),
    }
    print(f"[*] button registered: {bid}", flush=True)
    msg = json.dumps({"buttons": [
        {"id": k, "tooltip": v["tooltip"], "icon": v["icon"]}
        for k, v in sidebar_buttons.items()
    ]})
    for ws in list(clients):
        try: await ws.send_str(msg)
        except Exception: pass
    return web.Response(status=204)


async def sidebar_button_click(request):
    """Browser clicked a one-shot button — run its adb command."""
    bid = request.rel_url.query.get("id", "").strip()
    if bid not in sidebar_buttons:
        return web.Response(status=404)
    asyncio.create_task(_adb_input(sidebar_buttons[bid]["adb_cmd"]))
    return web.Response(status=204)


async def sidebar_button_list(request):
    """Browser fetches button list on load."""
    return web.json_response([
        {"id": bid, "tooltip": b["tooltip"], "icon": b["icon"]}
        for bid, b in sidebar_buttons.items()
    ])


async def toggle_register(request):
    """Automator registers a toggle: id, tooltip, state, callback, icon (SVG string)."""
    q = request.rel_url.query
    tid = q.get("id", "").strip()
    if not tid:
        return web.Response(status=400)
    toggles[tid] = {
        "tooltip":  q.get("tooltip",  tid),
        "state":    q.get("state",    "false").lower() in ("true", "1"),
        "callback": q.get("callback", ""),
        "icon":     q.get("icon",     ""),
    }
    print(f"[*] toggle registered: {tid} state={toggles[tid]['state']}", flush=True)
    await _broadcast_toggles()
    return web.Response(status=204)


async def toggle_set(request):
    """Automator updates a toggle's state without a browser click."""
    q = request.rel_url.query
    tid = q.get("id", "").strip()
    if tid not in toggles:
        return web.Response(status=404)
    toggles[tid]["state"] = q.get("state", "false").lower() in ("true", "1")
    await _broadcast_toggles()
    return web.Response(status=204)


async def toggle_click(request):
    """Browser clicked a toggle — flip state, push update, fire callback."""
    tid = request.rel_url.query.get("id", "").strip()
    if tid not in toggles:
        return web.Response(status=404)
    toggles[tid]["state"] = not toggles[tid]["state"]
    new_state = toggles[tid]["state"]
    print(f"[*] toggle clicked: {tid} → {new_state}", flush=True)
    await _broadcast_toggles()
    if toggles[tid]["callback"]:
        asyncio.create_task(_fire_callback(toggles[tid]["callback"], tid, new_state))
    return web.json_response({"id": tid, "state": new_state})


async def toggle_states(request):
    """Browser polls all toggle states."""
    return web.json_response({tid: t["state"] for tid, t in toggles.items()})


async def toggle_list(request):
    """Browser fetches full toggle list (id, tooltip, state, icon) on load."""
    return web.json_response([
        {"id": tid, "tooltip": t["tooltip"], "state": t["state"], "icon": t["icon"]}
        for tid, t in toggles.items()
    ])


async def keyevent_handler(request):
    code = request.rel_url.query.get('code', '').strip()
    if code.isdigit():
        asyncio.create_task(_adb_input(f"input keyevent {code}"))
    return web.Response(status=204)


async def key_handler(request):
    text = request.rel_url.query.get('text', '')
    if text:
        # shlex.quote wraps in single quotes so Android shell treats it as a literal
        asyncio.create_task(_adb_input(f"input text {shlex.quote(text)}"))
    return web.Response(status=204)


async def drag_handler(request):
    q   = request.rel_url.query
    x1  = max(0, min(int(q.get("x1", PHONE_W // 2)), PHONE_W))
    y1  = max(0, min(int(q.get("y1", PHONE_H // 2)), PHONE_H))
    x2  = max(0, min(int(q.get("x2", x1)),           PHONE_W))
    y2  = max(0, min(int(q.get("y2", y1)),            PHONE_H))
    dur = int(q.get("duration", "100"))
    asyncio.create_task(_adb_input(f"input swipe {x1} {y1} {x2} {y2} {dur}"))
    return web.Response(status=204)


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

async def index(request):
    return web.FileResponse("/webcodecs/index.html",
                            headers={"Cache-Control": "no-store"})


async def on_startup(app):
    asyncio.create_task(broadcaster())


async def main():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/settings", lambda r: web.json_response({"bitrate": BIT_RATE}))
    app.router.add_get("/sidebar-button",        sidebar_button_register)
    app.router.add_get("/sidebar-button/click",  sidebar_button_click)
    app.router.add_get("/sidebar-button/list",   sidebar_button_list)
    app.router.add_get("/toggle/register", toggle_register)
    app.router.add_get("/toggle/set",      toggle_set)
    app.router.add_get("/toggle/click",    toggle_click)
    app.router.add_get("/toggle/states",   toggle_states)
    app.router.add_get("/toggle/list",     toggle_list)
    app.router.add_get("/keyevent", keyevent_handler)
    app.router.add_get("/key", key_handler)
    app.router.add_get("/bitrate", bitrate_handler)
    app.router.add_get("/tap", tap_handler)
    app.router.add_get("/swipe", swipe_handler)   # reached via Caddy :6081 route
    app.router.add_get("/drag", drag_handler)
    app.router.add_static("/", "/webcodecs")

    runner = web.AppRunner(app)
    await runner.setup()
    for port in (6080, 6081):
        await web.TCPSite(runner, "0.0.0.0", port).start()
        print(f"[*] listening on :{port}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())

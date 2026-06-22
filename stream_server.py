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
import re
import shlex
from aiohttp import web

SETTINGS_FILE = "/data/stream_settings.json"
BIT_RATE = os.environ.get("BIT_RATE", "8M")

SCREEN_W = os.environ.get("SCREEN_W", "0")
SCREEN_H = os.environ.get("SCREEN_H", "0")

RES_DIVISOR = 1     # 1=full, 2=half, 4=quarter native resolution
NATIVE_W    = 0     # detected from `adb shell wm size` at startup
NATIVE_H    = 0


def _parse_bps(val: str) -> int:
    """Parse a bitrate string to bits-per-second.
    Accepts: '8M' → 8_000_000, '512k' → 512_000, '6000000' → 6_000_000."""
    v = val.strip().upper()
    if v.endswith("M"):
        return int(float(v[:-1]) * 1_000_000)
    if v.endswith("K"):
        return int(float(v[:-1]) * 1_000)
    return int(float(v))

clients: set[web.WebSocketResponse] = set()
stream_proc: asyncio.subprocess.Process | None = None
stream_gen = 0


# Unified item registry — type is "button", "toggle", or "numeric".
# button/toggle: {type, desc, state (bool), order, preserve_state}
# numeric:       {type, desc, value (float), order, preserve_state}
items: dict[str, dict] = {}


def _item_list():
    return sorted([
        {"id": iid, "type": i["type"], "desc": i["desc"],
         "state": i["state"], "order": i.get("order", 0)}
        for iid, i in items.items()
    ], key=lambda x: (x["order"], x["id"]))


def _load_settings():
    global BIT_RATE, RES_DIVISOR, items
    try:
        with open(SETTINGS_FILE) as f:
            s = json.load(f)
        if "bitrate"     in s: BIT_RATE    = s["bitrate"]
        if "res_divisor" in s: RES_DIVISOR = int(s["res_divisor"])
        if "items"       in s: items        = s["items"]
        res = {1: "full", 2: "half", 4: "quarter"}.get(RES_DIVISOR, "full")
        print(f"[*] loaded settings: bitrate={BIT_RATE} res={res} "
              f"{len(items)} item(s)", flush=True)
        return
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    print(f"[*] initialising settings: bitrate={BIT_RATE}", flush=True)
    _save_settings()

def _save_settings():
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump({"bitrate": BIT_RATE, "res_divisor": RES_DIVISOR,
                       "items": items}, f)
    except OSError as e:
        print(f"[!] couldn't save settings: {e}", flush=True)

_load_settings()


_reload_until = 0.0  # event-loop time; new connections get a reload signal before this


async def _broadcast_reload():
    msg = json.dumps({"reload": True})
    for ws in list(clients):
        try:
            await ws.send_str(msg)
        except Exception:
            pass


async def _broadcast_item_states():
    """Lightweight push: current state for every item."""
    msg = json.dumps({"item_states": {iid: i["state"] for iid, i in items.items()}})
    for ws in list(clients):
        try:
            await ws.send_str(msg)
        except Exception:
            pass


async def _broadcast_item_list():
    """Full metadata push on structural changes (register/deregister)."""
    msg = json.dumps({"item_list": _item_list()})
    for ws in list(clients):
        try:
            await ws.send_str(msg)
        except Exception:
            pass



def _screenrecord_args():
    """Build the screenrecord argument list (runs on the phone via adb shell)."""
    bitrate = _parse_bps(BIT_RATE)
    # Resolution: explicit env override → half-res → native (no --size flag)
    if SCREEN_W != "0" and SCREEN_H != "0":
        size = f"--size={SCREEN_W}x{SCREEN_H}"
    elif RES_DIVISOR > 1 and NATIVE_W and NATIVE_H:
        # Round to nearest even number (H.264 requirement)
        w = (NATIVE_W // RES_DIVISOR) & ~1
        h = (NATIVE_H // RES_DIVISOR) & ~1
        size = f"--size={w}x{h}"
    else:
        size = ""
    return " ".join(filter(None, [
        "screenrecord",
        "--output-format=h264",
        f"--bit-rate={bitrate}",
        size,
        "-",
    ]))


_GUARDIAN_APPLY = (
    "locksettings set-disabled true; "
    "svc power stayon usb; "
    "settings put system screen_brightness_mode 0; "
    "settings put system screen_brightness 0"
)
_GUARDIAN_RESTORE = (
    "locksettings set-disabled false; "
    "svc power stayon false; "
    "settings put system screen_brightness_mode 1; "
    "settings put system screen_brightness 128"
)


async def brightness_guardian():
    """Long-lived adb shell that applies phone display settings while connected
    and restores them automatically on USB disconnect or container stop."""
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                "adb", "shell",
                f"trap '{_GUARDIAN_RESTORE}' EXIT HUP TERM INT; {_GUARDIAN_APPLY}; cat",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            print("[*] phone settings guardian disconnected, restarting...", flush=True)
        except Exception as e:
            print(f"[!] phone settings guardian: {e}", flush=True)
        await asyncio.sleep(3)


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

            # Nudge the H.264 encoder to emit an initial IDR frame even when
            # the screen is fully static. KEYCODE_WAKEUP is a no-op on an
            # already-on display but causes the display pipeline to flush.
            async def _nudge():
                await asyncio.sleep(0.3)
                await _adb_input("input keyevent 224")
            asyncio.create_task(_nudge())

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


async def reload_handler(request):
    """Tell all connected browser sessions to reload the page."""
    await _broadcast_reload()
    print("[*] reload broadcast sent", flush=True)
    return web.Response(status=204)


async def ws_handler(request):
    global stream_gen
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    was_empty = not clients
    clients.add(ws)
    if was_empty or stream_proc is None:
        # No screenrecord running — bump gen so the broadcaster starts one.
        stream_gen += 1
    else:
        # Screenrecord already running; nudge the encoder for a fresh IDR so
        # the joining client doesn't wait through the next natural keyframe.
        asyncio.create_task(_adb_input("input keyevent 224"))
    print(f"[*] client connected ({len(clients)} total, gen={stream_gen})", flush=True)
    # During the startup reload window, tell this client to reload so it picks
    # up any code changes deployed with the container.
    if asyncio.get_event_loop().time() < _reload_until:
        try:
            await ws.send_str(json.dumps({"reload": True}))
        except Exception:
            pass
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
    val = request.rel_url.query.get("value", "").strip()
    try:
        bps = _parse_bps(val)
        if bps <= 0:
            raise ValueError
    except (ValueError, ZeroDivisionError):
        return web.Response(status=400)
    BIT_RATE = str(bps)
    stream_gen += 1
    print(f"[*] bitrate → {bps:,} bps", flush=True)
    _save_settings()
    msg = json.dumps({"settings": {"bitrate": BIT_RATE, "res_divisor": RES_DIVISOR,
                                   "native_w": NATIVE_W, "native_h": NATIVE_H}})
    for ws in list(clients):
        try: await ws.send_str(msg)
        except Exception: pass
    return web.Response(status=204)


_RES_NAMES = {"full": 1, "half": 2, "quarter": 4}

async def resolution_handler(request):
    global RES_DIVISOR, stream_gen
    val = request.rel_url.query.get("value", "").lower()
    if val not in _RES_NAMES:
        return web.Response(status=400)
    RES_DIVISOR = _RES_NAMES[val]
    stream_gen += 1
    print(f"[*] resolution → {val}", flush=True)
    _save_settings()
    msg = json.dumps({"settings": {"bitrate": BIT_RATE, "res_divisor": RES_DIVISOR,
                                   "native_w": NATIVE_W, "native_h": NATIVE_H}})
    for ws in list(clients):
        try: await ws.send_str(msg)
        except Exception: pass
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


async def item_register(request):
    """Register or update an item. type is 'button', 'toggle', or 'numeric'."""
    q = request.rel_url.query
    iid = q.get("id", "").strip()
    itype = q.get("type", "toggle").lower()
    if not iid or itype not in ("button", "toggle", "numeric"):
        return web.Response(status=400)
    try:
        order = int(q.get("order", items[iid].get("order", 0) if iid in items else 0))
    except ValueError:
        order = 0
    preserve = q.get("preserve_state", "false").lower() in ("true", "1")
    desc = q.get("desc", iid)
    raw = q.get("state", "0" if itype == "numeric" else "false")
    existing = items[iid]["state"] if (preserve and iid in items) else None
    if itype == "numeric":
        try:
            state = existing if existing is not None else float(raw)
        except ValueError:
            state = 0.0
    else:
        state = existing if existing is not None else raw.lower() in ("true", "1")
    items[iid] = {"type": itype, "desc": desc, "state": state, "order": order}
    print(f"[*] item registered: {iid} type={itype}", flush=True)
    _save_settings()
    await _broadcast_item_list()
    return web.Response(status=204)


async def item_set(request):
    """Set an item's state. For numeric, state is a float; for toggle/button, bool."""
    q = request.rel_url.query
    iid = q.get("id", "").strip()
    if iid not in items:
        return web.Response(status=404)
    item = items[iid]
    raw = q.get("state", "")
    if item["type"] == "numeric":
        try:
            item["state"] = float(raw)
        except ValueError:
            return web.Response(status=400)
    else:
        item["state"] = raw.lower() in ("true", "1")
    _save_settings()
    await _broadcast_item_states()
    return web.Response(status=204)


async def item_click(request):
    """Browser clicked an item — toggle flips, button sets true."""
    iid = request.rel_url.query.get("id", "").strip()
    if iid not in items:
        return web.Response(status=404)
    item = items[iid]
    if item["type"] == "toggle":
        item["state"] = not item["state"]
    elif item["type"] == "button":
        item["state"] = True
    print(f"[*] item clicked: {iid} → {item['state']}", flush=True)
    _save_settings()
    await _broadcast_item_states()
    return web.json_response({"id": iid, "state": item["state"]})


async def item_deregister(request):
    iid = request.rel_url.query.get("id", "").strip()
    if iid not in items:
        return web.Response(status=404)
    del items[iid]
    print(f"[*] item deregistered: {iid}", flush=True)
    _save_settings()
    await _broadcast_item_list()
    return web.Response(status=204)


async def item_states(request):
    return web.json_response({iid: i["state"] for iid, i in items.items()})


async def item_list(request):
    return web.json_response(_item_list())


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
    global _reload_until, NATIVE_W, NATIVE_H
    _reload_until = asyncio.get_event_loop().time() + 30
    # Detect native phone resolution for half-res support.
    try:
        p = await asyncio.create_subprocess_exec(
            "adb", "shell", "wm", "size",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), timeout=5)
        m = re.search(r"(\d+)x(\d+)", out.decode())
        if m:
            NATIVE_W, NATIVE_H = int(m.group(1)), int(m.group(2))
            print(f"[*] native resolution: {NATIVE_W}x{NATIVE_H}", flush=True)
    except Exception:
        pass
    asyncio.create_task(brightness_guardian())
    asyncio.create_task(broadcaster())


async def main():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/reload",   reload_handler)
    app.router.add_get("/settings", lambda r: web.json_response({
        "bitrate": BIT_RATE, "res_divisor": RES_DIVISOR,
        "native_w": NATIVE_W, "native_h": NATIVE_H,
    }))
    app.router.add_get("/resolution", resolution_handler)
    app.router.add_get("/item/register",   item_register)
    app.router.add_get("/item/set",        item_set)
    app.router.add_get("/item/click",      item_click)
    app.router.add_get("/item/deregister", item_deregister)
    app.router.add_get("/item/states",     item_states)
    app.router.add_get("/item/list",       item_list)
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

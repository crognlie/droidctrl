# Sidebar API — adding buttons and toggles from an automator

The droidctrl player page has a sidebar to the right of the Android screen.
Automator containers can add **buttons** (one-shot actions) and **toggles**
(stateful on/off switches) to this sidebar via HTTP requests to the stream
server.

All endpoints are on the stream server at `http://droidctrl:6080` (reachable
from any container on the same Docker network). The browser polls and receives
WebSocket pushes, so changes appear immediately in all open browser sessions.

---

## Buttons (one-shot actions)

Buttons fire an ADB command once when clicked. They have no persistent state.

### Registering a button

```
GET http://droidctrl:6080/sidebar-button
    ?id=<id>
    &tooltip=<text shown on hover>
    &adb_cmd=<shell command to run on click>
    &icon=<url-encoded SVG string>   (optional)
```

| Parameter | Required | Description |
|-----------|----------|-------------|
| `id` | yes | Unique identifier for this button |
| `tooltip` | yes | Text shown on hover |
| `adb_cmd` | yes | Shell command passed to `adb shell`, e.g. `input keyevent 3` |
| `icon` | no | URL-encoded SVG string. If omitted, a default icon is used. |

**Python example:**

```python
import requests

requests.get("http://droidctrl:6080/sidebar-button", params={
    "id":      "home",
    "tooltip": "Go home",
    "adb_cmd": "input keyevent 3",
})
```

**Custom SVG icon:**

```python
from urllib.parse import quote

icon_svg = '''<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
  <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>
</svg>'''

requests.get("http://droidctrl:6080/sidebar-button", params={
    "id":      "home",
    "tooltip": "Go home",
    "adb_cmd": "input keyevent 3",
    "icon":    icon_svg,   # requests handles URL-encoding automatically
})
```

### Removing a button

Call the register endpoint again with the same `id` to replace it, or restart
the droidctrl container to clear all dynamically registered buttons (they are
not persisted to disk).

---

## Toggles (stateful on/off switches)

Toggles have a persistent on/off state visible in the browser. The automator
can read and set the state; the browser can also flip it by clicking. When
the browser clicks, the server can notify the automator via an HTTP callback.

### Registering a toggle

```
GET http://droidctrl:6080/toggle/register
    ?id=<id>
    &tooltip=<text shown on hover>
    &state=<true|false>              (initial state, default false)
    &callback=<url>                  (optional — called when browser clicks)
    &icon=<url-encoded SVG string>   (optional)
```

| Parameter | Required | Description |
|-----------|----------|-------------|
| `id` | yes | Unique identifier |
| `tooltip` | yes | Text shown on hover |
| `state` | no | Initial state: `true` or `false` (default `false`) |
| `callback` | no | URL that receives a POST when the browser toggles this button |
| `icon` | no | URL-encoded SVG string |

**Python example:**

```python
import requests

requests.get("http://droidctrl:6080/toggle/register", params={
    "id":       "gem_mode",
    "tooltip":  "Gem collection on/off",
    "state":    "true",
    "callback": "http://my-automator:8080/on-toggle",
})
```

The toggle appears in the browser immediately with a green glow when active.

### Reading current state

```
GET http://droidctrl:6080/toggle/states
```

Returns a JSON object mapping toggle IDs to their current boolean states:

```json
{"gem_mode": true, "retry_mode": false}
```

```python
resp = requests.get("http://droidctrl:6080/toggle/states")
states = resp.json()
if states.get("gem_mode"):
    # gem mode is on
```

### Setting state from the automator

Use this when the automator itself wants to change a toggle's state (e.g.,
it detected a condition that should turn the toggle off):

```
GET http://droidctrl:6080/toggle/set
    ?id=<id>
    &state=<true|false>
```

```python
# Turn gem_mode off
requests.get("http://droidctrl:6080/toggle/set", params={
    "id":    "gem_mode",
    "state": "false",
})
```

All connected browser sessions see the update within 2 seconds (polling)
or instantly (WebSocket push).

### Receiving browser click callbacks

When the browser clicks a toggle and a `callback` URL was registered, the
server sends an HTTP POST to that URL with a JSON body:

```json
{"id": "gem_mode", "state": false}
```

`state` is the **new** state after the click.

**Receiving the callback in Python (aiohttp example):**

```python
from aiohttp import web

async def on_toggle(request):
    data = await request.json()
    toggle_id = data["id"]
    new_state = data["state"]
    print(f"Toggle {toggle_id} is now {new_state}")
    # Update automator behavior accordingly
    return web.Response(status=204)

app = web.Application()
app.router.add_post("/on-toggle", on_toggle)
web.run_app(app, port=8080)
```

Register with `callback=http://my-automator:8080/on-toggle`.

**Receiving the callback in Python (Flask example):**

```python
from flask import Flask, request

app = Flask(__name__)

@app.route("/on-toggle", methods=["POST"])
def on_toggle():
    data = request.get_json()
    toggle_id = data["id"]
    new_state = data["state"]
    print(f"Toggle {toggle_id} is now {new_state}")
    return "", 204

app.run(host="0.0.0.0", port=8080)
```

The callback has a 5-second timeout. If it fails, the toggle state in the
browser is already updated; only the automator notification is lost.

---

## Complete endpoint reference

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/sidebar-button` | Register a one-shot button |
| `GET` | `/toggle/register` | Register a toggle |
| `GET` | `/toggle/set` | Set a toggle's state (automator-driven) |
| `GET` | `/toggle/click` | Simulate a browser click on a toggle |
| `GET` | `/toggle/states` | Read all toggle states |
| `GET` | `/toggle/list` | Read all toggle metadata (id, tooltip, state, icon) |

All endpoints return `204 No Content` on success (except `toggle/click` which
returns `{"id": ..., "state": ...}` and `toggle/states`/`toggle/list`).

---

## Lifecycle notes

- **Buttons and toggles are not persisted.** They are cleared when the droidctrl
  container restarts. Automators should re-register on startup.
- **Multiple automators** can each register their own buttons/toggles. Use
  namespaced IDs (e.g. `tower_gem_mode` not just `gem_mode`) to avoid
  collisions.
- **State is server-side only.** The browser polls `/toggle/states` every 2
  seconds and also receives WebSocket pushes on every state change, so all
  open tabs stay in sync automatically.
- **The automator does not need to run an HTTP server** to receive callbacks.
  If you prefer polling over callbacks, omit the `callback` parameter and
  use `/toggle/states` on a timer.

---

## Full Python example

```python
"""
Example: automator that registers a gem-collection toggle, responds to
browser clicks, and updates its own state via polling.
"""
import time
import threading
import requests
from flask import Flask, request

DROIDCTRL = "http://droidctrl:6080"
MY_HOST   = "http://my-automator:8080"

gem_mode = False
flask_app = Flask(__name__)


@flask_app.route("/on-toggle", methods=["POST"])
def on_toggle():
    global gem_mode
    data = request.get_json()
    if data["id"] == "gem_mode":
        gem_mode = data["state"]
        print(f"[toggle] gem_mode → {gem_mode}")
    return "", 204


def register_ui():
    requests.get(f"{DROIDCTRL}/toggle/register", params={
        "id":       "gem_mode",
        "tooltip":  "Gem collection on/off",
        "state":    str(gem_mode).lower(),
        "callback": f"{MY_HOST}/on-toggle",
    })


def automator_loop():
    while True:
        if gem_mode:
            # do gem-collection work
            pass
        time.sleep(10)


if __name__ == "__main__":
    register_ui()
    threading.Thread(target=automator_loop, daemon=True).start()
    flask_app.run(host="0.0.0.0", port=8080)
```

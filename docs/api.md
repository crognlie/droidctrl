# droidctrl HTTP API

All endpoints are served by the stream server on port `:6080` (player page,
WebSocket stream, most endpoints) and `:6081` (swipe — routed there by Caddy
so wheel-scroll works alongside the video stream). From other containers on
the same Docker network use `http://droidctrl:6080/...`.

All endpoints use `GET`. All return `204 No Content` unless noted.

---

## Stream

### `GET /` — player page
Serves the browser player (`web/player.html`). Open in Chrome 94+.

### `WS /ws` — H.264 video stream
WebSocket endpoint. The server pushes raw Annex-B H.264 chunks; the browser
decodes them with the WebCodecs API. Also carries JSON text frames for
settings, toggle state, and reload signals (see WebSocket messages below).

---

## Settings

### `GET /settings`
Returns current server settings as JSON.
```json
{"bitrate": "8000000", "half_res": false, "native_w": 1080, "native_h": 2400}
```

### `GET /bitrate?value=<rate>`
Change the screenrecord bitrate ceiling. Takes effect immediately (restarts
screenrecord). Persisted across container restarts.

| Parameter | Example | Description |
|-----------|---------|-------------|
| `value` | `8M`, `4M`, `512k`, `6000000` | Bitrate — accepts `M` (megabit), `k` (kilobit), or raw bits/sec |

### `GET /resolution?value=<full|half>`
Switch between full and half native resolution. Half resolution halves both
dimensions (e.g. 1080×2400 → 540×1200), reducing encoder load and bitrate
at the cost of sharpness. Persisted across restarts.

The native resolution is detected at startup from `adb shell wm size` and
returned in `/settings` as `native_w` / `native_h`.

### `GET /reload`
Sends `{"reload": true}` to all connected browser sessions. Each tab reloads
once (sessionStorage cooldown prevents loops). Also called automatically on
container startup — open tabs reload within ~30 seconds to pick up code changes.

---

## Input

All input commands run `adb shell input ...` asynchronously and return `204`
immediately.

### `GET /tap?x=<px>&y=<px>`
Tap at phone-screen coordinates (pixels from top-left of the physical display).

| Parameter | Description |
|-----------|-------------|
| `x` | Horizontal position in phone pixels |
| `y` | Vertical position in phone pixels |

### `GET /drag?x1=<px>&y1=<px>&x2=<px>&y2=<px>&duration=<ms>`
Swipe from one point to another. Duration defaults to 100 ms.

### `GET /swipe?x=<px>&y=<px>&dir=<up|down>&amount=<px>&duration=<ms>`
Directional scroll swipe starting at `(x, y)`. Used by the browser's scroll
wheel. Routed via Caddy to `:6081` to avoid competing with the video stream.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `x`, `y` | screen centre | Origin of the swipe |
| `dir` | `up` | `up` scrolls content up (finger moves up); `down` scrolls down |
| `amount` | `400` | Swipe distance in phone pixels (clamped 100–1600) |
| `duration` | `80` | Gesture duration in ms |

### `GET /key?text=<chars>`
Inject printable text via `adb shell input text`. Shell-quoted so spaces and
most special characters work. One or more characters per call.

### `GET /keyevent?code=<keycode>`
Inject an Android key event by numeric keycode.

| Common keycodes | |
|---|---|
| `3` | Home |
| `4` | Back |
| `24` / `25` | Volume up / down |
| `26` | Power |
| `66` | Enter |
| `67` | Backspace |
| `111` | Escape |
| `187` | Recents (app switcher) |

Full list: [Android KeyEvent constants](https://developer.android.com/reference/android/view/KeyEvent)

---

## Gear modal items

Items appear in the gear modal (⚙ button in the sidebar). Three types:

| Type | Rendering | State |
|------|-----------|-------|
| `toggle` | ON / OFF buttons | bool |
| `button` | Full-width highlighted button | bool — `true` = highlighted |
| `numeric` | Number input field | float |

All items are sorted by `order` then `id`, and changes push to all open
sessions via WebSocket.

### `GET /item/register`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `id` | yes | Unique identifier |
| `type` | yes | `toggle`, `button`, or `numeric` |
| `desc` | yes | Label shown in the modal |
| `state` | no | Initial value — `true`/`false` for toggle/button, a number for numeric (default `false`/`0`) |
| `preserve_state` | no | If `true` and the item already exists, keep current state — recommended on automator restart |
| `order` | no | Sort key (default `0`; ties broken alphabetically by id) |

**Behaviour by type on click:**
- `toggle`: flips state true↔false
- `button`: sets state to `true` (automator detects and can reset via `/item/set`)
- `numeric`: no click handler; value is edited directly in the field

**Startup pattern:**
```python
BASE = "http://droidctrl:6080"

# Toggle: user can flip it on/off
requests.get(f"{BASE}/item/register", params={
    "id": "my_feature", "type": "toggle", "desc": "My feature",
    "state": "false", "preserve_state": "true", "order": "1",
})

# Button: highlights when state=true; automator resets it after handling
requests.get(f"{BASE}/item/register", params={
    "id": "run_now", "type": "button", "desc": "Run now",
    "state": "false", "order": "2",
})

# Numeric: user-editable parameter
requests.get(f"{BASE}/item/register", params={
    "id": "threshold", "type": "numeric", "desc": "Threshold",
    "value": "100", "preserve_state": "true", "order": "3",
})

# Sync state after registration
states = requests.get(f"{BASE}/item/states").json()
```

### `GET /item/set?id=<id>&state=<value>`
Set state. Use `true`/`false` for toggle/button; a number for numeric.
Persisted, pushed to all sessions.

### `GET /item/click?id=<id>`
Simulate a user click. Returns `{"id": "...", "state": current_state_or_value}`.

### `GET /item/deregister?id=<id>`
Remove an item. All open sessions update immediately.

### `GET /item/states`
Returns current state/value for every item: `{id: bool_or_float}`.

### `GET /item/list`
Returns full metadata sorted by `(order, id)`:
```json
[{"id": "my_feature", "type": "toggle", "desc": "My feature", "state": false, "order": 1}]
```

---

## WebSocket messages (server → browser)

Text frames on the `/ws` connection carry JSON.

| Key | Value | Meaning |
|-----|-------|---------|
| `reload` | `true` | Browser should reload the page |
| `settings` | `{"bitrate": "8M", ...}` | Settings changed |
| `item_states` | `{"id": bool_or_float, ...}` | Item state/value update |
| `item_list` | `[{id, type, desc, state, value, order}]` | Item list changed |

Binary frames are raw Annex-B H.264 chunks for the video stream.

---

## Persistence

Bitrate, resolution, and all items (state/value included) are saved to
`/data/stream_settings.json` and survive container restarts. Use
`preserve_state=true` when re-registering so user-set state is not clobbered.

---

## Complete endpoint reference

| Path | Description |
|------|-------------|
| `GET /` | Player page |
| `WS /ws` | H.264 video stream + JSON messages |
| `GET /settings` | Current settings JSON |
| `GET /reload` | Reload all open browser tabs |
| `GET /bitrate?value=X` | Change bitrate ceiling (M/k/raw bps) |
| `GET /resolution?value=full\|half\|quarter` | Switch resolution |
| `GET /tap?x=X&y=Y` | Tap at phone coordinates |
| `GET /drag?x1=…&y1=…&x2=…&y2=…&duration=D` | Drag gesture |
| `GET /swipe?x=X&y=Y&dir=D&amount=A` | Directional scroll swipe (`:6081`) |
| `GET /key?text=T` | Inject text via `adb shell input text` |
| `GET /keyevent?code=N` | Inject Android key event by code |
| `GET /item/register` | Register or update a modal item |
| `GET /item/set?id=X&state=Y` | Set state (bool or number depending on type) |
| `GET /item/click?id=X` | Simulate user click |
| `GET /item/deregister?id=X` | Remove an item |
| `GET /item/states` | All states/values `{id: bool_or_float}` |
| `GET /item/list` | All item metadata |

---

## Full Python automator example

```python
"""
Automator that registers items, polls for state changes, and updates behaviour.
"""
import time
import requests

BASE = "http://droidctrl:6080"


def register_ui():
    requests.get(f"{BASE}/item/register", params={
        "id": "auto_action", "type": "toggle", "desc": "Auto action",
        "state": "false", "preserve_state": "true", "order": "1",
    })
    requests.get(f"{BASE}/item/register", params={
        "id": "run_now", "type": "button", "desc": "Run now",
        "state": "false", "order": "2",
    })
    requests.get(f"{BASE}/item/register", params={
        "id": "threshold", "type": "numeric", "desc": "Threshold",
        "value": "100", "preserve_state": "true", "order": "3",
    })


def get_state():
    return requests.get(f"{BASE}/item/states").json()


def automator_loop():
    state = get_state()
    while True:
        new_state = get_state()
        if new_state != state:
            state = new_state
            print(f"[state] {state}")

        if state.get("run_now"):
            print("[action] run_now triggered")
            # reset the button after handling
            requests.get(f"{BASE}/item/set", params={"id": "run_now", "state": "false"})

        if state.get("auto_action"):
            threshold = state.get("threshold", 100)
            pass  # do work using threshold

        time.sleep(10)


if __name__ == "__main__":
    register_ui()
    automator_loop()
```

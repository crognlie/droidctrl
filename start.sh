#!/bin/bash
set -e

DISPLAY_NUM=:1
SCREEN_RES="${SCREEN_RES:-1080x2400x24}"
VNC_PORT=5900
# Port websockify listens on inside the container (fixed).
INTERNAL_NOVNC_PORT=6080
# Port shown in the startup URL — host-side port if remapped via $NOVNC_PORT in .env.
PUBLIC_NOVNC_PORT="${NOVNC_PORT:-$INTERNAL_NOVNC_PORT}"

SCRCPY_PID=""
ADB_PID=""
GNIREHTET_PID=""

# Graceful shutdown: kill scrcpy first (so it flushes its USB bulk transfers),
# then ask adb server to cleanly close the USB transport. Without this, SIGKILL
# leaves adbd on the phone with a wedged bulk endpoint that breaks next startup.
cleanup() {
    trap - TERM INT EXIT
    echo "[*] Shutdown — killing scrcpy and adb server cleanly"
    [ -n "$SCRCPY_PID" ] && kill -TERM "$SCRCPY_PID" 2>/dev/null || true
    [ -n "$GNIREHTET_PID" ] && kill -TERM "$GNIREHTET_PID" 2>/dev/null || true
    sleep 1
    adb kill-server 2>/dev/null || true
    [ -n "$ADB_PID" ] && kill -TERM "$ADB_PID" 2>/dev/null || true
    exit 0
}
trap cleanup TERM INT EXIT

export XDG_RUNTIME_DIR=/tmp/runtime-root
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

echo "[*] Cleaning stale X locks"
rm -f /tmp/.X${DISPLAY_NUM#:}-lock /tmp/.X11-unix/X${DISPLAY_NUM#:}

echo "[*] Starting Xvfb on $DISPLAY_NUM at $SCREEN_RES"
Xvfb "$DISPLAY_NUM" -screen 0 "$SCREEN_RES" &
export DISPLAY="$DISPLAY_NUM"
sleep 1

echo "[*] Starting ADB server (listening on all interfaces)"
adb -a -P 5037 nodaemon server &
ADB_PID=$!
sleep 1

# If the USB device is wedged from a previous ungraceful shutdown, wait-for-device
# hangs forever. Try a short probe and, on timeout, run USBDEVFS_RESET on the
# phone (matched by vendor ID) before retrying. Max 3 attempts.
echo "[*] Waiting for USB device..."
for attempt in 1 2 3; do
    if timeout 8 adb wait-for-device; then
        break
    fi
    echo "[!] No device after 8s (attempt $attempt/3)"
    if [ "$attempt" -lt 3 ]; then
        python3 /usb_reset.py "${USB_RESET_VID:-18d1}" || true
        adb kill-server 2>/dev/null || true
        sleep 2
        adb -a -P 5037 nodaemon server &
        ADB_PID=$!
        sleep 2
    fi
done
adb wait-for-device
echo "[*] Device found: $(adb devices | tail -n +2 | head -1)"

# Keep device awake over USB so screen-off doesn't drop the connection
echo "[*] Setting device to stay on while plugged in"
adb shell svc power stayon usb 2>/dev/null || true

start_reverse_tether() {
    if [ "${REVERSE_TETHER:-false}" = "true" ]; then
        echo "[*] Starting reverse tether (gnirehtet)"
        [ -n "$GNIREHTET_PID" ] && kill -TERM "$GNIREHTET_PID" 2>/dev/null || true
        (cd /usr/local/bin && gnirehtet run) &
        GNIREHTET_PID=$!
    fi
}
start_reverse_tether

echo "[*] Starting clipboard server on port 6081"
python3 /clipboard.py &

echo "[*] Starting x11vnc on port $VNC_PORT"
x11vnc \
    -display "$DISPLAY_NUM" \
    -nopw \
    -forever \
    -shared \
    -rfbport "$VNC_PORT" \
    -wait 33 \
    -quiet &

echo "[*] Starting noVNC on port $INTERNAL_NOVNC_PORT"
websockify \
    --web /usr/share/novnc \
    "$INTERNAL_NOVNC_PORT" \
    "localhost:$VNC_PORT" &

HOST="${NOVNC_HOST:-localhost}"
echo "[*] Ready — connect at http://${HOST}:${PUBLIC_NOVNC_PORT}/vnc.html?autoconnect=true&resize=scale"

while true; do
    echo "[*] Starting scrcpy"
    scrcpy \
        --no-audio \
        --turn-screen-off \
        --max-fps=30 \
        --video-bit-rate=8M \
        --window-borderless \
        --window-x=0 \
        --window-y=0 \
        --window-width="${SCREEN_W:-1080}" \
        --window-height="${SCREEN_H:-2400}" &
    SCRCPY_PID=$!

    wait "$SCRCPY_PID" 2>/dev/null || true
    echo "[!] scrcpy exited, restarting in 3s..."
    sleep 3
    attempt=0
    while ! timeout 8 adb wait-for-device 2>/dev/null; do
        attempt=$((attempt + 1))
        echo "[!] No device after 8s on reconnect (attempt $attempt), retrying..."
        python3 /usb_reset.py "${USB_RESET_VID:-18d1}" || true
        adb kill-server 2>/dev/null || true
        sleep 2
        adb -a -P 5037 nodaemon server &
        ADB_PID=$!
        sleep 2
    done
    adb shell svc power stayon usb 2>/dev/null || true
    start_reverse_tether
done

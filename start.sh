#!/bin/bash
set -e

DISPLAY_NUM=:1
SCREEN_RES="${SCREEN_RES:-1080x2400x24}"
VNC_PORT=5900
# Port websockify listens on inside the container (fixed).
INTERNAL_NOVNC_PORT=6080
# Port shown in the startup URL — host-side port if remapped via $NOVNC_PORT in .env.
PUBLIC_NOVNC_PORT="${NOVNC_PORT:-$INTERNAL_NOVNC_PORT}"

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
sleep 1

echo "[*] Waiting for USB device..."
adb wait-for-device
echo "[*] Device found: $(adb devices | tail -n +2 | head -1)"

# Keep device awake over USB so screen-off doesn't drop the connection
echo "[*] Setting device to stay on while plugged in"
adb shell svc power stayon usb 2>/dev/null || true

echo "[*] Starting clipboard server on port 6081"
python3 /clipboard.py &

echo "[*] Starting x11vnc on port $VNC_PORT"
x11vnc \
    -display "$DISPLAY_NUM" \
    -nopw \
    -forever \
    -shared \
    -rfbport "$VNC_PORT" \
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
        --max-fps=60 \
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
    adb wait-for-device 2>/dev/null || true
    adb shell svc power stayon usb 2>/dev/null || true
done

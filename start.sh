#!/bin/bash
set -e

ADB_PID=""
GNIREHTET_PID=""

cleanup() {
    trap - TERM INT EXIT
    echo "[*] Shutdown"
    [ -n "$GNIREHTET_PID" ] && kill -TERM "$GNIREHTET_PID" 2>/dev/null || true
    adb kill-server 2>/dev/null || true
    [ -n "$ADB_PID"       ] && kill -TERM "$ADB_PID"       2>/dev/null || true
    exit 0
}
trap cleanup TERM INT EXIT

echo "[*] Starting ADB server (listening on all interfaces)"
adb -a -P 5037 nodaemon server &
ADB_PID=$!
sleep 1

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

echo "[*] Setting device to stay on while plugged in"
adb shell svc power stayon usb 2>/dev/null || true

if [ "${REVERSE_TETHER:-false}" = "true" ]; then
    echo "[*] Starting reverse tether (gnirehtet)"
    (cd /usr/local/bin && gnirehtet run) &
    GNIREHTET_PID=$!
fi

echo "[*] Starting stream server on port 6080"
exec python3 /stream_server.py

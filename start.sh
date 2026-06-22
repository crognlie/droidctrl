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

if [ -n "${ADB_TCP_DEVICE:-}" ]; then
    # Non-Linux hosts (macOS, Windows): connect to phone over TCP/WiFi instead of USB.
    # Enable on the phone first: adb tcpip 5555
    echo "[*] Connecting to ADB TCP device: $ADB_TCP_DEVICE"
    adb connect "$ADB_TCP_DEVICE"
    adb wait-for-device
    echo "[*] Device found: $(adb devices | tail -n +2 | head -1)"
else
    # Linux: wait for USB device with wedge recovery — retries indefinitely.
    echo "[*] Waiting for USB device..."
    attempt=0
    while ! timeout 8 adb wait-for-device; do
        attempt=$((attempt + 1))
        echo "[!] No device after 8s (attempt $attempt), running USB recovery..."
        # ioctl reset (handles stuck/frozen devices)
        python3 /usb_reset.py "${USB_RESET_VID:-18d1}" || true
        # sysfs deauth/reauth (handles auth wedges — more reliable than ioctl alone)
        for d in /sys/bus/usb/devices/*; do
            v=$(cat "$d/idVendor" 2>/dev/null)
            [ "$v" = "${USB_RESET_VID:-18d1}" ] || continue
            echo "[*] sysfs reset: $d"
            echo 0 > "$d/authorized" 2>/dev/null || true
            sleep 1
            echo 1 > "$d/authorized" 2>/dev/null || true
        done
        adb kill-server 2>/dev/null || true
        sleep 2
        adb -a -P 5037 nodaemon server &
        ADB_PID=$!
        sleep 2
    done
    echo "[*] Device found: $(adb devices | tail -n +2 | head -1)"
fi


if [ "${REVERSE_TETHER:-false}" = "true" ]; then
    if command -v gnirehtet >/dev/null 2>&1; then
        echo "[*] Starting reverse tether (gnirehtet)"
        (cd /usr/local/bin && gnirehtet run) &
        GNIREHTET_PID=$!
    else
        echo "[!] REVERSE_TETHER=true but gnirehtet is not available on this architecture"
    fi
fi

echo "[*] Starting stream server on port 6080"
exec python3 /stream_server.py

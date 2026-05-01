#!/usr/bin/env python3
"""
USBDEVFS_RESET any device whose VID matches the first CLI argument (hex).
Optional second argument narrows to a specific PID.

Uses lsusb to locate the device rather than reading raw descriptors.

Usage: usb_reset.py <vid_hex> [pid_hex]
  e.g. usb_reset.py 18d1         # any Google device
       usb_reset.py 18d1 4ee7    # specifically Pixel adb+charging
"""
import fcntl
import subprocess
import sys

USBDEVFS_RESET = 21780  # _IO('U', 20)

target_vid = sys.argv[1].lower().lstrip('0x')
target_pid = sys.argv[2].lower().lstrip('0x') if len(sys.argv) > 2 else None

# lsusb line format: "Bus 006 Device 002: ID 18d1:4ee7 ..."
lines = subprocess.check_output(['lsusb'], text=True).splitlines()

found = 0
for line in lines:
    # e.g. Bus 006 Device 002: ID 18d1:4ee7 Google Inc. ...
    try:
        parts = line.split()
        bus = parts[1].zfill(3)
        dev = parts[3].rstrip(':').zfill(3)
        vid, pid = parts[5].split(':')
    except (IndexError, ValueError):
        continue

    if vid.lower() != target_vid:
        continue
    if target_pid is not None and pid.lower() != target_pid:
        continue

    path = f"/dev/bus/usb/{bus}/{dev}"
    print(f"[usb_reset] resetting {path} ({vid}:{pid})", flush=True)
    try:
        with open(path, 'w') as f:
            fcntl.ioctl(f, USBDEVFS_RESET, 0)
        found += 1
    except OSError as e:
        print(f"[usb_reset] failed {path}: {e}", flush=True)

if found == 0:
    pid_str = target_pid or '*'
    print(f"[usb_reset] no device matching {target_vid}:{pid_str}", flush=True)
    sys.exit(1)

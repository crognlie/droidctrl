#!/usr/bin/env python3
"""
USBDEVFS_RESET any device under /dev/bus/usb whose idVendor matches the first
CLI argument (hex). Optional second argument narrows to a specific idProduct.

Usage: usb_reset.py <vid_hex> [pid_hex]
  e.g. usb_reset.py 18d1         # any Google device (covers Pixel in adb mode)
       usb_reset.py 18d1 4ee7    # specifically Pixel adb+charging

Runs inside the droidctrl container: requires /dev/bus/usb passthrough and
CAP_SYS_ADMIN (granted via privileged: true). No apt deps beyond python3.
"""
import fcntl
import os
import struct
import sys

USBDEVFS_RESET = 21780  # _IO('U', 20)

target_vid = int(sys.argv[1], 16)
target_pid = int(sys.argv[2], 16) if len(sys.argv) > 2 else None

found = 0
for bus in sorted(os.listdir("/dev/bus/usb")):
    bus_dir = f"/dev/bus/usb/{bus}"
    if not os.path.isdir(bus_dir):
        continue
    for dev in sorted(os.listdir(bus_dir)):
        path = f"{bus_dir}/{dev}"
        try:
            with open(path, "rb") as f:
                desc = f.read(18)
            if len(desc) < 18:
                continue
            # Device descriptor: bLength, bDescriptorType, bcdUSB, bDeviceClass,
            # bDeviceSubClass, bDeviceProtocol, bMaxPacketSize0, idVendor,
            # idProduct, bcdDevice, iManufacturer, iProduct, iSerialNumber,
            # bNumConfigurations
            vid, pid = struct.unpack_from("<HH", desc, 8)
            if vid != target_vid:
                continue
            if target_pid is not None and pid != target_pid:
                continue
            print(f"[usb_reset] resetting {path} ({vid:04x}:{pid:04x})", flush=True)
            with open(path, "w") as f:
                fcntl.ioctl(f, USBDEVFS_RESET, 0)
            found += 1
        except OSError as e:
            print(f"[usb_reset] skip {path}: {e}", flush=True)

if found == 0:
    pid_str = f"{target_pid:04x}" if target_pid is not None else "*"
    print(f"[usb_reset] no device matching {target_vid:04x}:{pid_str}", flush=True)
    sys.exit(1)
